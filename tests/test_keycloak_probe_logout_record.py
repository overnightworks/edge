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
    monkeypatch.setattr(keycloak_probe, "LOGOUT_TIMEOUT_SECONDS", 20)

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
    assert clock.now >= keycloak_probe.LOGOUT_TIMEOUT_SECONDS


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


def clock_module(clock: FakeClock) -> SimpleNamespace:
    return SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
