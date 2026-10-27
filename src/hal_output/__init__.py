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


def write_verdict(glyph: Glyph, summary: str, detail: str) -> None:
    """The last line of a command on stdout: the caller's verdict, then its detail after a `·`."""
    print(f"{glyph.value} {printable(summary)} · {printable(detail)}")


def write_error(message: str, next_step: str | None = None) -> None:
    """`error: <message>` and, when there is a way on, `next: <command>` on stderr."""
    print(f"error: {printable(message)}", file=sys.stderr)
    if next_step:
        print(f"next: {printable(next_step)}", file=sys.stderr)
