"""The one owner of how this repository's tools print to a terminal: text from outside made safe
to show, and the form of a row, a verdict and an error. It imports nothing else of this
repository."""

import sys
from dataclasses import dataclass
from enum import Enum, StrEnum

SUBJECT_WIDTH = 14
EVENT_WIDTH = 13


class Glyph(Enum):
    DONE = "✓"
    FAILED = "✗"
    NEEDS_YOU = "!"
    UNCHANGED = "·"
    CHANGE = "~"


@dataclass(frozen=True)
class Row:
    """One thing a command did or found. The caller owns its event enum; `fact` is one sentence."""

    glyph: Glyph
    subject: str
    event: StrEnum
    fact: str = ""


@dataclass(frozen=True)
class Verdict:
    """The last line of a run: the caller computes the glyph, the summary and the count of each
    of its labels, in the order to show them; the renderer leaves out a zero count. `hint` is the
    command to run next."""

    glyph: Glyph
    summary: str
    counts: tuple[tuple[int, StrEnum], ...]
    hint: str = ""


def printable(text: str) -> str:
    """`text` with every character a terminal would act on escaped (a newline as `\\n`, ESC as
    `\\x1b`), so text from outside, a path or a command's line, stays one line of plain text."""
    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode()
        for character in text
    )


def write_row(row: Row) -> None:
    """The row on stdout; text from outside is escaped before it is padded, so columns hold."""
    subject, event, fact = printable(row.subject), printable(str(row.event)), printable(row.fact)
    print(
        f"  {row.glyph.value} {subject:<{SUBJECT_WIDTH}}  {event:<{EVENT_WIDTH}}  {fact}".rstrip()
    )


def write_verdict(verdict: Verdict) -> None:
    """The last line of a command on stdout: the summary, its non-zero counts, then the hint."""
    counted = ", ".join(
        f"{number} {printable(str(label))}" for number, label in verdict.counts if number
    )
    hint = [printable(verdict.hint)] if verdict.hint else []
    print(f"{verdict.glyph.value} {' · '.join([printable(verdict.summary), counted, *hint])}")


def write_error(message: str, next_step: str | None = None) -> None:
    """`error: <message>` and, when there is a way on, `next: <command>` on stderr."""
    print(f"error: {printable(message)}", file=sys.stderr)
    if next_step:
        print(f"next: {printable(next_step)}", file=sys.stderr)
