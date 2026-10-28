"""`home`: one command for the home directory, one subcommand per job."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hal_output import write_error
from home_tools import secrets_setup, secrets_view, setup, system_step
from home_tools import next as next_steps
from home_tools.platform_check import DiskSpaceReader, read_disk_space
from home_tools.secrets_setup import TERMINAL_BOUNDARIES, Boundaries
from home_tools.secrets_view import ExitCodeProbe, probe_exit_code
from home_tools.layout import CommandRunner, Layout, run_command


def edge_units_directory(layout: Layout) -> Path:
    """The user units sit beside `tunnel/` in the edge checkout, whose location Layout owns."""
    return layout.edge_tunnel_dir.parent / "systemd"


@dataclass(frozen=True)
class Ports:
    """Every boundary a subcommand handler may cross; `main` builds the real ones, tests hand in fakes."""

    run: CommandRunner
    probe: ExitCodeProbe
    secrets_setup: Boundaries
    now: datetime
    clock: Callable[[], float]
    system: system_step.Boundaries
    unit_directory: Path
    disk_space: DiskSpaceReader


# Looked up per call, so the tests can keep every run's time off this machine's clock.
MONOTONIC_CLOCK: Callable[[], float] = time.monotonic

SUBCOMMANDS = (next_steps, secrets_setup, setup)
UNEXPECTED_ERROR_EXIT = 3
MANIFEST_INVALID_EXIT = 2

EXAMPLES = """\
Examples:
  home next            what you need to do now, each step with its exact command
  home secrets         every secret, never a value
  home secrets --setup walks you through every missing secret at a terminal
  home setup           shows what the commit hook against secrets still needs; at a terminal it asks once for sudo
  home setup --apply   sets up quality, the engines and the hook, then the system step (asks once for sudo)
  home <command> --help details of one command

Guide: docs/home.md in the edge repository.
"""


def build_layout_and_ports(
    home: Path | None = None,
    run: CommandRunner | None = None,
    probe: ExitCodeProbe | None = None,
    now: datetime | None = None,
    secrets_setup_boundaries: Boundaries | None = None,
    system_boundaries: system_step.Boundaries | None = None,
    disk_space: DiskSpaceReader | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[Layout, Ports]:
    """The one owner of the real layout and boundaries; `home` and `hal setup` both build theirs here."""
    # The system boundaries are looked up per call, so the test sandbox can keep every test away from sudo and Ansible.
    system = system_boundaries or system_step.TERMINAL_BOUNDARIES
    layout = Layout(home or Path.home())
    return layout, Ports(
        run or run_command,
        probe or probe_exit_code,
        secrets_setup_boundaries or TERMINAL_BOUNDARIES,
        now or datetime.now(),
        clock or MONOTONIC_CLOCK,
        system,
        edge_units_directory(layout),
        disk_space or read_disk_space,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="home",
        description="Shows what this PC needs from you and walks you through the secrets.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(error_may_quote_a_secret=False)
    subcommands = parser.add_subparsers(title="Commands", metavar="<command>", required=True)
    for subcommand in SUBCOMMANDS:
        subcommand.register(subcommands.add_parser)
    return parser


def main(
    argv: Sequence[str] | None = None,
    home: Path | None = None,
    run: CommandRunner | None = None,
    probe: ExitCodeProbe | None = None,
    now: datetime | None = None,
    secrets_setup_boundaries: Boundaries | None = None,
    system_boundaries: system_step.Boundaries | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    layout, ports = build_layout_and_ports(
        home, run, probe, now, secrets_setup_boundaries, system_boundaries, None, clock
    )
    # Exit 1 means "findings" and the timer counts it as success, so a crash must end with its own code.
    try:
        return args.handler(args, layout, ports)
    except secrets_view.ManifestError as error:
        write_error(str(error))
        return MANIFEST_INVALID_EXIT
    except Exception as error:
        # A subcommand that handles secrets never has an unexpected error's message shown: it may quote a value.
        message = "" if args.error_may_quote_a_secret else f": {error}"
        write_error(f"unexpected error ({type(error).__name__}){message}")
        return UNEXPECTED_ERROR_EXIT
