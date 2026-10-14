"""`home`: one command for the home directory, one subcommand per job."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from home_tools import check, tidy_home
from home_tools.tidy_home import CommandRunner, Layout, load_config, run_command

SUBCOMMANDS = (tidy_home, check)

EXAMPLES = """\
Beispiele:
  home check           tägliche Prüfung, wie der Timer sie fährt
  home tidy            zeigt, was im Home liegt, wo es nicht hingehört
  home tidy --apply    räumt die clean-Funde weg
  home <befehl> --help Einzelheiten zu einem Befehl

Konfiguration: ~/.config/home/config.toml. Anleitung: README.md im Repository home-tools.
"""


def _with_german_help(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("-h", "--help", action="help", help="zeigt diese Hilfe und beendet sich")
    return parser


def _subcommand_adder(subcommands: argparse._SubParsersAction) -> Callable[..., argparse.ArgumentParser]:
    def add_parser(name: str, **kwargs: object) -> argparse.ArgumentParser:
        return _with_german_help(subcommands.add_parser(name, add_help=False, **kwargs))

    return add_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="home",
        add_help=False,
        description="Hält das Home in der Form, die ~/README.md beschreibt.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _with_german_help(parser)
    subcommands = parser.add_subparsers(title="Befehle", metavar="<befehl>", required=True)
    add_parser = _subcommand_adder(subcommands)
    for subcommand in SUBCOMMANDS:
        subcommand.register(add_parser)
    return parser


def main(argv: Sequence[str] | None = None, home: Path | None = None, run: CommandRunner = run_command) -> int:
    args = build_parser().parse_args(argv)
    layout = Layout(home or Path.home())
    try:
        config = load_config(layout)
    except (ValueError, OSError) as error:  # tomllib.TOMLDecodeError is a ValueError subclass
        print(f"home: {error}", file=sys.stderr)
        return 2
    return args.handler(args, layout, config, run)
