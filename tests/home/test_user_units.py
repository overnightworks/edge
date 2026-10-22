"""The user-units step of `hal setup` against a sandboxed HOME and a fake `systemctl --user`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from home_tools import setup
from home_tools.cli import build_layout_and_ports
from home_tools.system_step import Applied, CheckState, StepFailed
from home_tools.tidy_home import Layout
from home_tools.user_units import UserUnits

SERVICE = "backup.service"
TIMER = "backup.timer"
LOADED_BACKUP = f"  {SERVICE}: ExecStart=/checkout/bin/backup\n  {SERVICE}: Result=success\n"
LOADED_HAL_BACKUP = (
    f"  {SERVICE}: ExecStart=/checkout/bin/hal backup\n  {SERVICE}: Result=success\n"
)
MUTATING_VERBS = {"daemon-reload", "enable", "disable"}


class FakeSystemctl:
    """systemd's view of the user units: what it loaded at the last reload, and its timers."""

    def __init__(self, user_units: Path) -> None:
        self.user_units = user_units
        self.calls: list[list[str]] = []
        self.failing_verb: str | None = None
        self.loaded: dict[str, str] = {}
        self.enabled: set[str] = set()
        self.last_result = "success"

    def __call__(self, argv: Sequence[str]) -> str | None:
        assert argv[:2] == ["systemctl", "--user"]
        verb, *arguments = argv[2:]
        self.calls.append([verb, *arguments])
        if verb == self.failing_verb:
            return None
        if verb == "daemon-reload":
            self.loaded = {
                link.name: link.read_text() for link in self.user_units.iterdir() if link.exists()
            }
        elif verb == "enable":
            self.enabled.add(arguments[-1])
        elif verb == "disable":
            self.enabled.discard(arguments[-1])
            self.loaded.pop(arguments[-1], None)
        else:
            return self.show(arguments[0])
        return ""

    def show(self, name: str) -> str:
        link = self.user_units / name
        stale = link.exists() and self.loaded.get(name) != link.read_text()
        exec_start = next(
            (
                line
                for line in self.loaded.get(name, "").splitlines()
                if line.startswith("ExecStart=")
            ),
            "",
        )
        command = exec_start.removeprefix("ExecStart=")
        return "\n".join(
            [
                f"NeedDaemonReload={'yes' if stale else 'no'}",
                f"UnitFileState={'enabled' if name in self.enabled else 'linked'}",
                f"Result={self.last_result}",
                f"ActiveState={'active' if name in self.enabled else 'inactive'}",
                f"ExecStart={{ path={command} ; argv[]={command} ; ignore_errors=no ; }}"
                if command
                else "ExecStart=",
            ]
        )

    @property
    def changes(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] in MUTATING_VERBS]


class Machine:
    """A home whose checkout holds one service and its timer, with a fake systemd."""

    def __init__(self, home: Path) -> None:
        self.layout = Layout(home)
        self.checkout_units = self.layout.git / "edge" / "systemd"
        self.user_units = home / ".config" / "systemd" / "user"
        self.user_units.mkdir(parents=True)
        self.checkout_units.mkdir(parents=True)
        self.write_unit(SERVICE, "ExecStart=/checkout/bin/backup\n")
        self.write_unit(TIMER, "OnCalendar=weekly\n")
        self.systemctl = FakeSystemctl(self.user_units)
        self.step = UserUnits(self.layout, self.systemctl)

    def write_unit(self, name: str, content: str) -> None:
        (self.checkout_units / name).write_text(content)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path / "home")


