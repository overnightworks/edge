"""`hal check`: what is wrong with this PC. Bare, it is the daily check, run in process:
rows, verdict, report and the news it delivers. With an area, `exposure` (what reaches
beyond loopback, local) or `hosts` (what Cloudflare holds against the hosts `fleet.yaml` declares,
up to 10 s per host), the area does the checking; this module adds the grammar, the JSON wrapper
keyed by the area, and the exit code."""

import argparse
import json
from pathlib import Path

from edge.external import cli as external
from edge.external import host_check
from edge_check import inspection
from hal.grammar import Asks, Command, ExitCode
from hal_output import write_error
from home_tools import check as daily_check
from home_tools import secrets_view
from home_tools.cli import build_layout_and_ports
from home_tools.config import load_config

EXPOSURE = "exposure"
HOSTS = "hosts"


HELP = f"""\
hal check             the daily check: services, backup, commit hook, secrets, server logs; it
                      writes the report and mails and pops up what is new, as the timer's run does
hal check {EXPOSURE}    what reaches beyond loopback on this PC
hal check {HOSTS}       what Cloudflare holds against the hosts fleet.yaml declares
"""


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = HELP
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


def _run_daily() -> ExitCode:
    layout, ports = build_layout_and_ports()
    try:
        config = load_config(layout)
    except (ValueError, OSError) as error:  # tomllib.TOMLDecodeError is a ValueError subclass
        write_error(f"{layout.config_file}: {error}")
        return ExitCode.FAILURE
    context = daily_check.CheckContext(
        layout, config, ports.run, ports.probe, ports.now, ports.disk_space
    )
    try:
        return ExitCode(daily_check.check(context))
    except secrets_view.ManifestError as error:
        write_error(str(error))
        return ExitCode.FAILURE


def _run(arguments: argparse.Namespace) -> ExitCode:
    if arguments.allow_list is not None and arguments.area != EXPOSURE:
        write_error("--allow-list belongs to one area", f"hal check {EXPOSURE} --allow-list FILE")
        return ExitCode.USAGE
    if arguments.area is None:
        if arguments.json:
            write_error("--json belongs to one area", f"hal check {EXPOSURE} --json")
            return ExitCode.USAGE
        return _run_daily()
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
        "run the daily check, or one area: what reaches beyond loopback, what Cloudflare holds",
        _run,
        _configure,
        json=True,
        asks=Asks.NEVER,
    ),
)
