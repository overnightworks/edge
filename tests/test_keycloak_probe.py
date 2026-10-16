"""The Keycloak probe's credential bookkeeping and teardown, checked without a Keycloak stack."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.request import Request
from urllib.response import addinfourl

import pytest

PROBE = Path(__file__).resolve().parents[1] / "probe" / "keycloak" / "probe.py"
LOGIN_URL = "http://localhost:8090/realms/master/login-actions/authenticate"
SERVICE_CONTAINER = "edge-kc-probe-keycloak-1"
RUNNING_APPLY = "edge-kc-probe-apply-0a1b2c3d"
LATE_APPLY = "edge-kc-probe-apply-4e5f6a7b"
DAEMON_GONE = "Cannot connect to the Docker daemon"


@pytest.fixture(scope="module")
def probe() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("keycloak_probe", PROBE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def response_setting(*set_cookie_lines: str) -> addinfourl:
    headers = Message()
    for line in set_cookie_lines:
        headers["Set-Cookie"] = line
    return addinfourl(BytesIO(b""), headers, LOGIN_URL)


def test_every_cookie_value_reaches_proof_7_although_a_later_response_replaces_or_expires_it(
    probe: ModuleType,
) -> None:
    kept: dict[str, str] = {}
    jar = probe.ReceivedCookies(lambda name, value: kept.setdefault(name, value))
    request = Request(LOGIN_URL)

    jar.extract_cookies(
        response_setting(
            "KC_RESTART=restart-1; Path=/realms/master/; HttpOnly",
            "AUTH_SESSION_ID=session-1; Path=/realms/master/",
        ),
        request,
    )
    jar.extract_cookies(
        response_setting(
            "KC_RESTART=; Max-Age=0; Path=/realms/master/",
            "AUTH_SESSION_ID=session-2; Path=/realms/master/",
        ),
        request,
    )

    assert sorted(kept.values()) == ["restart-1", "session-1", "session-2"]


class FakeDocker:
    """The docker CLI as the teardown asks it, over the probe project's containers."""

    def __init__(self, *, apply_lands_during_down: bool, unlistable_kind: str | None) -> None:
        self.containers = {SERVICE_CONTAINER, RUNNING_APPLY}
        self.one_offs = {RUNNING_APPLY, LATE_APPLY}
        self.apply_lands_during_down = apply_lands_during_down
        self.unlistable_kind = unlistable_kind

    def run(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        listed = set()
        if command[1:3] == [self.unlistable_kind, "ls"]:
            return subprocess.CompletedProcess(command, 1, "", DAEMON_GONE)
        if command[:2] == ["docker", "compose"] and "down" in command:
            # Like Compose, down leaves a running one-off container alone.
            self.containers -= {SERVICE_CONTAINER}
            if self.apply_lands_during_down:
                self.containers.add(LATE_APPLY)
        elif command[1:3] == ["container", "rm"]:
            self.containers -= set(command[3:])
        elif command[1:3] == ["container", "ls"]:
            one_offs_only = "label=com.docker.compose.oneoff=True" in command
            listed = self.containers & self.one_offs if one_offs_only else self.containers
        return subprocess.CompletedProcess(command, 0, "\n".join(sorted(listed)), "")


@pytest.mark.parametrize(
    ("apply_lands_during_down", "unlistable_kind", "teardown_line", "removed", "containers_left"),
    [
        (False, None, "teardown: stack edge-kc-probe removed", True, set()),
        (
            True,
            None,
            f"teardown: stack edge-kc-probe FAILED (still there: container {LATE_APPLY})",
            False,
            {LATE_APPLY},
        ),
        (
            False,
            "volume",
            f"teardown: stack edge-kc-probe FAILED (still there: volumes unlisted ({DAEMON_GONE}))",
            False,
            set(),
        ),
    ],
    ids=[
        "a-sigterm-mid-apply-leaves-nothing",
        "a-container-left-behind-is-named",
        "a-failing-listing-is-not-taken-for-nothing-left",
    ],
)
def test_teardown_says_removed_only_when_nothing_of_the_stack_is_left(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply_lands_during_down: bool,
    unlistable_kind: str | None,
    teardown_line: str,
    removed: bool,
    containers_left: set[str],
) -> None:
    docker = FakeDocker(
        apply_lands_during_down=apply_lands_during_down, unlistable_kind=unlistable_kind
    )
    monkeypatch.setattr(probe.subprocess, "run", docker.run)
    # The compose environment names the docker group, which a host without Docker lacks.
    monkeypatch.setattr(probe.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=999))

    said_removed = probe.Stack(tmp_path).down()

    assert capsys.readouterr().out.splitlines() == [teardown_line]
    assert said_removed == removed
    assert docker.containers == containers_left