def test_a_fresh_apply_links_reloads_then_enables_the_timer_and_shows_the_loaded_command(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert machine.step.apply() is Applied.CHANGED

    assert [(machine.user_units / name).readlink() for name in (SERVICE, TIMER)] == [
        machine.checkout_units / SERVICE,
        machine.checkout_units / TIMER,
    ]
    assert machine.systemctl.changes == [["daemon-reload"], ["enable", "--now", TIMER]]
    assert capsys.readouterr().out == LOADED_BACKUP
    assert machine.step.is_verified()


def test_a_dry_run_reports_a_change_and_touches_nothing(machine: Machine) -> None:
    assert machine.step.check() is CheckState.WOULD_CHANGE

    assert list(machine.user_units.iterdir()) == []
    assert machine.systemctl.changes == []


def test_a_second_apply_changes_nothing_and_still_shows_the_loaded_command(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.step.apply()
    capsys.readouterr()
    machine.systemctl.calls.clear()

    assert (machine.step.apply(), machine.step.check()) == (Applied.UNCHANGED, CheckState.UNCHANGED)
    assert machine.systemctl.changes == []
    assert capsys.readouterr().out == LOADED_BACKUP


def test_a_changed_unit_file_is_pending_until_systemd_has_reloaded_it(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.step.apply()
    capsys.readouterr()
    machine.write_unit(SERVICE, "ExecStart=/checkout/bin/hal backup\n")

    assert not machine.step.is_verified()
    assert machine.step.apply() is Applied.UNCHANGED
    assert machine.systemctl.changes[-1] == ["daemon-reload"]
    assert capsys.readouterr().out == LOADED_HAL_BACKUP
    assert machine.step.is_verified()


def test_a_renamed_unit_has_its_old_link_disabled_and_removed_and_the_new_one_linked(
    machine: Machine,
) -> None:
    machine.step.apply()
    (machine.checkout_units / TIMER).rename(machine.checkout_units / "backup-weekly.timer")

    assert not machine.step.is_verified()
    machine.step.apply()

    assert sorted(path.name for path in machine.user_units.iterdir()) == [
        "backup-weekly.timer",
        SERVICE,
    ]
    assert ["disable", TIMER] in machine.systemctl.changes
    assert machine.systemctl.enabled == {"backup-weekly.timer"}
    assert machine.step.is_verified()


def test_a_link_to_a_file_outside_the_checkout_is_left_alone(
    machine: Machine, tmp_path: Path
) -> None:
    machine.step.apply()
    foreign = machine.user_units / "foreign.service"
    foreign.symlink_to(tmp_path / "gone.service")

    assert machine.step.is_verified()
    assert foreign.is_symlink()


def test_a_unit_file_of_its_own_in_the_user_directory_is_not_overwritten(machine: Machine) -> None:
    (machine.user_units / SERVICE).write_text("ExecStart=/mine\n")

    with pytest.raises(StepFailed, match="file of its own"):
        machine.step.apply()

    assert (machine.user_units / SERVICE).read_text() == "ExecStart=/mine\n"


@pytest.mark.parametrize("verb", ["daemon-reload", "enable", "show"])
def test_a_failing_systemctl_fails_the_step_loudly(machine: Machine, verb: str) -> None:
    machine.step.apply()
    machine.write_unit(SERVICE, "ExecStart=/checkout/bin/hal backup\n")
    machine.systemctl.enabled.clear()
    machine.systemctl.failing_verb = verb

    with pytest.raises(StepFailed, match=f"systemctl --user {verb}"):
        machine.step.apply()
    assert not machine.step.is_verified()


def test_hal_setup_lists_the_step_as_pending_until_the_units_are_settled(machine: Machine) -> None:

    def only_systemctl(argv: Sequence[str]) -> str | None:
        return machine.systemctl(argv) if argv[0] == "systemctl" else None

    layout, ports = build_layout_and_ports(machine.layout.home, run=only_systemctl)

    assert "User units" in [step.name for step in setup.pending(layout, ports)]
    machine.step.apply()
    assert "User units" not in [step.name for step in setup.pending(layout, ports)]


def test_a_failed_last_run_of_a_service_is_shown_with_its_result(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.systemctl.last_result = "exit-code"

    machine.step.apply()

    assert f"  {SERVICE}: Result=exit-code\n" in capsys.readouterr().out
