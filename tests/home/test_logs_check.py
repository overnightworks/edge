"""`home check`'s logs check (overnightworks/edge#167 cells C1 and C2) against fake docker and journalctl."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from home_tools import logs_check
from home_tools.system_step import LOG_RETENTION_DAYS

NOW = datetime(2026, 10, 22, 12, 0)


def boots_json(*first_entries: datetime) -> str:
    return json.dumps([{"index": -i, "first_entry": int(entry.timestamp() * 1_000_000)} for i, entry in enumerate(first_entries)])


class FakeMachine:
    """Answers the three tools the check reads; an answer of None is a tool that cannot run."""

    def __init__(self) -> None:
        self.containers: list[tuple[str, str, str]] = []  # name, log driver, compose project
        self.default_driver: str | None = "journald"
        self.docker_runs = True
        self.boots: str | None = boots_json(NOW - timedelta(days=3))

    def __call__(self, argv: Sequence[str]) -> str | None:
        if argv[0] == "journalctl":
            return self.boots
        if not self.docker_runs:
            return None
        if argv[1] == "ps":
            return "".join(f"id{i}\n" for i, _ in enumerate(self.containers))
        if argv[1] == "info":
            return None if self.default_driver is None else self.default_driver + "\n"
        return "".join(f"/{name}\t{driver}\t{project}\n" for name, driver, project in self.containers)


@pytest.fixture
def machine() -> FakeMachine:
    return FakeMachine()


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "var-log"
    directory.mkdir()
    return directory


def findings(machine: FakeMachine, log_directory: Path) -> dict[tuple[str, str], str]:
    found, _report = logs_check.inspect(machine, NOW, log_directory)
    return {(f.rule, f.subject): f.detail for f in found}


def test_a_machine_whose_logs_all_go_to_the_journal_has_no_findings(machine, log_directory) -> None:
    machine.containers = [("app-web-1", "journald", "app"), ("one-off", "none", "")]

    assert findings(machine, log_directory) == {}


def test_a_json_file_container_is_a_finding_naming_it_and_its_project(machine, log_directory) -> None:
    machine.containers = [("songmaker-api-1", "json-file", "songmaker"), ("edge-ca-1", "journald", "edge"), ("lonely", "json-file", "")]

    found = findings(machine, log_directory)

    assert set(found) == {("container-json-file", "songmaker-api-1"), ("container-json-file", "lonely")}
    assert "songmaker" in found[("container-json-file", "songmaker-api-1")]
    assert "Compose" in found[("container-json-file", "lonely")]


def test_a_default_driver_other_than_journald_is_a_finding(machine, log_directory) -> None:
    machine.default_driver = "json-file"

    assert set(findings(machine, log_directory)) == {("docker-default-driver", "json-file")}


@pytest.mark.parametrize(("days_old", "is_finding"), [(LOG_RETENTION_DAYS, False), (LOG_RETENTION_DAYS + 1, True)])
def test_a_journal_entry_older_than_the_retention_is_a_finding(machine, log_directory, days_old, is_finding) -> None:
    machine.boots = boots_json(NOW - timedelta(days=3), NOW - timedelta(days=days_old), NOW - timedelta(days=10))

    assert (("journal-age", "journal") in findings(machine, log_directory)) is is_finding


def test_the_report_states_the_journal_age_and_the_default_driver_without_a_finding(machine, log_directory) -> None:
    _found, report = logs_check.inspect(machine, NOW, log_directory)

    assert "3 Tage alt" in report
    assert "Docker-Standardtreiber: journald" in report


def test_an_rsyslog_file_and_its_archives_are_findings_by_name(machine, log_directory) -> None:
    for name in ["syslog", "syslog.1", "auth.log.2.gz", "dpkg.log"]:
        (log_directory / name).write_text("content that is never read")

    assert {subject for rule, subject in findings(machine, log_directory) if rule == "rsyslog-file"} == {
        str(log_directory / name) for name in ["syslog", "syslog.1", "auth.log.2.gz"]
    }


@pytest.mark.parametrize("unreadable", ["no output", "not json", "wrong shape", "no boots"])
def test_a_journal_that_cannot_be_read_is_a_finding_of_its_own(machine, log_directory, unreadable) -> None:
    machine.boots = {"no output": None, "not json": "denied", "wrong shape": '[{"index": 0}]', "no boots": "[]"}[unreadable]

    assert set(findings(machine, log_directory)) == {("journal-unreadable", "journal")}


def test_a_docker_that_cannot_be_run_is_one_finding_not_a_pass(machine, log_directory) -> None:
    machine.docker_runs = False

    assert set(findings(machine, log_directory)) == {("docker-unreadable", "docker")}


def test_a_missing_log_directory_has_no_rsyslog_files(machine, tmp_path) -> None:
    assert findings(machine, tmp_path / "absent") == {}
