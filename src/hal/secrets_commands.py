"""`hal secrets`: every secret of the manifest, never a value. It only views; the wizard that sets
up what is missing is `hal setup`. The view is `hal.workstation.secrets_view` and its rows are
`hal.secrets_rows`; this module adds the grammar, the report file and the exit code."""

import argparse

from hal import secrets_rows
from hal.grammar import Asks, Command, ExitCode
from hal.output.command import Verb, command
from hal.workstation import secrets_view
from hal.workstation.ports import build_layout_and_ports
from hal.workstation.report import write_state_file

HELP = f"""\
{command(Verb.SECRETS)}             what needs attention, and how many entries are fine
{command(Verb.SECRETS, "-v")}          every entry as a table: file, check, place, renewal
{command(Verb.SECRETS, "--by-service")}  which service reads which secret (declared)

It shows no value and changes nothing except its report file. It needs no terminal; agents
may run it.
{command(Verb.SETUP)} sets up the missing secrets, at your own terminal.
"""


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = HELP
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="every entry as a table, every finding alone"
    )
    parser.add_argument(
        "--by-service", action="store_true", help="grouped by service: declared dependencies"
    )


def _run(arguments: argparse.Namespace) -> ExitCode:
    if arguments.by_service:
        return ExitCode(secrets_view.show_by_service())
    layout, ports = build_layout_and_ports()
    view, report = secrets_view.inspect(layout, ports.probe, ports.now)
    write_state_file(layout.home, layout.secrets_report_file, report)
    secrets_rows.write(view, layout, verbose=arguments.verbose)
    return ExitCode(1 if view.findings else 0)


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
