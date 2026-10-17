"""`edge stop --all` and `edge start --all` against a fake systemctl: no systemd, no tunnel, and a
`tmp_path` HOME so that no test touches the real switches."""

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_edge_apply import (
    NOW,
    SWITCHED_OFF_AT,
    Ingress,
    ScriptedCompose,
    Terminal,
    switch_off,
)

from edge import barrier, cli, compose, emergency, switches, tunnel

PROBE_FLEET = "services:\n  probe:\n    run: {enabled: true}\n"
FIRST_STOP = "2026-10-17T22:10:04+02:00"
DROP_IN_PATHS = f"DropInPaths=/etc/systemd/system/cloudflared.service.d/{tunnel.BOOT_DROP_IN}\n"


@dataclass
class FakeSystemd:
    """systemctl and pkcheck over one unit's state; records every command."""

    active: bool = True
    refuses_stop: bool = False
    drop_in_installed: bool = True
    authorizes: bool = True
    start_fails: bool = False
    while_asking: Callable[[], None] | None = None
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
        self.active = self.active and self.refuses_stop
        return self._answer(1 if self.refuses_stop else 0)

    def verb_start(self) -> subprocess.CompletedProcess[str]:
        self.active = self.active or not self.start_fails
        return self._answer(1 if self.start_fails else 0)

    def verb_is_active(self) -> subprocess.CompletedProcess[str]:
        return self._answer(0 if self.active else 3, "active\n" if self.active else "inactive\n")

    def verb_show(self) -> subprocess.CompletedProcess[str]:
        return self._answer(0, DROP_IN_PATHS if self.drop_in_installed else "DropInPaths=\n")

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
def terminal_typing(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def operator_types(answer: str = "yes", while_prompt_waits: Callable[[], None] | None = None):
        keyboard = Terminal(f"{answer}\n")
        keyboard.while_prompt_waits = while_prompt_waits
        monkeypatch.setattr(sys, "stdin", keyboard)
        monkeypatch.setattr(sys, "stdout", Terminal())

    return operator_types


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
def stop_all(marks, systemd, capsys) -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        exit_code = emergency.stop_all(marks, NOW, tunnel.Tunnel(run=systemd.run))
        captured = capsys.readouterr()
        return exit_code, captured.out + captured.err

    return run


@pytest.fixture
def start_all(marks, systemd, repository, ingress, terminal_typing, capsys):
    def run(answer: str = "yes", while_prompt_waits=None) -> tuple[int, str]:
        terminal_typing(answer, while_prompt_waits)
        exit_code = emergency.start_all(
            marks,
            NOW,
            tunnel.Tunnel(run=systemd.run),
            compose.Compose(ScriptedCompose().run, repository),
            repository,
            ingress.verifier,
        )
        return exit_code, sys.stdout.getvalue() + capsys.readouterr().err

    return run


def switch_off_probe(marks: switches.Switches, ingress: Ingress, repository: Path) -> None:
    """Unit `probe` switched off the way `edge stop probe` leaves it: marker and barrier file."""
    switch_off(marks.directory, unit="probe")
    config = ScriptedCompose().config
    barrier.Barrier.of_unit(config, ("hello",), repository, "probe", ingress.verifier).close()


# --- edge stop --all -----------------------------------------------------------------------


def test_stop_all_records_the_switch_stops_the_tunnel_and_says_so(stop_all, marks, systemd):
    exit_code, output = stop_all()

    assert exit_code == 0
    assert "the tunnel is stopped (since 18.10. 08:00); services keep running locally" in output
    assert marks.global_mark() is not None
    assert not systemd.active


def test_stop_all_without_a_terminal_never_waits_for_a_password(stop_all, systemd):
    stop_all()

    stop_command = next(call for call in systemd.calls if "stop" in call)
    assert "--no-ask-password" in stop_command


def test_stop_all_at_a_terminal_lets_the_system_ask_for_the_password(
    stop_all, systemd, terminal_typing
):
    terminal_typing()

    stop_all()

    stop_command = next(call for call in systemd.calls if "stop" in call)
    assert "--no-ask-password" not in stop_command


def test_a_refused_stop_is_partial_and_keeps_the_switch(stop_all, marks, systemd):
    systemd.refuses_stop = True

    exit_code, output = stop_all()

    assert exit_code == 1
    assert (
        "partial: the global switch is recorded, but the tunnel still runs; "
        "`sudo systemctl stop cloudflared`" in output
    )
    assert marks.global_mark() is not None


def test_a_missing_boot_drop_in_is_incomplete_though_the_tunnel_is_stopped(
    stop_all, marks, systemd
):
    systemd.drop_in_installed = False

    exit_code, output = stop_all()

    assert exit_code == 1
    assert "incomplete: a reboot would reopen the tunnel" in output
    assert "overnightworks/home-tools#9" in output
    assert not systemd.active
    assert marks.global_mark() is not None


def test_a_repeated_stop_all_keeps_the_first_time(stop_all, marks):
    marks.record_global(emergency.datetime.fromisoformat(FIRST_STOP))

    _, output = stop_all()

    assert "since 17.10. 22:10" in output


def test_stop_all_leaves_the_unit_switches_alone(stop_all, marks):
    switch_off(marks.directory, unit="probe")

    stop_all()

    assert marks.since("probe") == SWITCHED_OFF_AT


def test_the_global_switch_is_no_unit_marker(marks):
    marks.record_global(NOW)

    assert marks.global_mark() is not None
    assert marks.mark("global-stop") is None


def test_stop_all_still_stops_the_tunnel_when_the_switch_cannot_be_recorded(
    stop_all, marks, systemd
):
    marks.directory.mkdir(parents=True)
    (marks.directory / switches.GLOBAL_STOP_FILE).mkdir()

    exit_code, output = stop_all()

    assert exit_code == 1
    assert "could not be recorded" in output
    assert not systemd.active


def test_stop_all_reports_a_missing_systemctl_as_not_stopped(marks, capsys):
    def no_systemctl(_command):
        raise tunnel.TunnelError("cannot run systemctl (missing)")

    exit_code = emergency.stop_all(marks, NOW, tunnel.Tunnel(run=no_systemctl))

    assert exit_code == 1
    assert "the tunnel was not stopped" in capsys.readouterr().err
    assert marks.global_mark() is not None


# --- edge start --all ----------------------------------------------------------------------


@pytest.fixture
def stopped_tunnel(marks, systemd):
    marks.record_global(emergency.datetime.fromisoformat(FIRST_STOP))
    systemd.active = False


def test_start_all_reopens_the_tunnel_and_clears_only_the_global_switch(
    start_all, stopped_tunnel, marks, systemd, ingress, repository
):
    switch_off_probe(marks, ingress, repository)

    exit_code, output = start_all()

    assert exit_code == 0
    assert "the tunnel is open" in output
    assert systemd.active
    assert marks.global_mark() is None
    assert marks.since("probe") == SWITCHED_OFF_AT
    assert (repository / "dynamic" / "switch-probe.yml").exists()


def test_start_all_proves_each_standing_barrier_before_it_reopens(
    start_all, stopped_tunnel, marks, ingress, repository
):
    switch_off_probe(marks, ingress, repository)
    (repository / "dynamic" / "switch-probe.yml").unlink()  # a lost file is rendered again

    _, output = start_all()

    assert "the barrier of probe is proven" in output
    assert "host check not built yet (#50 SW3)" in output
    assert (repository / "dynamic" / "switch-probe.yml").exists()


def test_a_barrier_that_cannot_be_proven_keeps_the_tunnel_closed(
    start_all, stopped_tunnel, marks, ingress, repository, systemd
):
    switch_off_probe(marks, ingress, repository)
    ingress.rejecting = True

    exit_code, output = start_all()

    assert exit_code == 1
    assert "the tunnel stays closed: the barrier of probe is not proven" in output
    assert not systemd.active
    assert marks.global_mark() is not None


def test_authorization_that_fails_leaves_the_tunnel_closed_and_the_switch_recorded(
    start_all, stopped_tunnel, marks, systemd
):
    systemd.authorizes = False

    exit_code, output = start_all()

    assert exit_code == 1
    assert "the system did not authorize the start" in output
    assert "start" not in systemd.verbs()
    assert marks.global_mark().since == FIRST_STOP


def test_authorization_is_asked_before_the_switch_is_cleared(
    start_all, stopped_tunnel, marks, systemd
):
    seen = []
    systemd.while_asking = lambda: seen.append(marks.global_mark())

    start_all()

    assert seen[0] is not None


def test_a_failed_start_records_the_global_switch_again(start_all, stopped_tunnel, marks, systemd):
    systemd.start_fails = True

    exit_code, output = start_all()

    assert exit_code == 1
    assert "the tunnel did not start" in output
    assert marks.global_mark() is not None
    assert not systemd.active


def test_a_stop_during_the_prompt_wins(start_all, stopped_tunnel, marks, systemd):
    exit_code, output = start_all(while_prompt_waits=lambda: marks.record_global(NOW))

    assert exit_code == 1
    assert "switched off again meanwhile" in output
    assert "start" not in systemd.verbs()
    assert marks.global_mark() is not None


@pytest.mark.parametrize("answer", ["no", "", "Yes", "yes please"])
def test_only_the_exact_yes_reopens_the_tunnel(start_all, stopped_tunnel, marks, systemd, answer):
    exit_code, _ = start_all(answer)

    assert exit_code == 1
    assert marks.global_mark() is not None
    assert not systemd.active


def test_start_all_refuses_without_a_terminal(marks, systemd, capsys, stopped_tunnel):
    exit_code = emergency.start_all(marks, NOW, tunnel.Tunnel(run=systemd.run))

    assert exit_code == 1
    assert "must be a terminal" in capsys.readouterr().err
    assert not systemd.calls
    assert marks.global_mark() is not None


def test_start_all_with_nothing_switched_off_changes_nothing(start_all, systemd):
    exit_code, output = start_all()

    assert exit_code == 0
    assert "no change" in output
    assert "start" not in systemd.verbs()


# --- the command line ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments", [["stop", "probe", "--all"], ["start", "probe", "--all"], ["stop"], ["start"]]
)
def test_a_unit_and_all_exclude_each_other_and_one_is_required(arguments, capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main(arguments)

    assert refused.value.code == 2
    assert "usage:" in capsys.readouterr().err
