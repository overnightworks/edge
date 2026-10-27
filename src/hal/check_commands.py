"""`hal check`: what is wrong with this PC. One area per run, `exposure` (what reaches beyond
loopback, local) or `hosts` (what Cloudflare holds against the hosts `fleet.yaml` declares, up to
10 s per host); the area does the checking, this module adds the grammar, the JSON wrapper keyed
by the area, and the exit code."""

import argparse
import json
from pathlib import Path

from edge.external import cli as external
from edge.external import host_check
from edge_check import inspection
from hal.grammar import Asks, Command, ExitCode
from hal_output import write_error

EXPOSURE = "exposure"
HOSTS = "hosts"


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("area", nargs="?", choices=(EXPOSURE, HOSTS), help="the area to check")
    parser.add_argument(
        "--allow-list",
        type=Path,
        help=f"{EXPOSURE} only: known listeners beyond loopback "
        f"(default: {inspection.DEFAULT_ALLOW_LIST})",
    )


_HOST_CHECK_EXIT = {
    host_check.CheckExit.CLEAN: ExitCode.OK,
    host_check.CheckExit.FINDINGS: ExitCode.FINDINGS,
    host_check.CheckExit.CANNOT_CHECK: ExitCode.FAILURE,
    external.INTERRUPTED: ExitCode.INTERRUPTED,
}


def _run_hosts(as_json: bool) -> ExitCode:
    def emit(text: str) -> None:
        print(json.dumps({HOSTS: json.loads(text)}, indent=2) if as_json else text)

    outcome = external.run_host_check(Path.home(), as_json=as_json, emit=emit)
    for line in outcome.cause:
        write_error(f"hal check: {HOSTS}: cannot check, {line}")
    return _HOST_CHECK_EXIT[outcome.exit_code]


def _run(arguments: argparse.Namespace) -> ExitCode:
    if arguments.allow_list is not None and arguments.area != EXPOSURE:
        write_error("--allow-list belongs to one area", f"hal check {EXPOSURE} --allow-list FILE")
        return ExitCode.USAGE
    if arguments.area == HOSTS:
        return _run_hosts(arguments.json)
    try:
        findings = inspection.inspect(arguments.allow_list or inspection.DEFAULT_ALLOW_LIST)
    except inspection.ExposureCheckError as error:
        write_error(f"hal check: {EXPOSURE}: cannot check, {error}")
        return ExitCode.FAILURE
    if arguments.json:
        print(json.dumps({EXPOSURE: inspection.as_json_objects(findings)}, indent=2))
    else:
        print(inspection.render_table(findings))
    return ExitCode.FINDINGS if findings else ExitCode.OK


COMMANDS = (
    Command(
        "check",
        "report what reaches beyond loopback on this PC, or what Cloudflare holds for the hosts",
        _run,
        _configure,
        json=True,
        asks=Asks.NEVER,
    ),
)
