"""`edge`: dispatches to its subcommands (`apply` and `external`)."""

import argparse
import sys

from edge import apply
from edge.external import cli as external_cli

PUBLISH_PHASE_MISSING = (
    "edge apply: the publish phase is not built yet; switch this repository's units "
    f"with `{apply.COMMAND}`"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edge", description=__doc__)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    add_apply_subcommand(subcommands)
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


def apply_command(arguments: argparse.Namespace) -> int:
    if arguments.closed:
        return apply.apply_closed()
    print(PUBLISH_PHASE_MISSING, file=sys.stderr)
    return apply.ApplyExit.CANNOT_PLAN
