"""`edge`: dispatches to its subcommands (`apply`, `stop`, `start` and `external`)."""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from edge import apply, emergency, switches, tunnel
from edge.external import cli as external_cli

COMMAND = "edge apply"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="edge", description=__doc__)
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    add_apply_subcommand(subcommands)
    add_switch_subcommands(subcommands)
    external_cli.add_subcommand(subcommands)
    arguments = parser.parse_args(argv)
    return arguments.run(arguments)


def add_apply_subcommand(subcommands: argparse._SubParsersAction) -> None:
    apply_parser = subcommands.add_parser(
        "apply",
        help="switch the units of fleet.yaml on or off, then publish to Cloudflare",
        description="The local part (`--closed`: the units of this repository and the apps of the "
        "catalog, at a terminal, after `yes`), then, only if it ended clean, the publish "
        "(`edge external apply`, after its own `yes`). Each part asks only when it has changes.",
    )
    apply_parser.add_argument(
        "--closed",
        action="store_true",
        help="the local part alone: units of this repository and the apps of the catalog "
        "(checked out, built and run at their newest release), at a terminal, after `yes`",
    )
    apply_parser.set_defaults(run=apply_command)


def add_switch_subcommands(subcommands: argparse._SubParsersAction) -> None:
    stop_parser = subcommands.add_parser(
        "stop",
        help="switch a unit off at once and keep it off, or with --all the tunnel "
        "(no terminal needed)",
    )
    add_unit_or_all(stop_parser, "stop the tunnel, so the PC is off the internet")
    stop_parser.set_defaults(run=stop_command)
    start_parser = subcommands.add_parser(
        "start", help="end the switch of a unit, or with --all of the tunnel (terminal, `yes`)"
    )
    add_unit_or_all(start_parser, "reopen the tunnel once every unit barrier is proven")
    start_parser.set_defaults(run=start_command)


def add_unit_or_all(parser: argparse.ArgumentParser, all_help: str) -> None:
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("unit", nargs="?", help="a unit of fleet.yaml")
    choice.add_argument("--all", action="store_true", help=all_help)


def tunnel_of_environment(command: str) -> tunnel.Tunnel | None:
    """The tunnel the environment names; None, after saying why, when it names an invalid one."""
    try:
        return tunnel.Tunnel.from_environment(os.environ)
    except tunnel.TunnelError as error:
        print(f"{command}: cannot plan, {error}", file=sys.stderr)
        return None


def stop_command(arguments: argparse.Namespace) -> int:
    return stop_all() if arguments.all else stop_unit(arguments.unit)


def start_command(arguments: argparse.Namespace) -> int:
    return start_all() if arguments.all else start_unit(arguments.unit)


def stop_all() -> int:
    door = tunnel_of_environment(emergency.STOP_ALL)
    if door is None:
        return apply.ApplyExit.CANNOT_PLAN
    return emergency.stop_all(
        switches.Switches(switches.default_state_directory()),
        datetime.now().astimezone(),
        door,
    )


def start_all() -> int:
    door = tunnel_of_environment(emergency.START_ALL)
    if door is None:
        return apply.ApplyExit.CANNOT_PLAN
    return emergency.start_all(
        switches.Switches(switches.default_state_directory()),
        datetime.now().astimezone(),
        door,
    )


def stop_unit(unit: str) -> int:
    return emergency.stop_unit(
        switches.Switches(switches.default_state_directory()),
        unit,
        datetime.now().astimezone(),
    )


def start_unit(unit: str) -> int:
    return emergency.start_unit(switches.Switches(switches.default_state_directory()), unit)


@dataclass
class _Run:
    """What the interrupt handler of the composer knows, recorded where each fact is true."""

    publish_started: bool = False
    publish_exit: int | None = None


def apply_command(arguments: argparse.Namespace) -> int:
    return apply_units(arguments.closed)


def apply_units(closed: bool) -> int:
    if closed:
        return apply.apply_closed(switches.default_state_directory())
    return _apply_then_publish()


def _apply_then_publish() -> int:
    """The local part, then the publish only if the local part ended clean. The exit is the local
    one unless it is 0, then the publish's with its 2 ("cannot plan") made 1: a 2 from here means
    the local part could not finish planning, and nothing was published. No signal handler is
    installed here; the one `except KeyboardInterrupt` (SIGINT) decides by what is recorded.

    Accepted windows, not covered: a SIGINT between the publish's return and the store of its
    exit (exit 1 and the check line although the publish finished); a SIGTERM after the publish
    left its own context (default action: the process ends by the signal); a second Ctrl-C while
    `_exit_after_interrupt` prints (ends with a traceback)."""
    run = _Run()
    try:
        local_exit = apply.apply_closed(switches.default_state_directory())
        if local_exit != apply.ApplyExit.APPLIED_OR_NOTHING:
            print(f"{COMMAND}: nothing published", file=sys.stderr)
            return local_exit
        run.publish_started = True
        run.publish_exit = _published_exit(external_cli.apply_command(Path.home()))
        _say_summary(run.publish_exit)
        return run.publish_exit
    except KeyboardInterrupt:
        return _exit_after_interrupt(run)


def _published_exit(publish_exit: int) -> int:
    if publish_exit == apply.ApplyExit.CANNOT_PLAN:
        return apply.ApplyExit.REFUSED
    return publish_exit


def _say_summary(publish_exit: int) -> None:
    if publish_exit == apply.ApplyExit.APPLIED_OR_NOTHING:
        print(f"{COMMAND}: done", file=sys.stderr)
    else:
        print(f"{COMMAND}: the publish did not end clean (exit {publish_exit})", file=sys.stderr)


def _exit_after_interrupt(run: _Run) -> int:
    if run.publish_exit is not None:
        return run.publish_exit
    if run.publish_started:
        print(
            f"{COMMAND}: interrupted during the publish; run `edge external check --hosts`",
            file=sys.stderr,
        )
    else:
        print(f"{COMMAND}: interrupted, nothing published", file=sys.stderr)
    return apply.ApplyExit.REFUSED
