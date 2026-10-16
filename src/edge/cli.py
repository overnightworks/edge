"""`edge`: dispatches to its subcommands (so far `external`)."""

import argparse

from edge.external import cli as external_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edge", description=__doc__)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    external_cli.add_subcommand(subcommands)
    arguments = parser.parse_args(argv)
    return arguments.run(arguments)
