"""`edge`: dispatches to its subcommands (`apply`, `stop`, `start` and `external`)."""

import argparse
import os
import sys
from datetime import datetime

from edge import apply, emergency, switches, tunnel
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
        "stop",
        help="switch a unit off at once and keep it off, or with --all the tunnel "
        "(no terminal needed)",
    )
    add_unit_or_all(stop_parser, "stop the tunnel, so the PC is off the internet")
    stop_parser.set_defaults(run=stop_command)
    start_parser = subcommands.add_parser(
        "start", help="end the switch of a unit, or with --all of the tunnel (terminal, `yes`)"
    )
    add_unit_or_all(start_parser, "reopen the tunnel once every unit barrier is proven")
    start_parser.set_defaults(run=start_command)


def add_unit_or_all(parser: argparse.ArgumentParser, all_help: str) -> None:
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("unit", nargs="?", help="a unit of fleet.yaml")
    choice.add_argument("--all", action="store_true", help=all_help)


def tunnel_of_environment(command: str) -> tunnel.Tunnel | None:
    """The tunnel the environment names; None, after saying why, when it names an invalid one."""
    try:
        return tunnel.Tunnel.from_environment(os.environ)
    except tunnel.TunnelError as error:
        print(f"{command}: cannot plan, {error}", file=sys.stderr)
        return None


def stop_command(arguments: argparse.Namespace) -> int:
    if arguments.all:
        door = tunnel_of_environment(emergency.STOP_ALL)
        if door is None:
            return apply.ApplyExit.CANNOT_PLAN
        return emergency.stop_all(
            switches.Switches(switches.default_state_directory()),
            datetime.now().astimezone(),
            door,
        )
    return emergency.stop_unit(
        switches.Switches(switches.default_state_directory()),
        arguments.unit,
        datetime.now().astimezone(),
    )


def start_command(arguments: argparse.Namespace) -> int:
    if arguments.all:
        door = tunnel_of_environment(emergency.START_ALL)
        if door is None:
            return apply.ApplyExit.CANNOT_PLAN
        return emergency.start_all(
            switches.Switches(switches.default_state_directory()),
            datetime.now().astimezone(),
            door,
        )
    return emergency.start_unit(
        switches.Switches(switches.default_state_directory()), arguments.unit
    )


def apply_command(arguments: argparse.Namespace) -> int:
    if arguments.closed:
        return apply.apply_closed(switches.default_state_directory())
    print(PUBLISH_PHASE_MISSING, file=sys.stderr)
    return apply.ApplyExit.CANNOT_PLAN
