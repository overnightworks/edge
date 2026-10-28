"""`hal check`'s backup check (overnightworks/edge#340 C5, C7, I1, I3, S3, R1) against status files in a temporary directory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hal.workstation import backup_check

NOW = datetime(2026, 10, 23, 12, 0).astimezone()


def status_document(**overrides) -> dict:
    document = {
        "schema": 2,
        "state": "ok",
        "phase": "check",
        "failing_phase": None,
        "failing_app": None,
        "exit_category": None,
        "last_attempt": (NOW - timedelta(hours=9)).isoformat(),
        "apps": {"songmaker": {"last_snapshot_id": "abc", "last_successful_backup": (NOW - timedelta(days=1)).isoformat(), "last_successful_restore_test": None}},
    }
    return {**document, **overrides}


@pytest.fixture
def status_file(tmp_path: Path) -> Path:
    path = tmp_path / backup_check.STATUS_FILE
    path.parent.mkdir(parents=True)
    return path


def home_of(status_file: Path) -> Path:
    return status_file.parents[len(backup_check.STATUS_FILE.parts) - 1]


def found(status_file: Path) -> dict[tuple[str, str], str]:
    findings, _report = backup_check.inspect(home_of(status_file), NOW)
    return {(f.rule, f.subject): f.detail for f in findings}


def test_a_recent_successful_backup_is_clean(status_file) -> None:
    status_file.write_text(json.dumps(status_document()))

    assert found(status_file) == {}


def test_a_failed_run_is_a_finding_for_the_app_it_names(status_file) -> None:
    status_file.write_text(json.dumps(status_document(state="failed", failing_app="songmaker", exit_category="command-failed")))

    assert set(found(status_file)) == {("backup-failed", "songmaker")}


def test_a_failed_run_that_belongs_to_no_app_is_a_finding_for_the_backup(status_file) -> None:
    status_file.write_text(json.dumps(status_document(state="failed", exit_category="not-ready")))

    assert set(found(status_file)) == {("backup-failed", "backup")}


@pytest.mark.parametrize(("running_hours", "expected"), [(5, set()), (7, {("backup-crashed", "backup")})])
def test_a_run_still_running_after_six_hours_is_crashed(status_file, running_hours, expected) -> None:
    status_file.write_text(json.dumps(status_document(state="running", last_attempt=(NOW - timedelta(hours=running_hours)).isoformat())))

    assert set(found(status_file)) == expected


@pytest.mark.parametrize(("age_days", "expected"), [(7, set()), (9, {("backup-stale", "songmaker")})])
def test_a_backup_older_than_eight_days_is_stale_per_app(status_file, age_days, expected) -> None:
    apps = {"songmaker": {"last_successful_backup": (NOW - timedelta(days=age_days)).isoformat()}, "fresh": {"last_successful_backup": NOW.isoformat()}}
    status_file.write_text(json.dumps(status_document(apps=apps)))

    assert set(found(status_file)) == expected


def test_an_app_that_never_succeeded_is_stale(status_file) -> None:
    status_file.write_text(json.dumps(status_document(apps={"new-app": {"last_successful_backup": None}})))

    assert set(found(status_file)) == {("backup-stale", "new-app")}


def test_a_missing_status_file_is_a_finding(status_file) -> None:
    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"schema": 3}), json.dumps({"schema": 1}), json.dumps({"schema": 2})])
def test_a_corrupt_or_unknown_schema_status_file_is_a_finding(status_file, content) -> None:
    status_file.write_text(content)

    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}


def test_an_unreadable_status_file_is_a_finding(status_file) -> None:
    status_file.write_text(json.dumps(status_document()))
    status_file.chmod(0)

    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}


def test_the_age_of_a_stale_backup_is_not_part_of_its_identity(status_file) -> None:
    def identity_at(age_days: int) -> list[str]:
        status_file.write_text(json.dumps(status_document(apps={"songmaker": {"last_successful_backup": (NOW - timedelta(days=age_days)).isoformat()}})))
        return [f.identity for f in backup_check.inspect(home_of(status_file), NOW)[0]]

    assert identity_at(9) == identity_at(30)


def test_fields_beyond_the_documented_ones_never_reach_the_report(status_file) -> None:
    status_file.write_text(json.dumps(status_document(command_line="restic -p CANARY-PASSWORD", apps={"songmaker": {"last_successful_backup": NOW.isoformat(), "path": "/CANARY-PATH"}})))

    _findings, report = backup_check.inspect(home_of(status_file), NOW)

    assert "CANARY" not in report


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "CANARY-STATE"},
        {"state": 5},
        {"exit_category": "CANARY-CATEGORY"},
        {"exit_category": 7},
        {"failing_app": 7},
        {"last_attempt": None},
        {"last_attempt": 12},
        {"last_attempt": "CANARY-TIME"},
        {"last_attempt": "2026-10-23T06:00:00"},
        {"apps": ["CANARY-APP"]},
        {"apps": {"songmaker": "CANARY-ENTRY"}},
        {"apps": {"songmaker": {"last_successful_backup": 5}}},
        {"apps": {"songmaker": {"last_successful_backup": "2026-10-23T06:00:00"}}},
    ],
    ids=str,
)
def test_a_value_outside_its_documented_type_or_values_is_unreadable_and_never_reaches_the_report(status_file, overrides) -> None:
    status_file.write_text(json.dumps(status_document(**overrides)))

    findings, report = backup_check.inspect(home_of(status_file), NOW)

    assert [(f.rule, f.subject) for f in findings] == [("backup-status-unreadable", "backup-status.json")]
    assert "CANARY" not in report


def test_a_symlink_in_place_of_the_status_file_is_unreadable_not_followed(status_file, tmp_path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(status_document()))
    status_file.symlink_to(target)

    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}


def test_a_symlinked_parent_directory_is_unreadable_and_its_content_never_reaches_the_report(status_file, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "backup-status.json").write_text(json.dumps(status_document(failing_app="CANARY", state="failed", exit_category="internal")))
    status_file.parent.rmdir()
    status_file.parent.symlink_to(elsewhere)

    findings, report = backup_check.inspect(home_of(status_file), NOW)

    assert [(f.rule, f.subject) for f in findings] == [("backup-status-unreadable", "backup-status.json")]
    assert "CANARY" not in report


def test_a_missing_parent_directory_is_unreadable_and_is_not_created(status_file) -> None:
    status_file.parent.rmdir()

    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}
    assert not status_file.parent.exists()


@pytest.mark.parametrize("make", [os.mkfifo, os.mkdir], ids=["fifo", "directory"])
def test_a_status_file_that_is_not_a_regular_file_is_unreadable_and_does_not_block(status_file, make) -> None:
    make(status_file)

    assert set(found(status_file)) == {("backup-status-unreadable", "backup-status.json")}


def test_times_show_on_the_wall_clock_of_this_machine(status_file, berlin_time) -> None:
    attempt = datetime.fromisoformat("2026-10-23T06:48:00+00:00")
    status_file.write_text(json.dumps(status_document(last_attempt=attempt.isoformat(), state="ok")))

    _findings, report = backup_check.inspect(home_of(status_file), datetime(2026, 10, 23, 12, 0))

    assert "23.10.2026 08:48" in report
