"""`hal setup`: the typed function of `home_tools.setup`, with the layout and boundaries
`home_tools.ports` builds, so the steps, the prompts and the lines are `home_tools`' own."""

import argparse

from hal.grammar import Asks, Command, ExitCode
from hal_output.command import Verb
from home_tools import setup
from home_tools.ports import build_layout_and_ports

# `setup` ends 0 or 1, and 1 means a step failed. Naming both keeps a new code from
# passing as 0.
EXIT_OF_SETUP: dict[int, ExitCode] = {0: ExitCode.OK, 1: ExitCode.FAILURE}

STEP_DESCRIPTION = (
    "The system step makes the journal Docker's default log driver and restarts Docker while its"
    " default driver is not journald: every container stops meanwhile, and one without a restart"
    " policy stays down."
)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.description = STEP_DESCRIPTION
    parser.add_argument(
        "--apply", action="store_true", help="run the steps instead of only showing them"
    )


def _setup(arguments: argparse.Namespace) -> ExitCode:
    layout, ports = build_layout_and_ports()
    return EXIT_OF_SETUP[setup.setup(layout, ports, arguments.apply)]


COMMANDS = (
    Command(
        Verb.SETUP,
        "set up quality, the commit hook, the system step (sudo, terminal) and the missing secrets",
        _setup,
        _configure,
        json=False,
        asks=Asks.YES,
    ),
)
