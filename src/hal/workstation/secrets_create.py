"""The `create` table of a manifest entry: the CLI that makes a tool login itself.

The entry derives its renewal text from it instead of declaring `recreate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hal.workstation.manifest_fields import fail, string, strings

CREATE_KEYS = frozenset({"run", "home", "label"})


@dataclass(frozen=True)
class CreateCommand:
    """The CLI that makes a secret itself: its argv, the variables hal points at the entry's folder, and how the operator knows the account."""

    run: tuple[str, ...]
    home: tuple[str, ...]
    label: str

    def renewal(self, folder: str) -> str:
        """The command that makes the login again, as the operator types it, its variables pointing at `folder`."""
        variables = (f"{name}={folder}" for name in self.home)
        return " ".join((*variables, *self.run))


def parse_create_table(path: Path, where: str, table: object) -> CreateCommand:
    """`run` and `label`, optional `home`."""
    if not isinstance(table, dict) or {"run", "label"} - set(table) or set(table) - CREATE_KEYS:
        raise fail(path, where, "create must be a table with run and label, and home when the CLI takes its folder from variables")
    run = strings(path, where, table, "run")
    if not run:
        raise fail(path, where, "create.run must name the CLI")
    return CreateCommand(run, strings(path, where, table, "home") if "home" in table else (), string(path, where, table, "label"))
