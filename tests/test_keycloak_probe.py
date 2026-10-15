"""The Keycloak probe's credential bookkeeping and teardown, checked without a Keycloak stack."""

import importlib.util
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
    assert spec is not None and spec.loader is not None
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
