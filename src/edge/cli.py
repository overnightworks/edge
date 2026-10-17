"""`edge`: dispatches to its subcommands (`apply`, `stop`, `start` and `external`)."""

import argparse
import sys
from datetime import datetime

from edge import apply, emergency, switches
from edge.external import cli as external_cli

PUBLISH_PHASE_MISSING = (
    "edge apply: the publish phase is not built yet; switch this repository's units "
    f"with `{apply.COMMAND}`"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edge", description=__doc__)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    add_apply_subcommand(subcommands)
    add_switch_subcommands(subcommands)
    external_cli.add_subcommand(subcommands)
    arguments = parser.parse_args(argv)
    return arguments.run(arguments)


def add_apply_subcommand(subcommands: argparse._SubParsersAction) -> None:
    apply_parser = subcommands.add_parser(
        "apply", help="switch the units of fleet.yaml on or off (needs --closed)"
    )
    apply_parser.add_argument(
        "--closed",
        action="store_true",
        help="the closed phase: units of this repository, at a terminal, after `yes`",
    )
    apply_parser.set_defaults(run=apply_command)


def add_switch_subcommands(subcommands: argparse._SubParsersAction) -> None:
    stop_parser = subcommands.add_parser(
        "stop", help="switch a unit off at once and keep it off (no terminal needed)"
    )
    stop_parser.add_argument("unit", help="a unit of fleet.yaml")
    stop_parser.set_defaults(run=stop_command)
    start_parser = subcommands.add_parser(
        "start", help="end the switch of a unit (at a terminal, after `yes`)"
    )
    start_parser.add_argument("unit", help="a unit of fleet.yaml")
    start_parser.set_defaults(run=start_command)


def stop_command(arguments: argparse.Namespace) -> int:
    return emergency.stop_unit(
        switches.Switches(switches.default_state_directory()),
        arguments.unit,
        datetime.now().astimezone(),
    )


def start_command(arguments: argparse.Namespace) -> int:
    return emergency.start_unit(
        switches.Switches(switches.default_state_directory()), arguments.unit
    )


def apply_command(arguments: argparse.Namespace) -> int:
    if arguments.closed:
        return apply.apply_closed(switches.default_state_directory())
    print(PUBLISH_PHASE_MISSING, file=sys.stderr)
    return apply.ApplyExit.CANNOT_PLAN
