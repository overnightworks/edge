"""The path step of `hal setup`: `~/.local/bin/hal` links to the `bin/hal` of the checkout hal runs from.

A missing link is created, a link into another hal checkout is re-pointed atomically, and anything else
at that name (a file, a directory, a link elsewhere) is the person's: the step fails and names it.
A hal that runs from an installed wheel has no checkout and leaves the entry alone.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from hal.output import printable
from hal.workstation.layout import Layout
from hal.workstation.system_step import Applied, CheckState, StepFailed

DISTRIBUTION_NAME = "overnightworks-hal"
LAUNCHER = Path("bin") / "hal"
DECISION = "you decide: move it away or delete it, then rerun"


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
        beside = self.entry.with_name(f".{self.entry.name}.new")
        beside.unlink(missing_ok=True)
        beside.symlink_to(target)
        beside.replace(self.entry)
        return Applied.CHANGED

    def is_open(self) -> bool:
        """Whether the link must be created or re-pointed; raises when the entry is not the person's to replace by this step."""
        if self.target is None:
            return False
        if not self.entry.is_symlink():
            if self.entry.exists():
                raise StepFailed(
                    self.refusal(
                        "a directory, not a link"
                        if self.entry.is_dir()
                        else "a file of its own, not a link"
                    )
                )
            return True
        current = self.current_target()
        if current == self.target:
            return False
        if not self.is_hal_checkout_launcher(current):
            raise StepFailed(
                self.refusal(f"a link to {printable(str(current))}, not a hal checkout")
            )
        return True

    def current_target(self) -> Path:
        return Path(os.path.normpath(self.entry.parent / self.entry.readlink()))

    def is_hal_checkout_launcher(self, launcher: Path) -> bool:
        """A `<root>/bin/hal` whose root names this project, or is gone and sits directly under `~/git`, where clones move within."""
        root = launcher.parent.parent
        if launcher.parent.name != LAUNCHER.parent.name or launcher.name != LAUNCHER.name:
            return False
        try:
            with (root / "pyproject.toml").open("rb") as pyproject:
                return tomllib.load(pyproject).get("project", {}).get("name") == DISTRIBUTION_NAME
        except FileNotFoundError:
            return not root.exists() and root.parent == self.clones_directory
        except (OSError, tomllib.TOMLDecodeError):
            return False

    def refusal(self, kind: str) -> str:
        return f"{self.shown(self.entry)} is {kind}; {DECISION}"

    def shown(self, path: Path) -> str:
        """The path with the home as `~`, and what a terminal would act on escaped."""
        return printable(
            f"~/{path.relative_to(self.home)}" if path.is_relative_to(self.home) else str(path)
        )
