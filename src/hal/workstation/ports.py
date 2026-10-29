"""The boundaries and the real layout every `hal` verb over `hal.workstation` is built with."""

from __future__ import annotations

import http.client
import shutil
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from hal.workstation import jobs, system_step
from hal.workstation.jobs import TimerJob
from hal.workstation.platform_check import DiskSpaceReader, read_disk_space
from hal.workstation.secrets_setup import TERMINAL_BOUNDARIES, Boundaries
from hal.workstation.secrets_view import ExitCodeProbe, probe_exit_code
from hal.workstation.layout import CommandRunner, Layout, run_command


Downloader = Callable[[str, BinaryIO], None]
"""Writes what the URL serves into the writer; an OSError says the download failed."""

# The longest the connection may stay silent; `run_command`'s limit is far too short for a binary of about 176 MB.
DOWNLOAD_TIMEOUT_SECONDS = 60


def download_release(url: str, writer: BinaryIO) -> None:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            shutil.copyfileobj(response, writer)
    except http.client.HTTPException as error:
        raise OSError(repr(error)) from error


@dataclass(frozen=True)
class Ports:
    """Every boundary a setup step may cross; `build_layout_and_ports` builds the real ones, tests hand in fakes."""

    run: CommandRunner
    probe: ExitCodeProbe
    secrets_setup: Boundaries
    now: datetime
    clock: Callable[[], float]
    system: system_step.Boundaries
    timer_jobs: tuple[TimerJob, ...]
    disk_space: DiskSpaceReader
    download: Downloader


# Looked up per call, so the tests can keep every run's time off this machine's clock.
MONOTONIC_CLOCK: Callable[[], float] = time.monotonic
# Looked up per call, so no test downloads.
DOWNLOAD: Downloader = download_release


def build_layout_and_ports(
    home: Path | None = None,
    run: CommandRunner | None = None,
    probe: ExitCodeProbe | None = None,
    now: datetime | None = None,
    secrets_setup_boundaries: Boundaries | None = None,
    system_boundaries: system_step.Boundaries | None = None,
    disk_space: DiskSpaceReader | None = None,
    clock: Callable[[], float] | None = None,
    download: Downloader | None = None,
) -> tuple[Layout, Ports]:
    """The one owner of the real layout and boundaries; every `hal` verb over `hal.workstation` builds its own here."""
    # The system boundaries and the timer jobs are looked up per call, so the test sandbox can keep every test away from sudo, Ansible and systemctl.
    system = system_boundaries or system_step.TERMINAL_BOUNDARIES
    layout = Layout(home or Path.home())
    return layout, Ports(
        run or run_command,
        probe or probe_exit_code,
        secrets_setup_boundaries or TERMINAL_BOUNDARIES,
        now or datetime.now(),
        clock or MONOTONIC_CLOCK,
        system,
        jobs.TIMER_JOBS,
        disk_space or read_disk_space,
        download or DOWNLOAD,
    )
