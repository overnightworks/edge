"""What every hal command shares: its exit codes, its declaration and the form of an error."""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum

from edge import compose


class ExitCode(IntEnum):
    OK = 0
    FINDINGS = 1  # the command ran and found something that needs attention
    USAGE = 2  # the command line is wrong
    FAILURE = 3  # the command could not do its job
    REFUSED = 4  # a guard of the command said no
    INTERRUPTED = 130  # Ctrl-C


class Asks(Enum):
    """Whether a command waits for the person at the terminal."""

    NEVER = "never"
    YES = "yes"


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[argparse.Namespace], ExitCode]
    configure: Callable[[argparse.ArgumentParser], None]
    json: bool  # only a command whose result a program reads offers `--json`
    asks: Asks


def write_error(message: str, next_step: str | None = None) -> None:
    """`error: <message>` and, when there is a way on, `next: <command>` on stderr."""
    print(f"error: {compose.printable(message)}", file=sys.stderr)
    if next_step:
        print(f"next: {next_step}", file=sys.stderr)
