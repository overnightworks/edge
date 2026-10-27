"""`hal legal`: an app's legal files checked with the checker and the assembly `hal apply` uses.
It writes nothing and builds no page; edge's own legal tree is read from this checkout."""

import argparse
from datetime import date
from enum import StrEnum
from pathlib import Path

from edge import compose, legal_step
from edge.legal_section import LegalError
from hal.grammar import Asks, Command, ExitCode
from hal_output import Glyph, Row, write_error, write_row


class LegalEvent(StrEnum):
    VALID = "legal valid"


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("key", help="the catalog key the app will have; it is the id of its h2")
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="the app's checkout, holding legal/datenschutz.html and, optionally, "
        "legal/nutzungsbedingungen.html (default: the current directory)",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=compose.REPOSITORY,
        help="edge's tree, holding the catalog, the frame and the general part "
        "(default: the checkout this hal runs from)",
    )


def _legal(arguments: argparse.Namespace) -> ExitCode:
    if not arguments.directory.is_dir():
        write_error(f"{arguments.directory} is not a directory")
        return ExitCode.FAILURE
    try:
        files = legal_step.check_app(
            arguments.repository, arguments.key, arguments.directory, date.today()
        )
    except LegalError as error:
        write_error(str(error))
        return ExitCode.FINDINGS
    pages = ", ".join(Path(file).stem for file in files)
    write_row(Row(Glyph.DONE, arguments.key, LegalEvent.VALID, pages))
    return ExitCode.OK


COMMANDS = (
    Command(
        "legal",
        "check an app's legal files the way `hal apply` would, before the app is tagged",
        _legal,
        _configure,
        json=False,
        asks=Asks.NEVER,
    ),
)
