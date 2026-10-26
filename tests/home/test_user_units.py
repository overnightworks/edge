"""The user-units step of `hal setup` against a sandboxed HOME and a fake `systemctl --user`."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from home_tools import setup
from home_tools.cli import build_layout_and_ports, edge_units_directory
from home_tools.system_step import Applied, CheckState, StepFailed
from home_tools.tidy_home import Layout
from home_tools.user_units import LoadedUnit, UserUnits

USER_UNITS_STEP = "User units"
SERVICE = "backup.service"
TIMER = "backup.timer"
TIMERS_WANTS = "timers.target.wants"
LOADED_BACKUP = [f"  {SERVICE}: ExecStart=/checkout/bin/backup", f"  {SERVICE}: Result=success"]
LOADED_HAL_BACKUP = [
    f"  {SERVICE}: ExecStart=/checkout/bin/hal backup",
    f"  {SERVICE}: Result=success",
]
MUTATING_VERBS = {"daemon-reload", "enable", "disable", "stop"}


class FakeSystemctl:
    """systemd's view of the user units: what it loaded at the last reload, and what runs.

    Like systemd 255 it keeps a unit loaded until the next reload, and it refuses to disable a
    unit whose file is gone.
    """

    def __init__(self, user_units: Path) -> None:
        self.user_units = user_units
        self.calls: list[list[str]] = []
        self.failing_verb: str | None = None
        self.loaded: dict[str, str] = {}
        self.active: set[str] = set()
        self.last_result = "success"
        self.starts_nothing = False
        self.loaded_from: dict[str, str] = {}

    def __call__(self, argv: Sequence[str]) -> str | None:
        assert argv[:2] == ["systemctl", "--user"]
        verb, *arguments = argv[2:]
        self.calls.append([verb, *arguments])
        if verb == self.failing_verb:
            return None
        if verb == "daemon-reload":
            self.loaded = {
                link.name: link.read_text() for link in self.user_units.iterdir() if link.is_file()
            }
        elif verb == "enable":
            self.enable(arguments[-1])
        elif verb == "disable":
            return self.disable(arguments[-1])
        elif verb == "stop":
            return self.stop(arguments[-1])
        else:
            return self.show(arguments[0])
        return ""

    def enable(self, name: str) -> None:
        wants = self.user_units / TIMERS_WANTS
        wants.mkdir(exist_ok=True)
        (wants / name).symlink_to((self.user_units / name).readlink())
        if not self.starts_nothing:
            self.active.add(name)

    def disable(self, name: str) -> str | None:
        if not (self.user_units / name).exists():
            return None
        (self.user_units / TIMERS_WANTS / name).unlink(missing_ok=True)
        return ""

    def stop(self, name: str) -> str | None:
        if name not in self.loaded:
            return None
        self.active.discard(name)
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
        fragment_path = self.loaded_from.get(name, str(link) if name in self.loaded else "")
        enabled = (self.user_units / TIMERS_WANTS / name).exists()
        return "\n".join(
            [
                f"NeedDaemonReload={'yes' if stale else 'no'}",
                f"UnitFileState={'enabled' if enabled else 'linked'}",
                f"Result={self.last_result}",
                f"FragmentPath={fragment_path}",
                f"ActiveState={'active' if name in self.active else 'inactive'}",
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

    def __init__(self, home: Path, checkout_units: Path) -> None:
        self.layout = Layout(home)
        self.checkout_units = checkout_units
        self.user_units = home / ".config" / "systemd" / "user"
        self.user_units.mkdir(parents=True)
        self.checkout_units.mkdir(parents=True)
        self.write_unit(SERVICE, "ExecStart=/checkout/bin/backup\n")
        self.write_unit(TIMER, "OnCalendar=weekly\n")
        self.systemctl = FakeSystemctl(self.user_units)
        self.step = UserUnits(self.layout, self.checkout_units, self.systemctl)

    def write_unit(self, name: str, content: str) -> None:
        (self.checkout_units / name).write_text(content)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    return Machine(home, edge_units_directory(Layout(home)))


def test_a_fresh_apply_links_reloads_then_enables_the_timer(machine: Machine) -> None:
    assert machine.step.apply() is Applied.CHANGED

    assert (machine.user_units / SERVICE).readlink() == machine.checkout_units / SERVICE
    assert (machine.user_units / TIMER).readlink() == machine.checkout_units / TIMER
    assert machine.systemctl.changes == [["daemon-reload"], ["enable", "--now", "backup.timer"]]
    assert machine.step.is_verified()


def test_a_dry_run_reports_a_change_and_touches_nothing(machine: Machine) -> None:
    assert machine.step.check() is CheckState.WOULD_CHANGE

    assert list(machine.user_units.iterdir()) == []
    assert machine.systemctl.changes == []


def test_a_second_apply_changes_nothing(machine: Machine) -> None:
    machine.step.apply()
    machine.systemctl.calls.clear()

    assert (machine.step.apply(), machine.step.check()) == (Applied.UNCHANGED, CheckState.UNCHANGED)
    assert machine.systemctl.changes == []


def test_the_loaded_command_and_last_result_of_each_service_are_listed(machine: Machine) -> None:
    machine.step.apply()

    assert machine.step.loaded_command_lines() == LOADED_BACKUP


def test_a_failed_last_run_of_a_service_is_listed_with_its_result(machine: Machine) -> None:
    machine.systemctl.last_result = "exit-code"
    machine.step.apply()

    assert machine.step.loaded_command_lines()[-1] == f"  {SERVICE}: Result=exit-code"


def test_every_command_of_a_unit_with_several_is_kept() -> None:
    shown = "\n".join(
        f"ExecStart={{ path={command} ; argv[]={command} ; ignore_errors=no ; }}"
        for command in ("/bin/first", "/bin/second --flag")
    )

    assert LoadedUnit.parse(shown).commands == ("/bin/first", "/bin/second --flag")


def test_hal_setup_prints_the_loaded_commands_under_the_heading_of_its_step(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def only_systemctl(argv: Sequence[str]) -> str | None:
        return machine.systemctl(argv) if argv[0] == "systemctl" else None

    layout, ports = build_layout_and_ports(machine.layout.home, run=only_systemctl)
    units = next(step for step in setup.pending(layout, ports) if step.name == USER_UNITS_STEP)
    monkeypatch.setattr(setup, "_steps", lambda _layout, _ports: (units,))

    assert setup.setup(layout, ports, apply=True) == 0

    assert capsys.readouterr().out.splitlines() == [f"{USER_UNITS_STEP}: changed", *LOADED_BACKUP]


def test_a_changed_unit_file_is_pending_until_systemd_has_reloaded_it(machine: Machine) -> None:
    machine.step.apply()
    machine.write_unit(SERVICE, "ExecStart=/checkout/bin/hal backup\n")

    assert not machine.step.is_verified()
    assert machine.step.apply() is Applied.CHANGED
    assert machine.systemctl.changes[-1] == ["daemon-reload"]
    assert machine.step.loaded_command_lines() == LOADED_HAL_BACKUP
    assert machine.step.is_verified()


def retire(machine: Machine, name: str, renamed_to: str | None) -> list[list[str]]:
    """Run the step to a settled state, then rename or remove `name` in the checkout and apply."""
    machine.step.apply()
    machine.systemctl.active.add(name)
    machine.systemctl.calls.clear()
    if renamed_to:
        (machine.checkout_units / name).rename(machine.checkout_units / renamed_to)
    else:
        (machine.checkout_units / name).unlink()
    assert not machine.step.is_verified()
    machine.step.apply()
    return machine.systemctl.changes


@pytest.mark.parametrize("name", [TIMER, SERVICE])
def test_a_renamed_unit_is_stopped_unlinked_everywhere_and_replaced_after_one_reload(
    machine: Machine, name: str
) -> None:
    new_name = f"new-{name}"

    changes = retire(machine, name, new_name)

    assert changes[0] == ["stop", name]
    assert changes.count(["daemon-reload"]) == 1
    assert machine.systemctl.active.isdisjoint([name])
    assert not (machine.user_units / name).is_symlink()
    assert not (machine.user_units / TIMERS_WANTS / name).is_symlink()
    assert (machine.user_units / new_name).readlink() == machine.checkout_units / new_name
    assert machine.step.is_verified()


def test_the_repository_units_replace_the_edge_backup_links_and_enable_both_hal_backup_timers(
    tmp_path: Path,
) -> None:
    """#410 sentences 1 and 4: the old edge-backup names are gone and both hal-backup timers run."""
    repository_units = Path(__file__).parents[2] / "systemd"
    home = tmp_path / "home"
    user_units = home / ".config" / "systemd" / "user"
    wants = user_units / TIMERS_WANTS
    wants.mkdir(parents=True)
    old_names = [
        f"edge-backup{stem}.{kind}"
        for stem in ("", "-restore-test")
        for kind in ("service", "timer")
    ]
    for old_name in old_names:
        (user_units / old_name).symlink_to(repository_units / old_name)
        if old_name.endswith(".timer"):
            (wants / old_name).symlink_to(repository_units / old_name)
    systemctl = FakeSystemctl(user_units)
    step = UserUnits(Layout(home), repository_units, systemctl)

    step.apply()

    assert sorted(path.name for path in user_units.rglob("edge-backup*")) == []
    assert {"hal-backup.timer", "hal-backup-restore-test.timer"} <= systemctl.active
    assert step.is_verified()


