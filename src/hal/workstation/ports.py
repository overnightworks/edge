"""The boundaries and the real layout every `hal` verb over `hal.workstation` is built with."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hal.workstation import system_step
from hal.workstation.platform_check import DiskSpaceReader, read_disk_space
from hal.workstation.secrets_setup import TERMINAL_BOUNDARIES, Boundaries
from hal.workstation.secrets_view import ExitCodeProbe, probe_exit_code
from hal.workstation.layout import CommandRunner, Layout, run_command


def edge_units_directory(layout: Layout) -> Path:
    """The user units sit beside `tunnel/` in the edge checkout, whose location Layout owns."""
    return layout.edge_tunnel_dir.parent / "systemd"


@dataclass(frozen=True)
class Ports:
    """Every boundary a setup step may cross; `build_layout_and_ports` builds the real ones, tests hand in fakes."""

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
    """The one owner of the real layout and boundaries; every `hal` verb over `hal.workstation` builds its own here."""
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
