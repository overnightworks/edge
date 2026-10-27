"""The one owner of how this repository's tools print to a terminal: text from outside made safe
to show, and the form of an error. It imports nothing else of this repository."""

import sys


def printable(text: str) -> str:
    """`text` with every character a terminal would act on escaped (a newline as `\\n`, ESC as
    `\\x1b`), so text from outside, a path or a command's line, stays one line of plain text."""
    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode()
        for character in text
    )


def write_error(message: str, next_step: str | None = None) -> None:
    """`error: <message>` and, when there is a way on, `next: <command>` on stderr."""
    print(f"error: {printable(message)}", file=sys.stderr)
    if next_step:
        print(f"next: {next_step}", file=sys.stderr)
