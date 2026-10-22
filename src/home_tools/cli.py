"""`home`: one command for the home directory, one subcommand per job."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from home_tools import check, secrets_setup, secrets_view, setup, system_step, tidy_home
from home_tools import next as next_steps
from home_tools.secrets_setup import TERMINAL_BOUNDARIES, Boundaries
from home_tools.secrets_view import ExitCodeProbe, probe_exit_code
from home_tools.tidy_home import CommandRunner, Config, Layout, load_config, run_command


@dataclass(frozen=True)
class Ports:
    """Every boundary a subcommand handler may cross; `main` builds the real ones, tests hand in fakes."""

    run: CommandRunner
    probe: ExitCodeProbe
    secrets_setup: Boundaries
    now: datetime
    system: system_step.Boundaries


SUBCOMMANDS = (next_steps, check, tidy_home, secrets_setup, setup)
UNEXPECTED_ERROR_EXIT = 3

EXAMPLES = """\
Beispiele:
  home next            was jetzt von dir gebraucht wird, je Schritt mit dem genauen Befehl
  home check           tägliche Prüfung, wie der Timer sie fährt
  home tidy            zeigt, was im Home liegt, wo es nicht hingehört
  home tidy --apply    räumt die clean-Funde weg
  home secrets         zeigt jedes Geheimnis, nie einen Wert
  home secrets --setup führt am Terminal durch jedes fehlende Geheimnis
  home setup           zeigt, was der Commit-Hook gegen Geheimnisse noch braucht; at a terminal it asks once for sudo
  home setup --apply   richtet quality, die Engines und den Hook ein, then the system step (asks once for sudo)
  home <befehl> --help Einzelheiten zu einem Befehl

Konfiguration: ~/.config/home/config.toml. Anleitung: README.md im Repository home-tools.
"""


def build_layout_and_ports(
    home: Path | None = None,
    run: CommandRunner = run_command,
    probe: ExitCodeProbe = probe_exit_code,
    now: datetime | None = None,
    secrets_setup_boundaries: Boundaries = TERMINAL_BOUNDARIES,
    system_boundaries: system_step.Boundaries | None = None,
) -> tuple[Layout, Ports]:
    """The one owner of the real layout and boundaries; `home` and `hal setup` both build theirs here."""
    # The system boundaries are looked up per call, so the test sandbox can keep every test away from sudo and Ansible.
    system = system_boundaries or system_step.TERMINAL_BOUNDARIES
    return Layout(home or Path.home()), Ports(run, probe, secrets_setup_boundaries, now or datetime.now(), system)


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
    parser.set_defaults(error_may_quote_a_secret=False, reads_config_as_a_source=False)
    subcommands = parser.add_subparsers(title="Befehle", metavar="<befehl>", required=True)
    add_parser = _subcommand_adder(subcommands)
    for subcommand in SUBCOMMANDS:
        subcommand.register(add_parser)
    return parser


def main(
    argv: Sequence[str] | None = None,
    home: Path | None = None,
    run: CommandRunner = run_command,
    probe: ExitCodeProbe = probe_exit_code,
    now: datetime | None = None,
    secrets_setup_boundaries: Boundaries = TERMINAL_BOUNDARIES,
    system_boundaries: system_step.Boundaries | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    layout, ports = build_layout_and_ports(home, run, probe, now, secrets_setup_boundaries, system_boundaries)
    try:
        # A subcommand that reads the config as one of its sources turns an invalid one into its own line, never exit 2.
        config = Config() if args.reads_config_as_a_source else load_config(layout)
    except (ValueError, OSError) as error:  # tomllib.TOMLDecodeError is a ValueError subclass
        print(f"home: {error}", file=sys.stderr)
        return 2
    # Exit 1 means "findings" and the timer counts it as success, so a crash must end with its own code.
    try:
        return args.handler(args, layout, config, ports)
    except secrets_view.ManifestError as error:
        print(f"home: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        # A subcommand that handles secrets never has an unexpected error's message shown: it may quote a value.
        message = "" if args.error_may_quote_a_secret else f": {error}"
        print(f"home: unerwarteter Fehler ({type(error).__name__}){message}", file=sys.stderr)
        return UNEXPECTED_ERROR_EXIT