def test_a_removed_timer_is_stopped_and_has_no_link_left(machine: Machine) -> None:
    changes = retire(machine, TIMER, None)

    assert changes == [["stop", "backup.timer"], ["daemon-reload"]]
    assert machine.systemctl.active.isdisjoint([TIMER])
    assert sorted(path.name for path in machine.user_units.rglob("*") if path.is_symlink()) == [
        SERVICE
    ]
    assert machine.step.is_verified()


def test_a_removed_pair_stops_its_timer_before_its_service(machine: Machine) -> None:
    machine.step.apply()
    machine.systemctl.active.update([SERVICE, TIMER])
    machine.systemctl.calls.clear()
    for name in (SERVICE, TIMER):
        (machine.checkout_units / name).unlink()

    machine.step.apply()

    assert machine.systemctl.changes == [["stop", TIMER], ["stop", SERVICE], ["daemon-reload"]]


def test_a_retired_link_sharing_its_name_with_a_foreign_unit_stops_nothing(
    machine: Machine, tmp_path: Path
) -> None:
    machine.step.apply()
    foreign = tmp_path / "foreign.timer"
    foreign.write_text("OnCalendar=daily\n")
    (machine.checkout_units / TIMER).unlink()
    (machine.user_units / TIMER).unlink()
    (machine.user_units / TIMER).symlink_to(foreign)
    machine.systemctl.active.add(TIMER)
    machine.systemctl.calls.clear()

    with pytest.raises(StepFailed, match=f"{TIMERS_WANTS}/{TIMER}.*does not own that unit"):
        machine.step.apply()

    assert machine.systemctl.changes == []
    assert (machine.user_units / TIMERS_WANTS / TIMER).is_symlink()
    assert not machine.step.is_verified()


