"""What `hal rename` prints: a row for each step of the plan and of the apply, a row for each
finding, and the verdict or error that ends the run. The words are English; paths show `~` for
the home directory."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol

from hal_output import Glyph, Row, Verdict, write_error, write_row, write_verdict


class StepKind(Enum):
    """A step's row subject, its event while planned and its event once done."""

    MOVE_CLONE = ("clone", "move", "moved")
    MOVE_WORKTREES = ("worktrees", "move", "moved")
    REPAIR_WORKTREES = ("worktrees", "repair", "repaired")
    SET_URL = ("origin", "set url", "url set")
    MOVE_MEMORY = ("memory", "move", "moved")
    SYNC = ("venv", "uv sync", "uv synced")


@dataclass(frozen=True)
class StepRow:
    """The row of one step; `count` is the number of things a repair covers."""

    kind: StepKind
    fact: str
    count: int = 0

    @classmethod
    def of_move(cls, kind: StepKind, source: Path, target: Path) -> "StepRow":
        if kind is StepKind.MOVE_MEMORY:
            return cls(kind, "Claude project memory of the clone")
        return cls(kind, f"{source} → {target}")

    @classmethod
    def of_repair(cls, worktrees: Path, count: int) -> "StepRow":
        return cls(StepKind.REPAIR_WORKTREES, f"in {worktrees}", count)

    @classmethod
    def of_origin(cls, url: str, deploy_checkout: Path | None) -> "StepRow":
        fact = f"{url} · deploy checkout {deploy_checkout}" if deploy_checkout else url
        return cls(StepKind.SET_URL, fact)


@dataclass(frozen=True)
class Finding:
    """Something hal reports and never rewrites; `subject` names the thing it found."""

    subject: str
    sentence: str


class HasRow(Protocol):
    row: StepRow


class Label(StrEnum):
    STEPS = "steps"


def _event(word: str, count: int) -> StrEnum:
    """The event column is a closed word plus, for a repair, how many worktrees it covers."""
    text = f"{word} {count}" if count else word
    return StrEnum("Event", {"EVENT": text}).EVENT


def _home_as_tilde(text: str, home: Path) -> str:
    return re.sub(rf"(?<![\w/]){re.escape(str(home))}(?![\w.-])", "~", text)


def write_done_row(row: StepRow, home: Path) -> None:
    subject, _, done = row.kind.value
    write_row(Row(Glyph.DONE, subject, _event(done, row.count), _home_as_tilde(row.fact, home)))


def write_findings(findings: Sequence[Finding], home: Path) -> None:
    for finding in findings:
        sentence = _home_as_tilde(finding.sentence, home)
        write_row(Row(Glyph.NEEDS_YOU, finding.subject, _event("check", 0), sentence))


def write_plan(
    steps: Sequence[HasRow], findings: Sequence[Finding], old: str, new: str, home: Path
) -> None:
    """The rows, the findings, then the verdict and the command that applies the plan."""
    for row in (step.row for step in steps):
        subject, planned, _ = row.kind.value
        fact = _home_as_tilde(row.fact, home)
        write_row(Row(Glyph.CHANGE, subject, _event(planned, row.count), fact))
    write_findings(findings, home)
    write_verdict(Verdict(Glyph.UNCHANGED, "plan only, nothing changed", ()))
    print(f"next: hal rename {old} {new} --apply")


def write_nothing_to_do(old: str) -> None:
    write_verdict(Verdict(Glyph.UNCHANGED, f"nothing to do, no clone of {old} to move", ()))


def write_renamed(old: str, new: str, steps: int) -> None:
    write_verdict(Verdict(Glyph.DONE, f"renamed {old} → {new}", ((steps, Label.STEPS),)))


def write_refusals(reasons: Sequence[str]) -> None:
    for reason in reasons:
        write_error(f"cannot rename, {reason} · nothing changed")
