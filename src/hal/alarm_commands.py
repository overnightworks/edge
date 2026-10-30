"""`hal alarm`: the timer's five-minute run over the services and the backup, in process.

The grammar and the exit code; `hal.workstation.alarm` does the counting and the delivery."""

import argparse

from hal import config as hal_config
from hal.grammar import Asks, Command, ExitCode
from hal.output import write_error
from hal.output.command import Verb
from hal.workstation import alarm
from hal.workstation import check as daily_check
from hal.workstation.ports import build_layout_and_ports


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.description = "settings: ~/.config/hal/config.toml, table [alarm] (docs/home.md)"


def _run(_arguments: argparse.Namespace) -> ExitCode:
    layout, ports = build_layout_and_ports()
    try:
        settings = hal_config.load(layout.home)
    except hal_config.ConfigInvalid as error:
        write_error(f"{error.path}: {error.reason}")
        return ExitCode.USAGE
    # `list` reads no exposure: the alarm runs the services and the backup only.
    context = daily_check.CheckContext(
        layout, settings.check, ports.run, ports.probe, ports.now, ports.disk_space, list
    )
    alarm.alarm(context, settings.alarm)
    return ExitCode.OK


COMMANDS = (
    Command(
        Verb.ALARM,
        "mail a service or backup finding that two runs in a row reported, and one that went",
        _run,
        _configure,
        json=False,
        asks=Asks.NEVER,
    ),
)