def test_a_timer_that_does_not_run_after_enabling_fails_the_step_naming_it(
    machine: Machine,
) -> None:
    machine.systemctl.starts_nothing = True

    with pytest.raises(StepFailed, match=f"{TIMER} enabled but not active"):
        machine.step.apply()


def test_a_retired_unit_that_is_not_running_is_not_stopped(machine: Machine) -> None:
    machine.step.apply()
    (machine.checkout_units / SERVICE).unlink()

    machine.step.apply()

    assert machine.systemctl.changes.count(["stop", SERVICE]) == 0
    assert machine.step.is_verified()


def test_a_link_that_is_already_gone_is_not_an_error(machine: Machine) -> None:
    machine.step.apply()
    (machine.checkout_units / TIMER).unlink()
    (machine.user_units / TIMER).unlink()
    machine.systemctl.active.discard(TIMER)
    machine.systemctl.calls.clear()

    machine.step.apply()

    assert machine.systemctl.changes == [["daemon-reload"]]
    assert not (machine.user_units / TIMERS_WANTS / TIMER).is_symlink()
    assert machine.step.is_verified()


def test_a_link_to_a_file_outside_the_checkout_is_left_alone(
    machine: Machine, tmp_path: Path
) -> None:
    machine.step.apply()
    foreign = machine.user_units / "foreign.service"
    foreign.symlink_to(tmp_path / "gone.service")

    assert machine.step.is_verified()
    assert foreign.is_symlink()


def foreign_link_elsewhere(entry: Path, tmp_path: Path) -> None:
    entry.symlink_to(tmp_path / "another-project.service")


def mask(entry: Path, tmp_path: Path) -> None:
    entry.symlink_to("/dev/null")


def file_of_its_own(entry: Path, tmp_path: Path) -> None:
    entry.write_text("ExecStart=/mine\n")


@pytest.mark.parametrize("occupy", [foreign_link_elsewhere, mask, file_of_its_own])
def test_a_same_name_entry_the_checkout_does_not_own_is_never_replaced(
    machine: Machine, tmp_path: Path, occupy: Callable[[Path, Path], None]
) -> None:
    entry = machine.user_units / SERVICE
    occupy(entry, tmp_path)
    before = (entry.is_symlink(), entry.is_symlink() and entry.readlink())

    for action in (machine.step.apply, machine.step.check):
        with pytest.raises(StepFailed, match=f"{entry}.*does not own it.*you decide"):
            action()

    assert before == (entry.is_symlink(), entry.is_symlink() and entry.readlink())
    assert machine.systemctl.changes == []
    assert not machine.step.is_verified()


