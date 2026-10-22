"""The release tags of an app: their grammar, their order and what is skipped.

A release is a tag `vYYYY.MM.DD.N`: a real calendar date, month and day with two digits, and a
number N from 1 without a leading zero. The newest is the greatest by date, then by N as a
number. Every other `v*` tag, and every release not on the default branch of origin, is skipped
and named, so a tag cut in another form is never silently ignored. Pure: the callers ask git.
"""

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date

from edge import compose

# Not `\d` and not `$`: those accept other scripts' digits and a trailing newline.
_RELEASE = re.compile(r"v([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([1-9][0-9]*)")
GRAMMAR = "vYYYY.MM.DD.N"


@dataclass(frozen=True, order=True)
class Release:
    day: date
    number: int

    @property
    def tag(self) -> str:
        return f"v{self.day.year:04d}.{self.day.month:02d}.{self.day.day:02d}.{self.number}"


@dataclass(frozen=True)
class Choice:
    """The newest release on the branch, if any, and one line per skipped tag."""

    newest: Release | None
    skipped: tuple[str, ...]


def parse(tag: str) -> Release | None:
    """The release `tag` names; None when it does not follow the grammar."""
    if not (found := _RELEASE.fullmatch(tag)):
        return None
    year, month, day, number = (int(part) for part in found.groups())
    try:
        return Release(date(year, month, day), number)
    except ValueError:
        return None


def choose(tags: Iterable[str], on_branch: Collection[str], branch: str) -> Choice:
    """The newest release among `tags` that is in `on_branch`; every other tag is skipped and
    named, in tag order."""
    eligible: list[Release] = []
    skipped: list[str] = []
    for tag in sorted(set(tags)):
        release = parse(tag)
        shown = compose.printable(tag)
        if release is None:
            skipped.append(f"skipped tag {shown}: not {GRAMMAR}")
        elif tag not in on_branch:
            skipped.append(f"skipped tag {shown}: not on origin {compose.printable(branch)}")
        else:
            eligible.append(release)
    return Choice(max(eligible, default=None), tuple(skipped))
