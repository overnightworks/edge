"""The one reader of probe-rp's logout log lines in probe/keycloak/probe.py: what counts as a
logout record, and what a failed log read is."""

import json
import subprocess
from types import ModuleType, SimpleNamespace

import pytest
import test_keycloak_probe

keycloak_probe = test_keycloak_probe.probe  # the fixture that loads probe.py


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def logout_line(user_id: str) -> str:
    return "backchannel-logout " + json.dumps({"sub": user_id, "iss": "issuer", "sid": "s"})


@pytest.mark.parametrize(
    ("read_takes_seconds", "found"),
    [(1, True), (20, False)],
    ids=["a read inside the bound counts", "a read that returns after the bound does not"],
)
def test_a_logout_record_counts_only_when_it_was_read_within_the_bound(
    keycloak_probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    read_takes_seconds: int,
    found: bool,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(keycloak_probe, "time", clock_module(clock))
    monkeypatch.setattr(keycloak_probe.Probe, "logout_timeout_seconds", 20)

    def logs(_service: str) -> str:
        clock.now += read_takes_seconds
        return "noise\n" + logout_line("someone") + "\n" + logout_line("user-1") + "\n"

    record = keycloak_probe.Probe(stack=SimpleNamespace(logs=logs)).logout_record("user-1")

    assert (record is not None) is found


def test_a_logout_record_is_absent_when_the_log_never_names_the_user(
    keycloak_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(keycloak_probe, "time", clock_module(clock))

    record = keycloak_probe.Probe(
        stack=SimpleNamespace(logs=lambda _service: logout_line("other"))
    ).logout_record("user-1")

    assert record is None
    assert clock.now >= keycloak_probe.Probe.logout_timeout_seconds


def test_a_failed_log_read_fails_the_probe_instead_of_reading_as_no_logout(
    keycloak_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = object.__new__(keycloak_probe.Stack)
    monkeypatch.setattr(
        stack,
        "compose",
        lambda *_args: subprocess.CompletedProcess([], 1, stdout="", stderr="no such service"),
        raising=False,
    )

    with pytest.raises(keycloak_probe.ProbeFailure, match="compose logs failed"):
        keycloak_probe.Probe(stack=stack).logout_record("user-1")


class ControlProbe:
    """The cell's probe, reduced to what the control touches; any call past the control's gate
    shows as a recorded measurement."""

    logout_timeout_seconds = 20

    def __init__(self, control_logout: dict[str, str] | None, admin_status: int) -> None:
        self.control_logout = control_logout
        self.admin = SimpleNamespace(
            create_user=lambda *_: "user-1",
            call=lambda *_: SimpleNamespace(status=admin_status),
            create=lambda *_: "client-uuid",
            join_group=lambda *_: None,
            group_paths=lambda _user_id: ["/apps/songmaker"],
            get=lambda _path: {"value": "secret", "id": "group-id"},
        )
        self.reported: list[str] = []

    def admin_now(self) -> SimpleNamespace:
        return self.admin

    def keep_secret(self, _name: str, value: str) -> str:
        return value

    def through_tunnel(self, *_args: object, **_kwargs: object) -> object:
        return object()

    def tokens_from(self, *_args: object) -> dict[str, str]:
        return {}

    def logout_record(self, _user_id: str) -> dict[str, str] | None:
        return self.control_logout

    def held(self, proof: str, detail: str) -> None:
        self.reported.append(f"{proof} {detail}")


@pytest.mark.parametrize(
    ("admin_status", "control_logout", "message"),
    [
        (500, {"sub": "user-1"}, "control's admin call answered 500"),
        (204, None, "control's logout did not arrive within 20 s"),
    ],
    ids=["the control's admin call fails", "the control's logout never arrives"],
)
def test_a_control_that_fails_stops_the_cell_before_it_measures_any_action(
    keycloak_probe: ModuleType,
    admin_status: int,
    control_logout: dict[str, str] | None,
    message: str,
) -> None:
    probe = ControlProbe(control_logout, admin_status)

    with pytest.raises(keycloak_probe.ProbeFailure, match=message):
        keycloak_probe.logout_actions_cell.prove(probe, keycloak_probe.expect, "/apps/songmaker")

    assert probe.reported == []


def clock_module(clock: FakeClock) -> SimpleNamespace:
    return SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
