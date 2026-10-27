"""What `hal rename` prints: a row for each step of the plan and of the apply, a row for each
finding, and the verdict or error that ends the run. The words are English; paths show `~` for
the home directory."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol

from hal_output import Glyph, Noun, Row, Verdict, write_error, write_row, write_verdict


class Event(StrEnum):
    """The event column: what a step will do, what it did, and what a finding is."""

    MOVE = "move"
    MOVED = "moved"
    REPAIR = "repair"
    REPAIRED = "repaired"
    SET_URL = "set url"
    URL_SET = "url set"
    UV_SYNC = "uv sync"
    UV_SYNCED = "uv synced"
    CHECK = "check"


class StepKind(Enum):
    """A step's row subject, its event while planned and its event once done."""

    MOVE_CLONE = ("clone", Event.MOVE, Event.MOVED)
    MOVE_WORKTREES = ("worktrees", Event.MOVE, Event.MOVED)
    REPAIR_WORKTREES = ("worktrees", Event.REPAIR, Event.REPAIRED)
    SET_URL = ("origin", Event.SET_URL, Event.URL_SET)
    MOVE_MEMORY = ("memory", Event.MOVE, Event.MOVED)
    SYNC = ("venv", Event.UV_SYNC, Event.UV_SYNCED)


@dataclass(frozen=True)
class StepRow:
    """The row of one step."""

    kind: StepKind
    fact: str

    @classmethod
    def of_move(cls, kind: StepKind, source: Path, target: Path) -> "StepRow":
        if kind is StepKind.MOVE_MEMORY:
            return cls(kind, "Claude project memory of the clone")
        return cls(kind, f"{source} → {target}")

    @classmethod
    def of_repair(cls, worktrees: Path, count: int) -> "StepRow":
        return cls(StepKind.REPAIR_WORKTREES, f"{count} in {worktrees}")

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


class Label(Noun):
    STEP = "step"
    FINDING = "finding"


def _home_as_tilde(text: str, home: Path) -> str:
    return re.sub(rf"(?<![\w/]){re.escape(str(home))}(?![\w.-])", "~", text)


def write_done_row(row: StepRow, home: Path) -> None:
    subject, _, done = row.kind.value
    write_row(Row(Glyph.DONE, subject, done, _home_as_tilde(row.fact, home)))


def write_findings(findings: Sequence[Finding], home: Path) -> None:
    for finding in findings:
        sentence = _home_as_tilde(finding.sentence, home)
        write_row(Row(Glyph.NEEDS_YOU, finding.subject, Event.CHECK, sentence))


def write_plan(
    steps: Sequence[HasRow], findings: Sequence[Finding], old: str, new: str, home: Path
) -> None:
    """The rows, the findings, then the verdict and the command that applies the plan."""
    for row in (step.row for step in steps):
        subject, planned, _ = row.kind.value
        fact = _home_as_tilde(row.fact, home)
        write_row(Row(Glyph.CHANGE, subject, planned, fact))
    write_findings(findings, home)
    next_step = f"hal rename {old} {new} --apply"
    write_verdict(Verdict(Glyph.UNCHANGED, "plan only, nothing changed", (), next_step=next_step))


def write_nothing_to_do(old: str) -> None:
    write_verdict(Verdict(Glyph.UNCHANGED, f"nothing to do, no clone of {old} to move", ()))


def write_renamed(old: str, new: str, steps: int, findings: int) -> None:
    glyph = Glyph.NEEDS_YOU if findings else Glyph.DONE
    counts = ((steps, Label.STEP), (findings, Label.FINDING))
    write_verdict(Verdict(glyph, f"renamed {old} → {new}", counts))


def write_refusals(reasons: Sequence[str]) -> None:
    for reason in reasons:
        write_error(f"cannot rename, {reason} · nothing changed")
