"""What every hal command shares: its exit codes and its declaration."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum

from hal_output.command import Verb


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
    name: Verb
    summary: str
    run: Callable[[argparse.Namespace], ExitCode]
    configure: Callable[[argparse.ArgumentParser], None]
    json: bool  # only a command whose result a program reads offers `--json`
    asks: Asks
