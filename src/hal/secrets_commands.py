"""`hal secrets`: every secret of the manifest, never a value. It only views; the wizard that sets
up what is missing is `hal setup --apply`. The view is `home_tools.secrets_view`; this module adds
the grammar and the exit code."""

import argparse

from hal.grammar import Asks, Command, ExitCode
from hal_output.command import Verb, command
from home_tools import secrets_view
from home_tools.cli import build_layout_and_ports

HELP = f"""\
{command(Verb.SECRETS)}             every secret of the manifest: file, check, expiry
{command(Verb.SECRETS, "--by-service")}  which service reads which secret (declared)

It shows no value and changes nothing except its report file. It needs no terminal; agents
may run it.
{command(Verb.SETUP, "--apply")} sets up the missing secrets, at your own terminal.
"""


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = HELP
    parser.add_argument(
        "--by-service", action="store_true", help="grouped by service: declared dependencies"
    )


def _run(arguments: argparse.Namespace) -> ExitCode:
    if arguments.by_service:
        return ExitCode(secrets_view.show_by_service())
    layout, ports = build_layout_and_ports()
    return ExitCode(secrets_view.show(layout, ports.probe, ports.now))


COMMANDS = (
    Command(
        Verb.SECRETS,
        "show every secret and what is missing; never a value",
        _run,
        _configure,
        json=False,
        asks=Asks.NEVER,
    ),
)
