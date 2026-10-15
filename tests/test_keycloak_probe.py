"""The Keycloak probe's credential bookkeeping and teardown, checked without a Keycloak stack."""

import importlib.util
import subprocess
import sys
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import ModuleType
from urllib.request import Request
from urllib.response import addinfourl

import pytest

PROBE = Path(__file__).resolve().parents[1] / "probe" / "keycloak" / "probe.py"
LOGIN_URL = "http://localhost:8090/realms/master/login-actions/authenticate"
SERVICE_CONTAINER = "edge-kc-probe-keycloak-1"
RUNNING_APPLY = "edge-kc-probe-apply-0a1b2c3d"
LATE_APPLY = "edge-kc-probe-apply-4e5f6a7b"


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

    def __init__(self, *, apply_lands_during_down: bool) -> None:
        self.containers = {SERVICE_CONTAINER, RUNNING_APPLY}
        self.one_offs = {RUNNING_APPLY, LATE_APPLY}
        self.apply_lands_during_down = apply_lands_during_down

    def run(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        listed = set()
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
    ("apply_lands_during_down", "teardown_line", "left"),
    [
        (False, "teardown: stack edge-kc-probe removed", set()),
        (
            True,
            f"teardown: stack edge-kc-probe FAILED (still there: container {LATE_APPLY})",
            {LATE_APPLY},
        ),
    ],
    ids=["a-sigterm-mid-apply-leaves-nothing", "a-container-left-behind-is-named"],
)
def test_teardown_says_removed_only_when_nothing_of_the_stack_is_left(
    probe: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply_lands_during_down: bool,
    teardown_line: str,
    left: set[str],
) -> None:
    docker = FakeDocker(apply_lands_during_down=apply_lands_during_down)
    monkeypatch.setattr(probe.subprocess, "run", docker.run)

    removed = probe.Stack(tmp_path).down()

    assert capsys.readouterr().out.splitlines() == [teardown_line]
    assert removed == (not left)
    assert docker.containers == left