@pytest.fixture
def restored_signal_handlers() -> Iterator[None]:
    saved = {name: signal.getsignal(name) for name in (signal.SIGTERM, signal.SIGINT)}
    yield
    for name, handler in saved.items():
        signal.signal(name, handler)


def run_main(
    probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    scratch: Path,
    *,
    run_signal: signal.Signals | None,
    down_signal: signal.Signals | None,
) -> int:
    """main() over a stack and proofs that each receive a signal, as `timeout` or Ctrl-C would."""

    class FakeProbe:
        def __init__(self, _stack: object) -> None: ...

        def run(self) -> None:
            if run_signal:
                os.kill(os.getpid(), run_signal)

    class FakeStack:
        def __init__(self, _scratch: Path) -> None: ...

        def down(self) -> bool:
            if down_signal:
                os.kill(os.getpid(), down_signal)
            print("teardown: stack edge-kc-probe removed", flush=True)
            return True

    monkeypatch.setattr(sys, "argv", ["probe.py", "--scratch", str(scratch)])
    monkeypatch.setattr(probe, "ports_free", lambda *_: [])
    monkeypatch.setattr(probe, "Probe", FakeProbe)
    monkeypatch.setattr(probe, "Stack", FakeStack)
    return probe.main()


@pytest.mark.parametrize("signal_name", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_teardown_does_not_cut_it_short(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_signal_handlers: None,
    signal_name: signal.Signals,
) -> None:
    exit_code = run_main(
        probe, monkeypatch, tmp_path / "s", run_signal=None, down_signal=signal_name
    )

    assert exit_code == 0
    assert "teardown: stack edge-kc-probe removed" in capsys.readouterr().out


@pytest.mark.parametrize("signal_name", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_proofs_tears_down_and_ends_with_one_failed_line(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_signal_handlers: None,
    signal_name: signal.Signals,
) -> None:
    exit_code = run_main(
        probe, monkeypatch, tmp_path / "s", run_signal=signal_name, down_signal=None
    )

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        "teardown: stack edge-kc-probe removed",
        f"PROBE FAILED: stopped by {signal_name.name}",
    ]


def test_a_scratch_that_is_a_file_or_not_empty_is_refused_with_the_usage_exit_code(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a_file = tmp_path / "file"
    a_file.write_text("x")
    non_empty = tmp_path / "dir"
    non_empty.mkdir()
    (non_empty / "x").write_text("x")

    for scratch in (a_file, non_empty):
        with pytest.raises(SystemExit) as refused:
            run_main(probe, monkeypatch, scratch, run_signal=None, down_signal=None)
        assert refused.value.code == 2
        assert f"{scratch} is not an empty directory" in capsys.readouterr().err


def test_a_taken_port_is_refused_with_its_own_exit_code(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["probe.py", "--scratch", str(tmp_path / "s")])
    monkeypatch.setattr(probe, "ports_free", lambda *_: [8081])

    assert probe.main() == 3


SECRET_VALUE = "s3cret-value-that-must-not-be-printed"
FRESH_APPLY_LINE = "fresh apply done"


class HeldStack:
    """A stack whose bring-up is done and whose teardown only says so."""

    def __init__(self, scratch: Path) -> None:
        self.secrets_dir = scratch / "secrets"

    def down(self) -> bool:
        print("teardown: stack edge-kc-probe removed", flush=True)
        return True


class OperatorInput:
    """Stdin that ends the way a caller would: by a signal or by closing the pipe."""

    def __init__(self, signal_to_send: signal.Signals | None) -> None:
        self.signal_to_send = signal_to_send

    def read(self) -> str:
        if self.signal_to_send:
            os.kill(os.getpid(), self.signal_to_send)
        return ""


def run_hold(
    probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    scratch: Path,
    *,
    before_ready: signal.Signals | None = None,
    after_ready: signal.Signals | None = None,
) -> int:
    def start_with_bootstrap_admin(self: object) -> None:
        self.secret_values = {"config-cli-secret": SECRET_VALUE}
        print("stack: up with the temporary bootstrap admin", flush=True)

    def apply_and_restart(_self: object) -> None:
        if before_ready:
            os.kill(os.getpid(), before_ready)

    monkeypatch.setattr(sys, "argv", ["probe.py", "hold", "--scratch", str(scratch)])
    monkeypatch.setattr(sys, "stdin", OperatorInput(after_ready))
    monkeypatch.setattr(probe, "ports_free", lambda *_: [])
    monkeypatch.setattr(probe, "Stack", HeldStack)
    monkeypatch.setattr(probe.Probe, "start_with_bootstrap_admin", start_with_bootstrap_admin)
    monkeypatch.setattr(probe.Probe, "apply_and_restart", apply_and_restart)
    return probe.main()


def ready_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("READY ")]


def test_hold_prints_one_ready_line_naming_the_secret_file_and_no_secret_value(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_signal_handlers: None,
) -> None:
    scratch = tmp_path / "s"
    run_hold(probe, monkeypatch, scratch)

    output = capsys.readouterr().out
    [ready] = ready_lines(output)
    assert json.loads(ready.removeprefix("READY ")) == {
        "admin_url": "http://localhost:8090",
        "edge_url": "http://127.0.0.1:8081",
        "realm": "hallucinai",
        "public_issuer": "https://auth.hallucinai.de/realms/hallucinai",
        "admin_client_id": "config-cli",
        "admin_client_secret_file": str(scratch.resolve() / "secrets" / "config-cli-secret"),
    }
    assert SECRET_VALUE not in output


@pytest.mark.parametrize(
    "ending", [signal.SIGTERM, signal.SIGINT, None], ids=["sigterm", "sigint", "stdin-eof"]
)
def test_hold_ends_normally_when_told_to_stop_after_ready(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_signal_handlers: None,
    ending: signal.Signals | None,
) -> None:
    exit_code = run_hold(probe, monkeypatch, tmp_path / "s", after_ready=ending)

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert [line.split(" ")[0] for line in lines] == ["stack:", "READY", "teardown:", "HOLD"]
    assert lines[-2:] == ["teardown: stack edge-kc-probe removed", "HOLD ENDED"]


def test_hold_stopped_by_a_signal_before_ready_tears_down_and_fails(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_signal_handlers: None,
) -> None:
    exit_code = run_hold(probe, monkeypatch, tmp_path / "s", before_ready=signal.SIGTERM)

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 1
    assert ready_lines("\n".join(lines)) == []
    assert lines[-2:] == [
        "teardown: stack edge-kc-probe removed",
        "PROBE FAILED: stopped by SIGTERM",
    ]


def test_run_proves_3b_before_the_fresh_apply_and_3a_after_it(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def start_with_bootstrap_admin(_self: object) -> object:
        print("stack: up with the temporary bootstrap admin", flush=True)
        return object()

    def missing_variable_fails_loudly(self: object, _admin: object) -> None:
        self.held("3b", "an undefined variable fails the apply")

    def apply_and_restart(_self: object) -> object:
        print(FRESH_APPLY_LINE, flush=True)
        return object()

    for later_proof in (
        "seed_member",
        "reapply_is_idempotent",
        "public_issuer_and_paths",
        "admin_console_only_on_loopback",
        "changed_yaml_applies",
        "create_probe_rp",
        "member_token_carries_groups_and_audience",
        "disabled_user_refused_on_refresh",
        "backchannel_logout_reaches_rp",
        "no_secret_leaked",
    ):
        monkeypatch.setattr(probe.Probe, later_proof, lambda *_: None)
    monkeypatch.setattr(probe.Probe, "start_with_bootstrap_admin", start_with_bootstrap_admin)
    monkeypatch.setattr(probe.Probe, "missing_variable_fails_loudly", missing_variable_fails_loudly)
    monkeypatch.setattr(probe.Probe, "apply_and_restart", apply_and_restart)

    probe.Probe(stack=object()).run()

    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" held:")[0] for line in lines] == [
        "stack: up with the temporary bootstrap admin",
        "PROOF 3b",
        FRESH_APPLY_LINE,
        "PROOF 3a",
    ]
