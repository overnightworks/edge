"""`hal`: the door. It lists its commands, parses the command line, and gives every command the
same exit codes and the same error form (`error: …`, then `next: …`), with no traceback."""

import argparse
import sys
from collections.abc import Sequence

from hal import grammar, stack_commands, version
from hal.grammar import Command, ExitCode

DESCRIPTION = "hal, the daily door of the workstation."
COMMANDS = (*stack_commands.COMMANDS, version.COMMAND)


class _Parser(argparse.ArgumentParser):
    """A usage error is one `error:`/`next:` pair that names the parser's own help, exit 2."""

    def error(self, message: str):
        grammar.write_error(message, f"{self.prog} --help")
        raise SystemExit(ExitCode.USAGE)

    def parse_known_args(self, args=None, namespace=None):
        # A subcommand's parser judges its own leftovers, so the pair names its help, not hal's.
        parsed, unrecognized = super().parse_known_args(args, namespace)
        if unrecognized:
            self.error(f"unrecognized arguments: {' '.join(unrecognized)}")
        return parsed, unrecognized


def build_parser(commands: Sequence[Command]) -> argparse.ArgumentParser:
    parser = _Parser(prog="hal", description=DESCRIPTION)
    registry = parser.add_subparsers(dest="command", metavar="command", required=True)
    for command in commands:
        subparser = registry.add_parser(command.name, help=command.summary)
        command.configure(subparser)
        if command.json:
            subparser.add_argument("--json", action="store_true", help="print the result as JSON")
        subparser.set_defaults(run=command.run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = build_parser(COMMANDS)
    if not arguments:
        parser.print_help()
        return ExitCode.OK
    try:
        parsed = parser.parse_args(arguments)
        return parsed.run(parsed)
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    except Exception as error:
        # Only the type: a message may hold a path, a token or a line of someone else's text.
        grammar.write_error(f"unexpected error ({type(error).__name__})")
        return ExitCode.FAILURE
