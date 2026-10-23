"""`home check`'s backup check: reports a failed or dead backup run and every app whose last good backup is too old.

It reads one file, the status the backup writes (schema 2, documented in docs/backup.md), and only the fields named
here; it never opens the backups, their repository or their password. `edge_backup` stays unimported because the two
packages are independent. A status file that is missing, unreadable or of another schema is a finding of its own,
never a pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from home_tools.report import Finding, render_findings_report

STATUS_SCHEMA = 2
MAXIMUM_BACKUP_AGE = timedelta(days=8)
MAXIMUM_RUN_DURATION = timedelta(hours=6)
STATUS_SUBJECT = "backup-status.json"
RUN_SUBJECT = "backup"


@dataclass(frozen=True)
class BackupStatus:
    """The documented fields the check judges; times are timezone-aware."""

    state: str
    last_attempt: datetime
    failing_app: str | None
    exit_category: str | None
    last_successful_backups: dict[str, datetime | None]


def _time(text: str | None) -> datetime | None:
    moment = None if text is None else datetime.fromisoformat(text)
    if moment is not None and moment.tzinfo is None:
        raise ValueError("Zeit ohne Zeitzone")
    return moment


def parse_status(text: str) -> BackupStatus:
    document = json.loads(text)
    if document["schema"] != STATUS_SCHEMA:
        raise ValueError(f"Schema {document['schema']!r}")
    last_attempt = _time(document["last_attempt"])
    if last_attempt is None:
        raise ValueError("last_attempt fehlt")
    return BackupStatus(
        state=str(document["state"]),
        last_attempt=last_attempt,
        failing_app=document["failing_app"],
        exit_category=document["exit_category"],
        last_successful_backups={name: _time(record["last_successful_backup"]) for name, record in document["apps"].items()},
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


def _read(status_file: Path) -> tuple[BackupStatus | None, str]:
    """The status, or the reason it cannot be judged."""
    try:
        return parse_status(status_file.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "Statusdatei fehlt, die Sicherung lief noch nie"
    except OSError as error:
        return None, f"Statusdatei unlesbar: {error.strerror}"
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, f"Statusdatei nicht lesbar oder Schema nicht {STATUS_SCHEMA}"


def inspect(status_file: Path, now: datetime) -> tuple[list[Finding], str]:
    """What `home check` reads of the backup's status; findings and their report."""
    status, problem = _read(status_file)
    if status is None:
        return _unreadable(problem), render_findings_report("backup", now, [], _unreadable(problem))
    local_now = now.astimezone()
    findings = _run_findings(status, local_now) + _staleness_findings(status, local_now)
    status_lines = [f"Letzter Lauf: {status.state}, {status.last_attempt:%d.%m.%Y %H:%M}", f"Apps: {len(status.last_successful_backups)}"]
    return findings, render_findings_report("backup", now, status_lines, findings)
