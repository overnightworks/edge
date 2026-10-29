"""The timer jobs hal runs on this machine: the one place a job's unit name, command, schedule and
service settings are written. `hal setup` writes the units from these declarations.

This module holds no systemd text; the unit writer renders it.
"""

from __future__ import annotations

from dataclasses import dataclass

from hal.output.command import Verb, command


@dataclass(frozen=True)
class TimerJob:
    """One `hal` verb that a systemd timer runs on a calendar."""

    name: str
    service_description: str
    timer_description: str
    verb: Verb
    arguments: tuple[str, ...]
    on_calendar: str
    timeout_start: str | None = None
    success_exit_statuses: tuple[int, ...] = ()
    documentation: str | None = None


TIMER_JOBS: tuple[TimerJob, ...] = (
    TimerJob(
        name="hal-backup",
        service_description=f"{command(Verb.BACKUP)}: encrypted restic backup of the declared apps",
        timer_description=f"{command(Verb.BACKUP)} weekly, Sunday 03:00",
        verb=Verb.BACKUP,
        arguments=(),
        on_calendar="Sun *-*-* 03:00:00",
        timeout_start="6h",
        documentation="docs/backup.md",
    ),
    TimerJob(
        name="hal-backup-restore-test",
        service_description=f"{command(Verb.BACKUP)}: restore the latest snapshots and prove they are good",
        timer_description=f"{command(Verb.BACKUP)} restore test monthly, first Sunday 05:00",
        verb=Verb.RESTORE,
        arguments=("--test",),
        # Two hours after the weekly backup; the host lock serialises them if the backup runs long.
        on_calendar="Sun *-*-01..07 05:00:00",
        timeout_start="6h",
        documentation="docs/backup.md",
    ),
    TimerJob(
        name="hal-check",
        service_description=(
            f"{command(Verb.CHECK)}: reports what is wrong on this machine and in the secrets"
            " manifest; mails only when a finding appears or goes"
        ),
        timer_description=f"Daily {command(Verb.CHECK)}",
        verb=Verb.CHECK,
        arguments=(),
        on_calendar="*-*-* 09:00",
        # Exit 1 means "findings exist": news, not a failure. Exit 2 (invalid config) and 3
        # (unexpected error) still fail the unit.
        success_exit_statuses=(1,),
    ),
)
