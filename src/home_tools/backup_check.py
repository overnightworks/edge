"""`home check`'s backup check: reports a failed or dead backup run and every app whose last good backup is too old.

It reads one file, the status the backup writes (schema 2, documented in docs/backup.md), and only the fields named
here; it never opens the backups, their repository or their password. `edge_backup` stays unimported because the two
packages are independent. A status file that is missing, unreadable or of another schema is a finding of its own,
never a pass.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from home_tools.report import Finding, render_findings_report

STATUS_FILE = Path("state/edge/backup-status.json")  # below ~/.local; written by the backup, which `home` only reads
STATUS_SCHEMA = 2
STATES = frozenset({"running", "ok", "failed"})
EXIT_CATEGORIES = frozenset({"configuration", "not-ready", "command-failed", "verification-failed", "interrupted", "internal"})
MAXIMUM_BACKUP_AGE = timedelta(days=8)
MAXIMUM_RUN_DURATION = timedelta(hours=6)
STATUS_SUBJECT = "backup-status.json"
RUN_SUBJECT = "backup"


@dataclass(frozen=True)
class BackupStatus:
    """The documented fields the check judges, each checked against its documented type or values; times are local and timezone-aware."""

    state: str
    last_attempt: datetime
    failing_app: str | None
    exit_category: str | None
    last_successful_backups: dict[str, datetime | None]


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Text erwartet")
    return value


def _one_of(value: object, allowed: frozenset[str]) -> str:
    text = _text(value)
    if text not in allowed:
        raise ValueError("Wert nicht dokumentiert")
    return text


def _moment(value: object) -> datetime:
    moment = datetime.fromisoformat(_text(value))
    if moment.tzinfo is None:
        raise ValueError("Zeit ohne Zeitzone")
    return moment.astimezone()


def _optional[T](value: object, parse: Callable[[object], T]) -> T | None:
    return None if value is None else parse(value)


def parse_status(text: str) -> BackupStatus:
    document = json.loads(text)
    if document["schema"] != STATUS_SCHEMA:
        raise ValueError(f"Schema {document['schema']!r}")
    return BackupStatus(
        state=_one_of(document["state"], STATES),
        last_attempt=_moment(document["last_attempt"]),
        failing_app=_optional(document["failing_app"], _text),
        exit_category=_optional(document["exit_category"], lambda value: _one_of(value, EXIT_CATEGORIES)),
        last_successful_backups={name: _optional(record["last_successful_backup"], _moment) for name, record in document["apps"].items()},
    )


def _run_findings(status: BackupStatus, now: datetime) -> list[Finding]:
    subject = status.failing_app or RUN_SUBJECT
    if status.state == "failed":
        return [Finding("backup-failed", subject, f"der letzte Lauf ist fehlgeschlagen ({status.exit_category})")]
    if status.state == "running" and now - status.last_attempt > MAXIMUM_RUN_DURATION:
        return [Finding("backup-crashed", RUN_SUBJECT, f"läuft seit {status.last_attempt:%d.%m.%Y %H:%M}, erlaubt sind 6 Stunden")]
    return []


def _staleness_findings(status: BackupStatus, now: datetime) -> list[Finding]:
    findings = []
    for app, last_good in sorted(status.last_successful_backups.items()):
        if last_good is None:
            findings.append(Finding("backup-stale", app, "noch nie erfolgreich gesichert"))
        elif now - last_good > MAXIMUM_BACKUP_AGE:
            findings.append(Finding("backup-stale", app, f"letzte Sicherung {last_good:%d.%m.%Y}, {(now - last_good).days} Tage alt, erlaubt 8"))
    return findings


def _unreadable(why: str) -> list[Finding]:
    return [Finding("backup-status-unreadable", STATUS_SUBJECT, why)]


def _read_regular_file(status_file: Path) -> str:
    """A symlink in the file's place is refused, not followed, and so is anything that is not a regular file; it is opened
    without blocking, because a FIFO planted there would otherwise wait forever for a writer."""
    descriptor = os.open(status_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with open(descriptor, encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("keine reguläre Datei")
        return handle.read()


def _read(status_file: Path) -> tuple[BackupStatus | None, str]:
    """The status, or the reason it cannot be judged."""
    try:
        return parse_status(_read_regular_file(status_file)), ""
    except FileNotFoundError:
        return None, "Statusdatei fehlt, die Sicherung lief noch nie"
    except OSError as error:
        return None, f"Statusdatei unlesbar: {error.strerror}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, f"Statusdatei ungültig oder Schema nicht {STATUS_SCHEMA}"


def inspect(status_file: Path, now: datetime) -> tuple[list[Finding], str]:
    """What `home check` reads of the backup's status; findings and their report."""
    status, problem = _read(status_file)
    if status is None:
        return _unreadable(problem), render_findings_report("backup", now, [], _unreadable(problem))
    local_now = now.astimezone()
    findings = _run_findings(status, local_now) + _staleness_findings(status, local_now)
    status_lines = [f"Letzter Lauf: {status.state}, {status.last_attempt:%d.%m.%Y %H:%M}", f"Apps: {len(status.last_successful_backups)}"]
    return findings, render_findings_report("backup", now, status_lines, findings)
