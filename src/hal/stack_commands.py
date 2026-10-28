"""`hal apply`, `hal stop`, `hal start`, `hal offline` and `hal online`: the stack's switches behind
hal's door. Each calls the typed function of `edge.cli` that `edge` calls too, so the guards, the
prompts, the state files and the lines are the stack's own; only the exit number is hal's."""

import argparse

from edge import apply
from edge import cli as stack
from hal.grammar import Asks, Command, ExitCode
from hal_output.command import Verb

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
        help="the local part alone: units of this repository and the apps of the catalog "
        "(checked out, built and run at their newest release), at a terminal, after `yes`",
    )


def _configure_unit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("unit", help="a unit or an app of fleet.yaml")


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
        Verb.APPLY,
        "switch the units of fleet.yaml on or off, then publish to Cloudflare",
        _apply,
        _configure_apply,
        json=False,
        asks=Asks.YES,
    ),
    Command(
        Verb.STOP,
        "switch a unit or an app off at once and keep it off (no terminal needed)",
        _stop,
        _configure_unit,
        json=False,
        asks=Asks.NEVER,
    ),
    Command(
        Verb.START,
        "end the switch of a unit or an app (terminal, `yes`)",
        _start,
        _configure_unit,
        json=False,
        asks=Asks.YES,
    ),
    Command(
        Verb.OFFLINE,
        "stop the tunnel, so the PC is off the internet (no terminal needed)",
        _offline,
        _configure_nothing,
        json=False,
        asks=Asks.NEVER,
    ),
    Command(
        Verb.ONLINE,
        "reopen the tunnel once every unit and app barrier is proven (terminal, `yes`)",
        _online,
        _configure_nothing,
        json=False,
        asks=Asks.YES,
    ),
)
