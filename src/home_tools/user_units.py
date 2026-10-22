"""The user-units step of `hal setup`: the checkout's `systemd/` units, linked and enabled.

Every unit file of the checkout is linked into the user's unit directory, systemd reloads after a
change, and each timer is enabled and running. A link into the checkout whose file is gone (a
renamed or removed unit) is stopped while systemd still has it loaded, then unlinked, at the top
level and in the `*.wants/` and `*.requires/` directories. A same-name entry the checkout does not
own (a link elsewhere, a mask, a file) is never replaced: the step fails and the person decides.
`systemctl --user` runs through the same command boundary as the other steps' tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from home_tools.system_step import Applied, CheckState, StepFailed
from home_tools.tidy_home import CommandRunner, Layout

SERVICE_SUFFIX = ".service"
TIMER_SUFFIX = ".timer"
UNIT_SUFFIXES = (SERVICE_SUFFIX, TIMER_SUFFIX)
SHOWN_PROPERTIES = "NeedDaemonReload,UnitFileState,ActiveState,ExecStart,Result"
# systemd prints a loaded command as `{ path=… ; argv[]=… ; ignore_errors=… ; … }`.
EXEC_START_ARGV = re.compile(r"argv\[\]=(.*?) ;")
DEPENDENCY_DIRECTORY_SUFFIXES = (".wants", ".requires")
STOPPED_STATES = ("inactive", "failed", "")


@dataclass(frozen=True)
class LoadedUnit:
    """What systemd reports about one unit it knows."""

    needs_reload: bool
    enabled: bool
    active_state: str
    commands: tuple[str, ...]
    last_result: str

    @classmethod
    def parse(cls, shown: str) -> LoadedUnit:
        lines = [line.partition("=") for line in shown.splitlines()]
        properties = {key: value for key, _, value in lines}
        return cls(
            needs_reload=properties.get("NeedDaemonReload") == "yes",
            enabled=properties.get("UnitFileState") == "enabled",
            active_state=properties.get("ActiveState", ""),
            commands=tuple(
                command
                for key, _, value in lines
                if key == "ExecStart"
                for command in EXEC_START_ARGV.findall(value)
            ),
            last_result=properties.get("Result", ""),
        )

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def stopped(self) -> bool:
        return self.active_state in STOPPED_STATES


class UserUnits:
    def __init__(self, layout: Layout, checkout_units: Path, run: CommandRunner) -> None:
        self.checkout_units = checkout_units
        self.user_units = layout.home / ".config" / "systemd" / "user"
        self.run = run

    def unit_files(self) -> list[Path]:
        if not self.checkout_units.is_dir():
            raise StepFailed(f"{self.checkout_units} is missing, so there are no units to link")
        return sorted(
            path for path in self.checkout_units.iterdir() if path.suffix in UNIT_SUFFIXES
        )

    def is_verified(self) -> bool:
        """Whether `verify` holds; a failure to ask systemd or to read the checkout counts as no."""
        try:
            return self.verify()
        except StepFailed:
            return False

    def verify(self) -> bool:
        """Linked, nothing to reload, every timer enabled and active, no dangling link.

        Raises when it cannot tell: the checkout's units are missing, a same-name entry is not
        the checkout's, or systemctl fails.
        """
        self.refuse_foreign_entries()
        if self.dangling_links() or not all(self.is_linked(path) for path in self.unit_files()):
            return False
        return all(self.is_settled(path) for path in self.unit_files())

    def check(self) -> CheckState:
        return CheckState.UNCHANGED if self.verify() else CheckState.WOULD_CHANGE

    def apply(self) -> Applied:
        self.refuse_foreign_entries()
        dangling = self.dangling_links()
        self.retire(dangling)
        unlinked = [path for path in self.unit_files() if not self.is_linked(path)]
        for path in unlinked:
            self.link(path)
        reloaded = bool(dangling or unlinked) or any(
            self.show(path.name).needs_reload for path in self.unit_files()
        )
        if reloaded:
            self.systemctl("daemon-reload")
        started = self.start_idle_timers()
        return Applied.CHANGED if reloaded or started else Applied.UNCHANGED

    def start_idle_timers(self) -> list[Path]:
        idle = [path for path in self.timers() if not self.is_running_enabled(path)]
        for path in idle:
            self.systemctl("enable", "--now", path.name)
        not_running = [path.name for path in idle if not self.show(path.name).active]
        if not_running:
            raise StepFailed(
                f"{', '.join(not_running)} enabled but not active; see `systemctl --user status`"
            )
        return idle

    def loaded_command_lines(self) -> list[str]:
        """What systemd has loaded for each service and how its last run ended, so a missed reload
        or a failed timer run shows."""
        lines = []
        for path in self.unit_files():
            if path.suffix == SERVICE_SUFFIX:
                unit = self.show(path.name)
                lines += [f"  {path.name}: ExecStart={command}" for command in unit.commands]
                lines.append(f"  {path.name}: Result={unit.last_result}")
        return lines

    def timers(self) -> list[Path]:
        return [path for path in self.unit_files() if path.suffix == TIMER_SUFFIX]

    def is_linked(self, path: Path) -> bool:
        link = self.user_units / path.name
        return link.is_symlink() and link.readlink() == path

    def is_running_enabled(self, path: Path) -> bool:
        unit = self.show(path.name)
        return unit.enabled and unit.active

    def is_settled(self, path: Path) -> bool:
        if self.show(path.name).needs_reload:
            return False
        return self.is_running_enabled(path) if path.suffix == TIMER_SUFFIX else True

    def refuse_foreign_entries(self) -> None:
        """A same-name entry that is not this checkout's link is the person's: never replace it,
        and never stop a unit a retired link of this checkout shares its name with."""
        for path in self.unit_files():
            entry = self.user_units / path.name
            if (entry.is_symlink() or entry.exists()) and not self.is_linked(path):
                kind = (
                    f"a link to {entry.readlink()}" if entry.is_symlink() else "a file of its own"
                )
                raise StepFailed(
                    f"{entry} is {kind}, not a link to {path}; this checkout does not own it,"
                    " so you decide: move it away or delete it, then rerun"
                )
        retiring = set(self.dangling_links())
        for link in retiring:
            entry = self.user_units / link.name
            if (entry.is_symlink() or entry.exists()) and entry not in retiring:
                raise StepFailed(
                    f"{link} is a retired link of this checkout, but {entry} is not;"
                    " this checkout does not own that unit, so you decide: move it away"
                    " or delete the retired link, then rerun"
                )

    def link_directories(self) -> list[Path]:
        if not self.user_units.is_dir():
            return []
        dependency_directories = sorted(
            directory
            for directory in self.user_units.iterdir()
            if directory.suffix in DEPENDENCY_DIRECTORY_SUFFIXES
            and directory.is_dir()
            and not directory.is_symlink()
        )
        return [self.user_units, *dependency_directories]

    def dangling_links(self) -> list[Path]:
        """Links into the checkout's `systemd/` whose file is gone, wherever systemd keeps them."""
        return sorted(
            link
            for directory in self.link_directories()
            for link in directory.iterdir()
            if link.is_symlink()
            and link.readlink().parent == self.checkout_units
            and not link.exists()
        )

    def retire(self, links: list[Path]) -> None:
        """systemd refuses to disable a unit whose file is gone, so stop it while it is still
        loaded and unlink its links; the reload after the last link drops it. Timers stop first, so
        none starts its service again meanwhile."""
        for name in sorted({link.name for link in links}, key=self.timers_first):
            if not self.show(name).stopped:
                self.systemctl("stop", name)
        for link in links:
            link.unlink(missing_ok=True)

    @staticmethod
    def timers_first(name: str) -> tuple[bool, str]:
        return not name.endswith(TIMER_SUFFIX), name

    def link(self, path: Path) -> None:
        self.user_units.mkdir(parents=True, exist_ok=True)
        (self.user_units / path.name).symlink_to(path)

    def show(self, name: str) -> LoadedUnit:
        return LoadedUnit.parse(self.systemctl("show", name, f"--property={SHOWN_PROPERTIES}"))

    def systemctl(self, *arguments: str) -> str:
        output = self.run(["systemctl", "--user", *arguments])
        if output is None:
            raise StepFailed(f"`systemctl --user {' '.join(arguments)}` failed")
        return output