def test_a_missing_checkout_unit_directory_is_an_error_never_verified(machine: Machine) -> None:
    shutil.rmtree(machine.checkout_units)

    for action in (machine.step.apply, machine.step.check):
        with pytest.raises(StepFailed, match="is missing"):
            action()
    assert not machine.step.is_verified()


@pytest.mark.parametrize("verb", ["daemon-reload", "enable", "show"])
def test_a_failing_systemctl_fails_the_step_loudly(machine: Machine, verb: str) -> None:
    machine.step.apply()
    shutil.rmtree(machine.user_units / TIMERS_WANTS)
    machine.write_unit(SERVICE, "ExecStart=/checkout/bin/hal backup\n")
    machine.systemctl.failing_verb = verb

    with pytest.raises(StepFailed, match=f"systemctl --user {verb}"):
        machine.step.apply()
    assert not machine.step.is_verified()


def test_a_failing_stop_of_a_retired_unit_fails_the_step_loudly(machine: Machine) -> None:
    machine.step.apply()
    machine.systemctl.active.add(TIMER)
    (machine.checkout_units / TIMER).unlink()
    machine.systemctl.failing_verb = "stop"

    with pytest.raises(StepFailed, match="systemctl --user stop"):
        machine.step.apply()
    assert (machine.user_units / TIMER).is_symlink()


def test_the_dry_run_names_a_failing_systemctl_instead_of_reporting_a_change(
    machine: Machine,
) -> None:
    machine.step.apply()
    machine.systemctl.failing_verb = "show"

    with pytest.raises(StepFailed, match="systemctl --user show"):
        machine.step.check()


def test_hal_setup_lists_the_step_as_pending_until_the_units_are_settled(machine: Machine) -> None:

    def only_systemctl(argv: Sequence[str]) -> str | None:
        return machine.systemctl(argv) if argv[0] == "systemctl" else None

    layout, ports = build_layout_and_ports(machine.layout.home, run=only_systemctl)

    assert [step.name for step in setup.pending(layout, ports)].count(USER_UNITS_STEP) == 1
    machine.step.apply()
    assert [step.name for step in setup.pending(layout, ports)].count(USER_UNITS_STEP) == 0


def test_a_retired_link_whose_name_systemd_loaded_from_elsewhere_stops_nothing(
    machine: Machine, tmp_path: Path
) -> None:
    machine.step.apply()
    (machine.checkout_units / TIMER).unlink()
    (machine.user_units / TIMER).unlink()
    elsewhere = tmp_path / ".local" / "share" / "systemd" / "user" / TIMER
    machine.systemctl.loaded_from[TIMER] = str(elsewhere)
    machine.systemctl.active.add(TIMER)
    machine.systemctl.calls.clear()

    with pytest.raises(StepFailed, match=f"{TIMERS_WANTS}/{TIMER}.*{elsewhere}"):
        machine.step.apply()

    assert machine.systemctl.changes == []
    assert (machine.user_units / TIMERS_WANTS / TIMER).is_symlink()


@pytest.mark.parametrize("fragment_path", ["", None])
def test_a_running_retired_unit_without_a_top_level_entry_is_never_stopped(
    machine: Machine, fragment_path: str | None
) -> None:
    machine.step.apply()
    (machine.checkout_units / TIMER).unlink()
    (machine.user_units / TIMER).unlink()
    if fragment_path is not None:
        machine.systemctl.loaded_from[TIMER] = fragment_path
    machine.systemctl.active.add(TIMER)
    machine.systemctl.calls.clear()

    with pytest.raises(StepFailed, match=f"systemctl --user stop {TIMER}"):
        machine.step.apply()

    assert machine.systemctl.changes == []
    assert (machine.user_units / TIMERS_WANTS / TIMER).is_symlink()


def test_a_unit_that_turns_active_after_the_preflight_is_not_stopped_without_proof(
    machine: Machine,
) -> None:
    machine.step.apply()
    (machine.checkout_units / TIMER).unlink()
    (machine.user_units / TIMER).unlink()
    machine.systemctl.active.discard(TIMER)
    machine.step.refuse_foreign_entries()
    machine.systemctl.active.add(TIMER)
    machine.systemctl.calls.clear()

    dangling = machine.step.dangling_links()

    with pytest.raises(StepFailed, match=f"systemctl --user stop {TIMER}"):
        machine.step.retire(dangling)

    assert machine.systemctl.changes == []
    assert (machine.user_units / TIMERS_WANTS / TIMER).is_symlink()
