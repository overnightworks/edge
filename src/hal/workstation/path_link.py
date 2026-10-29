"""The path step of `hal setup`: `~/.local/bin/hal` links to the `bin/hal` of the checkout hal runs from.

A missing link is created, a link into another hal checkout is re-pointed atomically, and anything else
at that name (a file, a directory, a link elsewhere) is the person's: the step fails and names it.
A hal that runs from an installed wheel has no checkout and leaves the entry alone.
"""

from __future__ import annotations

import os
import secrets
import tomllib
from pathlib import Path

from hal.output import printable
from hal.workstation.layout import Layout
from hal.workstation.system_step import Applied, CheckState, StepFailed

DISTRIBUTION_NAME = "overnightworks-hal"
LAUNCHER = Path("bin") / "hal"


class PathLink:
    def __init__(self, layout: Layout, checkout: Path | None) -> None:
        self.home = layout.home
        self.clones_directory = layout.git
        self.entry = layout.local_dir / "bin" / "hal"
        self.target = None if checkout is None else checkout / LAUNCHER

    def check(self) -> CheckState:
        return CheckState.WOULD_CHANGE if self.is_open() else CheckState.UNCHANGED

    def fact(self) -> str:
        return (
            "" if self.target is None else f"{self.shown(self.entry)} → {self.shown(self.target)}"
        )

    def apply(self) -> Applied:
        target = self.target
        if target is None or not self.is_open():
            return Applied.UNCHANGED
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        beside = self.entry.with_name(f".{self.entry.name}.{secrets.token_hex(4)}.new")
        beside.symlink_to(target)
        try:
            beside.replace(self.entry)
        except OSError:
            beside.unlink()
            raise
        return Applied.CHANGED

    def is_open(self) -> bool:
        """Whether the link must be created or re-pointed; raises when the entry is not the person's to replace by this step."""
        if self.target is None:
            return False
        if not self.entry.is_symlink():
            if self.entry.exists():
                raise StepFailed(self.refusal())
            return True
        current = self.current_target()
        if current == self.directory_resolved(self.target):
            return False
        if not self.is_hal_checkout_launcher(current):
            raise StepFailed(self.refusal())
        return True

    def current_target(self) -> Path:
        return self.directory_resolved(self.entry.parent / self.entry.readlink())

    @staticmethod
    def directory_resolved(launcher: Path) -> Path:
        """The launcher with its directory resolved by the filesystem (a dot-dot after a symlink), and the launcher itself left unfollowed."""
        return Path(os.path.realpath(launcher.parent)) / launcher.name

    def is_hal_checkout_launcher(self, launcher: Path) -> bool:
        """A `<root>/bin/hal` whose root names this project, or is gone and sits directly under `~/git`, where clones move within."""
        root = launcher.parent.parent
        if launcher.parent.name != LAUNCHER.parent.name or launcher.name != LAUNCHER.name:
            return False
        try:
            with (root / "pyproject.toml").open("rb") as pyproject:
                project = tomllib.load(pyproject).get("project")
                return isinstance(project, dict) and project.get("name") == DISTRIBUTION_NAME
        except FileNotFoundError:
            return not root.exists() and root.parent == Path(os.path.realpath(self.clones_directory))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return False

    def refusal(self) -> str:
        return f"{self.shown(self.entry)} is not a hal link · move or delete it, then rerun"

    def shown(self, path: Path) -> str:
        """The path with the home as `~`, and what a terminal would act on escaped."""
        return printable(
            f"~/{path.relative_to(self.home)}" if path.is_relative_to(self.home) else str(path)
        )
