"""What the `test_stack_global_stop` tests share with other test modules."""

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from support.stack_apply import (
    NOW,
    at_terminal,  # noqa: F401 the fixture, requested by name below
)
from support.stack_apply_fakes import (
    Ingress,
    ScriptedCompose,
)

from hal.stack import compose, emergency, switches, tunnel

PROBE_FLEET = "services:\n  probe:\n    run: {enabled: true}\n"


FIRST_STOP = "2026-10-17T22:10:04+02:00"


DROP_IN_DIRECTORY = "/etc/systemd/system/cloudflared.service.d"


@dataclass
class FakeSystemd:
    """systemctl and pkcheck over one unit's state; records every command."""

    active: bool = True
    refuses_stop: bool = False
    stop_times_out_after_stopping: bool = False
    drop_ins: list[str] = field(default_factory=lambda: [tunnel.BOOT_DROP_IN])
    authorizes: bool = True
    start_fails: bool = False
    start_leaves_unit_activating: bool = False
    state_unreadable_after_start: bool = False
    activating: bool = False
    started: bool = False
    while_asking: Callable[[], None] | None = None
    while_stopping: Callable[[], None] | None = None
    while_starting: Callable[[], None] | None = None
    calls: list[list[str]] = field(default_factory=list)

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[0] == "pkcheck":
            if self.while_asking:
                self.while_asking()
            return self._answer(0 if self.authorizes else 1)
        verb = next(word for word in command[2:] if not word.startswith("-"))
        return getattr(self, f"verb_{verb.replace('-', '_')}")()

    def verb_stop(self) -> subprocess.CompletedProcess[str]:
        if self.while_stopping:
            self.while_stopping()
        self.active = self.active and self.refuses_stop
        self.activating = self.activating and self.refuses_stop
        if self.stop_times_out_after_stopping:
            raise tunnel.TunnelError("cannot run systemctl (timed out)")
        return self._answer(1 if self.refuses_stop else 0)

    def verb_start(self) -> subprocess.CompletedProcess[str]:
        if self.while_starting:
            self.while_starting()
        self.started = True
        self.activating = self.start_leaves_unit_activating
        self.active = (self.active or not self.start_fails) and not self.activating
        return self._answer(1 if self.start_fails else 0)

    def verb_is_active(self) -> subprocess.CompletedProcess[str]:
        if self.started and self.state_unreadable_after_start:
            raise tunnel.TunnelError("cannot run systemctl (timed out)")
        if self.activating:
            return self._answer(3, "activating\n")
        return self._answer(0 if self.active else 3, "active\n" if self.active else "inactive\n")

    def verb_show(self) -> subprocess.CompletedProcess[str]:
        paths = " ".join(f"{DROP_IN_DIRECTORY}/{name}" for name in self.drop_ins)
        return self._answer(0, f"DropInPaths={paths}\n")

    @staticmethod
    def _answer(code: int, output: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, output)

    def verbs(self) -> list[str]:
        return [call[2] for call in self.calls if call[0] == "systemctl"]


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def marks(home: Path) -> switches.Switches:
    return switches.Switches(switches.default_state_directory())


@pytest.fixture
def systemd() -> FakeSystemd:
    return FakeSystemd()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "fleet.yaml").write_text(PROBE_FLEET)
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "traefik.yml").write_text('entryPoints:\n  edge:\n    address: "127.0.0.1:8081"\n')
    return tmp_path


@pytest.fixture
def ingress(repository: Path) -> Ingress:
    return Ingress(repository)


@pytest.fixture
def start_all(marks, systemd, repository, ingress, at_terminal, capsys):  # noqa: F811
    def run(
        answer: str = "yes", while_prompt_waits=None, unit: str = tunnel.CLOUDFLARED_UNIT
    ) -> tuple[int, str]:
        at_terminal(answer, while_prompt_waits)
        exit_code = emergency.start_all(
            marks,
            NOW,
            tunnel.Tunnel(unit, run=systemd.run),
            compose.Compose(ScriptedCompose().run, repository),
            repository,
            ingress.verifier,
        )
        return exit_code, sys.stdout.getvalue() + capsys.readouterr().err

    return run


@pytest.fixture
def hosts_asked(ingress: Ingress, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Host of every request Traefik is asked; a test names this fixture before the ones that
    build the verifier, so the verifier asks through it."""
    asked: list[str] = []
    answer = ingress.fetch

    def fetch(address: str, host: str, seconds: float, clock: Callable[[], float]):
        asked.append(host)
        return answer(address, host, seconds, clock)

    monkeypatch.setattr(ingress, "fetch", fetch)
    return asked


@pytest.fixture
def stopped_tunnel(marks, systemd):
    marks.record_global(emergency.datetime.fromisoformat(FIRST_STOP))
    systemd.active = False
