"""Tests of the failed hal jobs that `hal check` reports in its Services row."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import RunnerWithCleanLogs, Sandbox, home_check, message_of, no_container_restarts, restarting_containers, write_backup_status

from hal.workstation.layout import Layout


pytestmark = pytest.mark.usefixtures("quiet_pc")

ACTION_TITLE = "PC check: action needed – Services"


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME so that the setup step can install the hook there."""
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


def failed_hal_units(runner, *names: str) -> None:
    runner.failed_units = "".join(f"{name} loaded failed failed CANARY-DESC\n" for name in names)


def test_a_failed_hal_job_is_an_action_in_services_with_its_words_and_is_remembered_by_unit(clean_home, runner, mail_stub) -> None:
    """The Services row, the mail, the report and the memory name the unit and nothing else of it."""
    failed_hal_units(runner, "hal-backup.service")

    code = home_check(clean_home, runner)

    layout = Layout(clean_home)
    news = message_of(mail_stub.sent[0]).get_content()
    assert code == 1
    assert "hal-backup.service failed on its last run." in news
    assert 'Tell an agent: "hal-backup.service failed, please look".' in news
    assert "platform\\tjob-failed\\thal-backup.service" in layout.check_findings_file.read_text()
    assert "job-failed | hal-backup.service" in layout.check_report_file.read_text()
    assert runner.notifications == [ACTION_TITLE]
    assert "CANARY" not in news + layout.check_report_file.read_text() + layout.check_findings_file.read_text()


def test_a_job_that_stays_failed_never_notifies_again_and_a_reset_is_mailed_as_done(clean_home, runner, mail_stub) -> None:
    failed_hal_units(runner, "hal-backup.service")
    home_check(clean_home, runner)
    home_check(clean_home, runner)
    runner.failed_units = ""

    code = home_check(clean_home, runner)

    assert code == 0
    assert len(mail_stub.sent) == 2
    assert "Done\n\n- hal-backup.service no longer reports a failure." in message_of(mail_stub.sent[1]).get_content()


def test_two_failed_hal_jobs_are_one_services_row_counting_two_actions(clean_home, runner, capsys) -> None:
    failed_hal_units(runner, "hal-backup.service", "hal-check.timer")

    home_check(clean_home, runner)

    assert "! services        2 actions      hal-backup.service failed on its last run · +1 more" in capsys.readouterr().out


def test_a_failed_backup_unit_and_a_failed_backup_status_are_two_actions(clean_home, runner) -> None:
    write_backup_status(clean_home, state="failed")
    failed_hal_units(runner, "hal-backup.service")

    home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert "job-failed | hal-backup.service" in report
    assert "backup-failed" in report


def test_a_container_action_and_a_job_action_are_both_in_services_and_mailed_in_identity_order(clean_home, runner, mail_stub, capsys) -> None:
    restarting_containers(runner, "edge-a")
    failed_hal_units(runner, "hal-backup.service")

    home_check(clean_home, runner)

    news = message_of(mail_stub.sent[0]).get_content()
    assert "! services        2 actions" in capsys.readouterr().out
    assert "hal-backup.service failed on its last run." in news
    assert "edge-a" in news
    assert news.index("edge-a") < news.index("hal-backup.service failed")
    findings = Layout(clean_home).check_findings_file.read_text()
    assert "platform\\tcontainer-" in findings
    assert "platform\\tjob-failed\\t" in findings


def test_systemd_that_cannot_be_read_is_an_action_and_holds_back_container_recoveries(clean_home, runner, mail_stub) -> None:
    restarting_containers(runner, "edge-a")
    home_check(clean_home, runner)
    no_container_restarts(runner)
    runner.failed_units = None

    home_check(clean_home, runner)

    news = message_of(mail_stub.sent[-1]).get_content()
    assert "The check cannot read systemd; whether the jobs ran is unknown." in news
    assert "no longer keeps restarting" not in news


def test_a_logged_out_session_still_reads_the_failed_jobs(clean_home, runner) -> None:
    runner.session_active = False
    failed_hal_units(runner, "hal-check.service")

    home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert "job-failed | hal-check.service" in report
    assert "systemd-unreadable" not in report
