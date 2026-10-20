"""`hal apply`, `hal stop`, `hal start`, `hal offline` and `hal online`: the stack's switches behind
hal's door. Each calls the typed function of `edge.cli` that `edge` calls too, so the guards, the
prompts, the state files and the lines are the stack's own; only the exit number is hal's."""

import argparse

from edge import apply
from edge import cli as stack
from hal.grammar import Asks, Command, ExitCode

# Every outcome of the stack means "the command could not do its job"; the finer split waits until
# the owners classify their own outcomes. Naming every member keeps a new one from passing as 0.
EXIT_OF_STACK: dict[apply.ApplyExit, ExitCode] = {
    apply.ApplyExit.APPLIED_OR_NOTHING: ExitCode.OK,
    apply.ApplyExit.CANNOT_PLAN: ExitCode.FAILURE,
    apply.ApplyExit.REFUSED: ExitCode.FAILURE,
}


def _exit_of(stack_exit: int) -> ExitCode:
    return EXIT_OF_STACK[apply.ApplyExit(stack_exit)]


def _configure_apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--closed",
        action="store_true",
        help="the local part alone: units of this repository, at a terminal, after `yes`",
    )


def _configure_unit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("unit", help="a unit of fleet.yaml")


def _configure_nothing(_parser: argparse.ArgumentParser) -> None:
    """`offline` and `online` take no argument."""


def _apply(arguments: argparse.Namespace) -> ExitCode:
    return _exit_of(stack.apply_units(arguments.closed))


def _stop(arguments: argparse.Namespace) -> ExitCode:
    return _exit_of(stack.stop_unit(arguments.unit))


def _start(arguments: argparse.Namespace) -> ExitCode:
    return _exit_of(stack.start_unit(arguments.unit))


def _offline(_arguments: argparse.Namespace) -> ExitCode:
    return _exit_of(stack.stop_all())


def _online(_arguments: argparse.Namespace) -> ExitCode:
    return _exit_of(stack.start_all())


COMMANDS = (
    Command(
        "apply",
        "switch the units of fleet.yaml on or off, then publish to Cloudflare",
        _apply,
        _configure_apply,
        json=False,
        asks=Asks.YES,
    ),
    Command(
        "stop",
        "switch a unit off at once and keep it off (no terminal needed)",
        _stop,
        _configure_unit,
        json=False,
        asks=Asks.NEVER,
    ),
    Command(
        "start",
        "end the switch of a unit (terminal, `yes`)",
        _start,
        _configure_unit,
        json=False,
        asks=Asks.YES,
    ),
    Command(
        "offline",
        "stop the tunnel, so the PC is off the internet (no terminal needed)",
        _offline,
        _configure_nothing,
        json=False,
        asks=Asks.NEVER,
    ),
    Command(
        "online",
        "reopen the tunnel once every unit barrier is proven (terminal, `yes`)",
        _online,
        _configure_nothing,
        json=False,
        asks=Asks.YES,
    ),
)
