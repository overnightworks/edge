"""The user-units step of `hal setup`: the checkout's `systemd/` units, linked and enabled.

Every unit file of the checkout is linked into the user's unit directory, systemd reloads after a
change, and each timer is enabled and running. A link into the checkout whose file is gone (a
renamed or removed unit) is disabled and removed. `systemctl --user` runs through the same
command boundary as the other steps' tools.
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


@dataclass(frozen=True)
class LoadedUnit:
    """What systemd reports about one unit it knows."""

    needs_reload: bool
    enabled: bool
    active: bool
    commands: tuple[str, ...]
    last_result: str

    @classmethod
    def parse(cls, shown: str) -> LoadedUnit:
        properties = dict(line.partition("=")[::2] for line in shown.splitlines())
        return cls(
            needs_reload=properties.get("NeedDaemonReload") == "yes",
            enabled=properties.get("UnitFileState") == "enabled",
            active=properties.get("ActiveState") == "active",
            commands=tuple(EXEC_START_ARGV.findall(properties.get("ExecStart", ""))),
            last_result=properties.get("Result", ""),
        )


class UserUnits:
    def __init__(self, layout: Layout, run: CommandRunner) -> None:
        self.checkout_units = layout.edge_tunnel_dir.parent / "systemd"
        self.user_units = layout.home / ".config" / "systemd" / "user"
        self.run = run

    def unit_files(self) -> list[Path]:
        if not self.checkout_units.is_dir():
            return []
        return sorted(
            path for path in self.checkout_units.iterdir() if path.suffix in UNIT_SUFFIXES
        )

    def is_verified(self) -> bool:
        """Linked, nothing to reload, every timer enabled and active, no dangling link."""
        if self.dangling_links() or not all(self.is_linked(path) for path in self.unit_files()):
            return False
        try:
            return all(self.is_settled(path) for path in self.unit_files())
        except StepFailed:
            return False

    def check(self) -> CheckState:
        return CheckState.UNCHANGED if self.is_verified() else CheckState.WOULD_CHANGE

    def apply(self) -> Applied:
        dangling = self.dangling_links()
        unlinked = [path for path in self.unit_files() if not self.is_linked(path)]
        for link in dangling:
            self.remove_dangling_link(link)
        for path in unlinked:
            self.link(path)
        if dangling or unlinked or any(self.show(path).needs_reload for path in self.unit_files()):
            self.systemctl("daemon-reload")
        idle = [path for path in self.timers() if not self.is_running_enabled(path)]
        for path in idle:
            self.systemctl("enable", "--now", path.name)
        self.print_loaded_commands()
        return Applied.CHANGED if dangling or unlinked or idle else Applied.UNCHANGED

    def print_loaded_commands(self) -> None:
        """What systemd has loaded for each service and how its last run ended, so a missed reload
        or a failed timer run shows."""
        for path in self.unit_files():
            if path.suffix == SERVICE_SUFFIX:
                unit = self.show(path)
                for command in unit.commands:
                    print(f"  {path.name}: ExecStart={command}")
                print(f"  {path.name}: Result={unit.last_result}")

    def timers(self) -> list[Path]:
        return [path for path in self.unit_files() if path.suffix == TIMER_SUFFIX]

    def is_linked(self, path: Path) -> bool:
        link = self.user_units / path.name
        return link.is_symlink() and link.readlink() == path

    def is_running_enabled(self, path: Path) -> bool:
        unit = self.show(path)
        return unit.enabled and unit.active

    def is_settled(self, path: Path) -> bool:
        if self.show(path).needs_reload:
            return False
        return self.is_running_enabled(path) if path.suffix == TIMER_SUFFIX else True

    def dangling_links(self) -> list[Path]:
        """Links into the checkout's `systemd/` whose file is gone."""
        if not self.user_units.is_dir():
            return []
        return sorted(
            link
            for link in self.user_units.iterdir()
            if link.is_symlink()
            and link.readlink().parent == self.checkout_units
            and not link.exists()
        )

    def remove_dangling_link(self, link: Path) -> None:
        self.systemctl("disable", link.name)
        link.unlink()

    def link(self, path: Path) -> None:
        link = self.user_units / path.name
        if link.exists() and not link.is_symlink():
            raise StepFailed(
                f"{link} is a file of its own, not a link to {path}; move it away, then rerun"
            )
        self.user_units.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(path)

    def show(self, path: Path) -> LoadedUnit:
        return LoadedUnit.parse(self.systemctl("show", path.name, f"--property={SHOWN_PROPERTIES}"))

    def systemctl(self, *arguments: str) -> str:
        output = self.run(["systemctl", "--user", *arguments])
        if output is None:
            raise StepFailed(f"`systemctl --user {' '.join(arguments)}` failed")
        return output
