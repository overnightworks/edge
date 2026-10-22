"""`home check`'s logs check: reports the age and the driver of every log store the 30-day bound covers.

It reads the container log drivers, Docker's default driver, the journal's oldest entry and the rsyslog files, and
changes nothing. No message text is read: the journal's age comes from the boot list's timestamps. A store it cannot
read is a finding of its own, never a pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from home_tools.report import findings_count, markdown_table
from home_tools.system_step import LOG_RETENTION_DAYS
from home_tools.tidy_home import CommandRunner

LOG_DIRECTORY = Path("/var/log")
RSYSLOG_FILE_NAMES = ("syslog", "auth.log", "kern.log", "mail.log", "mail.err", "ufw.log")
WANTED_DRIVER = "journald"
JSON_FILE_DRIVER = "json-file"
MICROSECONDS_PER_SECOND = 1_000_000

DOCKER_CONTAINER_IDS = ["docker", "ps", "-aq"]
DOCKER_DEFAULT_DRIVER = ["docker", "info", "--format", "{{.LoggingDriver}}"]
INSPECT_FORMAT = '{{.Name}}\t{{.HostConfig.LogConfig.Type}}\t{{index .Config.Labels "com.docker.compose.project"}}'
JOURNAL_BOOTS = ["journalctl", "--list-boots", "-o", "json"]


@dataclass(frozen=True)
class LogFinding:
    rule: str
    subject: str
    detail: str

    @property
    def identity(self) -> str:
        return f"{self.rule}\t{self.subject}"


@dataclass(frozen=True)
class LogReading:
    """What the check saw of the stores; the findings and the lines that state each store's driver or age."""

    findings: list[LogFinding]
    stand: list[str]


def _container_findings(run: CommandRunner) -> LogReading:
    identifiers = run(DOCKER_CONTAINER_IDS)
    if identifiers is None:
        return LogReading([LogFinding("docker-unreadable", "docker", "`docker ps -a` lässt sich nicht ausführen")], [])
    ids = identifiers.split()
    listing = run(["docker", "inspect", "--format", INSPECT_FORMAT, *ids]) if ids else ""
    if listing is None:
        return LogReading([LogFinding("docker-unreadable", "docker", "`docker inspect` lässt sich nicht ausführen")], [])
    findings = []
    for line in listing.splitlines():
        name, driver, project = line.split("\t")
        if driver == JSON_FILE_DRIVER:
            where = f"Projekt {project}" if project else "ohne Compose-Projekt"
            findings.append(LogFinding("container-json-file", name.removeprefix("/"), f"Treiber {JSON_FILE_DRIVER}, {where}"))
    return LogReading(findings, [f"Container: {len(ids)}, davon {len(findings)} mit {JSON_FILE_DRIVER}"])


def _default_driver_findings(run: CommandRunner) -> LogReading:
    output = run(DOCKER_DEFAULT_DRIVER)
    if output is None:
        return LogReading([LogFinding("docker-unreadable", "docker", "`docker info` lässt sich nicht ausführen")], [])
    driver = output.strip()
    stand = [f"Docker-Standardtreiber: {driver}"]
    if driver == WANTED_DRIVER:
        return LogReading([], stand)
    return LogReading([LogFinding("docker-default-driver", driver, f"Standardtreiber ist {driver}, erwartet {WANTED_DRIVER}")], stand)


def _oldest_journal_entry(boots_json: str) -> datetime | None:
    """The earliest `first_entry` (microseconds since the epoch) of any boot; None when no boot is listed."""
    boots = json.loads(boots_json)
    return min((datetime.fromtimestamp(boot["first_entry"] / MICROSECONDS_PER_SECOND) for boot in boots), default=None)


def _journal_findings(run: CommandRunner, now: datetime) -> LogReading:
    output = run(JOURNAL_BOOTS)
    try:
        oldest = None if output is None else _oldest_journal_entry(output)
    except (ValueError, KeyError, TypeError):
        oldest = None
    if oldest is None:
        return LogReading([LogFinding("journal-unreadable", "journal", "`journalctl --list-boots` liefert kein lesbares Ergebnis")], [])
    age = now - oldest
    stand = [f"Journal: ältester Eintrag {oldest:%d.%m.%Y}, {age.days} Tage alt"]
    if age <= timedelta(days=LOG_RETENTION_DAYS):
        return LogReading([], stand)
    detail = f"ältester Eintrag {oldest:%d.%m.%Y}, {age.days} Tage alt, erlaubt {LOG_RETENTION_DAYS}"
    return LogReading([LogFinding("journal-age", "journal", detail)], stand)


def _rsyslog_findings(log_directory: Path) -> LogReading:
    """A file is an rsyslog file by its name, or by its name plus a rotation suffix; its content is never opened."""
    found = sorted(
        path
        for path in (log_directory.iterdir() if log_directory.is_dir() else [])
        if any(path.name == name or path.name.startswith(f"{name}.") for name in RSYSLOG_FILE_NAMES)
    )
    findings = [LogFinding("rsyslog-file", str(path), "rsyslog schreibt hier mit; das Journal ist der einzige Speicher") for path in found]
    return LogReading(findings, [f"rsyslog-Dateien in {log_directory}: {len(found)}"])


def collect(run: CommandRunner, now: datetime, log_directory: Path) -> LogReading:
    readings = [_container_findings(run), _default_driver_findings(run), _journal_findings(run, now), _rsyslog_findings(log_directory)]
    findings: list[LogFinding] = []
    stand: list[str] = []
    for reading in readings:
        findings.extend(reading.findings)
        stand.extend(reading.stand)
    return LogReading(_without_repeats(findings), stand)


def _without_repeats(findings: list[LogFinding]) -> list[LogFinding]:
    """A tool that cannot run fails each of its reads alike; one finding says it once."""
    return list({finding.identity: finding for finding in findings}.values())


def render_report(reading: LogReading, now: datetime) -> str:
    lines = [f"# home logs {now:%d.%m.%Y %H:%M} | {findings_count(len(reading.findings))}", ""]
    lines += [f"- {line}" for line in reading.stand]
    lines.append("")
    if reading.findings:
        lines += markdown_table(["Regel", "Gegenstand", "Befund"], [[f.rule, f.subject, f.detail] for f in reading.findings])
    else:
        lines.append("Keine Funde.")
    return "\n".join(lines) + "\n"


def inspect(run: CommandRunner, now: datetime, log_directory: Path) -> tuple[list[LogFinding], str]:
    """What `home check` reads of the log stores; findings and their report."""
    reading = collect(run, now, log_directory)
    return reading.findings, render_report(reading, now)
