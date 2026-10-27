"""`hal check`: what is wrong with this PC. One area today, `exposure` (what reaches beyond
loopback); the area does the checking, this module adds the grammar, the JSON wrapper keyed by
the area, and the exit code."""

import argparse
import json
from pathlib import Path

from edge_check import inspection
from hal import grammar
from hal.grammar import Asks, Command, ExitCode

EXPOSURE = "exposure"


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("area", nargs="?", choices=(EXPOSURE,), help="the area to check")
    parser.add_argument(
        "--allow-list",
        type=Path,
        help=f"known listeners beyond loopback (default: {inspection.DEFAULT_ALLOW_LIST})",
    )


def _run(arguments: argparse.Namespace) -> ExitCode:
    if arguments.area is None and arguments.allow_list is not None:
        grammar.write_error(
            "--allow-list belongs to one area", f"hal check {EXPOSURE} --allow-list FILE"
        )
        return ExitCode.USAGE
    try:
        findings = inspection.inspect(arguments.allow_list or inspection.DEFAULT_ALLOW_LIST)
    except inspection.ExposureCheckError as error:
        grammar.write_error(f"hal check: {EXPOSURE}: cannot check, {error}")
        return ExitCode.FAILURE
    if arguments.json:
        print(json.dumps({EXPOSURE: inspection.as_json_objects(findings)}, indent=2))
    else:
        print(inspection.render_table(findings))
    return ExitCode.FINDINGS if findings else ExitCode.OK


COMMANDS = (
    Command(
        "check",
        "report what reaches beyond loopback on this PC",
        _run,
        _configure,
        json=True,
        asks=Asks.NEVER,
    ),
)
