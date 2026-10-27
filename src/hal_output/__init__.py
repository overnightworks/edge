"""The one owner of how this repository's tools print to a terminal: text from outside made safe
to show, and the form of a row, a verdict, an error and the indented detail lines under a row. It
imports nothing else of this repository."""

import os
import sys
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TextIO

DETAIL_INDENT = "    "
SUBJECT_WIDTH = 14
EVENT_WIDTH = 14

_RESET = "\x1b[0m"
_DIM = "2"
_BOLD_RED = "1;31"
_CYAN = "36"
"""The 16 base ANSI colours, so a terminal's own theme decides the shades."""


def _colour_on(stream: TextIO) -> bool:
    """Colour only for a person: the stream is a terminal, `NO_COLOR` is empty or unset, and the
    terminal is not `dumb`."""
    try:
        is_terminal = stream.isatty()
    except (AttributeError, ValueError):  # no stdout when daemonised, or a closed stream
        return False
    return is_terminal and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _styled(text: str, style: str, stream: TextIO) -> str:
    return f"\x1b[{style}m{text}{_RESET}" if _colour_on(stream) else text


class Glyph(Enum):
    DONE = "✓"
    FAILED = "✗"
    NEEDS_YOU = "!"
    UNCHANGED = "·"
    CHANGE = "~"
    ANNOUNCE = "\u203a"

    @property
    def verdict_style(self) -> str:
        return _VERDICT_STYLES[self]


_ROW_STYLES = {
    Glyph.DONE: "32",
    Glyph.FAILED: "31",
    Glyph.NEEDS_YOU: "33",
    Glyph.CHANGE: "34",
    Glyph.ANNOUNCE: "36",
}
_VERDICT_STYLES = {
    Glyph.DONE: "1",
    Glyph.FAILED: _BOLD_RED,
    Glyph.NEEDS_YOU: "1;33",
    Glyph.UNCHANGED: _DIM,
    Glyph.CHANGE: "1;34",
}


class Tone(Enum):
    """How a detail line looks on a terminal: unstyled, dim, or in the needs-you row's yellow."""

    PLAIN = None
    DIM = _DIM
    WARNING = _ROW_STYLES[Glyph.NEEDS_YOU]


class Noun(StrEnum):
    """An event or label that names something to count: its value is the singular phrase, and a
    count other than one shows it with an `s` after its first word (`action`, `area checked`)."""


@dataclass(frozen=True)
class Row:
    """One thing a command did or found. The caller owns its event enum; `fact` is one sentence;
    `count` is shown before the event, which takes the plural when it is a `Noun`."""

    glyph: Glyph
    subject: str
    event: StrEnum
    fact: str = ""
    count: int | None = None


@dataclass(frozen=True)
class Verdict:
    """The last line of a run: the caller computes the glyph, the summary and the count of each
    of its labels, in the order to show them; the renderer leaves out a zero count. `hint` is the
    command to run next; `elapsed_seconds` is the run time, shown after the counts. Without a
    summary the counts are the headline. `details` are plain phrases after the counts and
    `next_step` is a `next:` line under the verdict."""

    glyph: Glyph
    summary: str
    counts: tuple[tuple[int, StrEnum], ...]
    hint: str = ""
    elapsed_seconds: float | None = None
    details: tuple[str, ...] = ()
    next_step: str = ""


SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


def format_duration(seconds: float) -> str:
    """Whole seconds rounded down: `<1s`, `47s`, `1m05s`, `1h02m`."""
    whole = int(seconds)
    if whole < 1:
        return "<1s"
    if whole < SECONDS_PER_MINUTE:
        return f"{whole}s"
    if whole < SECONDS_PER_HOUR:
        return f"{whole // SECONDS_PER_MINUTE}m{whole % SECONDS_PER_MINUTE:02d}s"
    minutes = whole % SECONDS_PER_HOUR // SECONDS_PER_MINUTE
    return f"{whole // SECONDS_PER_HOUR}h{minutes:02d}m"


def printable(text: str) -> str:
    """`text` with every character a terminal would act on escaped (a newline as `\\n`, ESC as
    `\\x1b`), so text from outside, a path or a command's line, stays one line of plain text."""
    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode()
        for character in text
    )


def _counted(number: int, label: StrEnum) -> str:
    text = printable(str(label))
    if isinstance(label, Noun) and number != 1:
        first, separator, rest = text.partition(" ")
        text = f"{first}s{separator}{rest}"
    return f"{number} {text}"


def write_row(row: Row) -> None:
    """The row on stdout; text from outside is escaped before it is padded, so columns hold.
    On a terminal only the glyph is coloured and the fact dim; a `·` row is dim as a whole."""
    stdout = sys.stdout
    subject = printable(row.subject)
    event = printable(str(row.event)) if row.count is None else _counted(row.count, row.event)
    fact = printable(row.fact).rstrip()
    columns = f"{subject:<{SUBJECT_WIDTH}}  {event:<{EVENT_WIDTH}}"
    if row.glyph is Glyph.UNCHANGED:
        line = f"  {row.glyph.value} {columns} {fact}".rstrip()
        print(_styled(line, _DIM, stdout))
        return
    glyph = _styled(row.glyph.value, _ROW_STYLES[row.glyph], stdout)
    line = f"  {glyph} {columns}"
    print(f"{line} {_styled(fact, _DIM, stdout)}" if fact else line.rstrip())


def write_verdict(verdict: Verdict) -> None:
    """The last line of a command on stdout: the summary, its non-zero counts, the run time, then
    the hint. On a terminal the glyph and summary are bold in the glyph's colour, the counts and
    the run time dim; a `next:` line follows when the verdict has a next step."""
    stdout = sys.stdout
    counted = ", ".join(_counted(number, label) for number, label in verdict.counts if number)
    hint = [printable(verdict.hint)] if verdict.hint else []
    headline_text = printable(verdict.summary) or counted
    headline = _styled(
        f"{verdict.glyph.value} {headline_text}", verdict.glyph.verdict_style, stdout
    )
    counts = [_styled(counted, _DIM, stdout)] if counted and verdict.summary else []
    details = [_styled(printable(detail), _DIM, stdout) for detail in verdict.details]
    run_time = (
        [_styled(format_duration(verdict.elapsed_seconds), _DIM, stdout)]
        if verdict.elapsed_seconds is not None
        else []
    )
    print(" · ".join([headline, *counts, *details, *run_time, *hint]))
    if verdict.next_step:
        print(f"{_styled('next:', _CYAN, stdout)} {printable(verdict.next_step)}")


def write_error(message: str, next_step: str | None = None) -> None:
    """`error: <message>` and, when there is a way on, `next: <command>` on stderr; on a terminal
    the labels are bold red and cyan."""
    stderr = sys.stderr
    print(f"{_styled('error:', _BOLD_RED, stderr)} {printable(message)}", file=stderr)
    if next_step:
        print(f"{_styled('next:', _CYAN, stderr)} {printable(next_step)}", file=stderr)


def write_detail(*lines: str, tone: Tone = Tone.PLAIN) -> None:
    """Each line on stdout under a row, indented and escaped; on a terminal it takes the tone's
    style."""
    stdout = sys.stdout
    for line in lines:
        text = f"{DETAIL_INDENT}{printable(line)}"
        print(text if tone.value is None else _styled(text, tone.value, stdout))
