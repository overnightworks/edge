"""Tests of the disk-low finding in `hal check` (#460): the mail, the report and an unreadable filesystem."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support.workstation_support import RunnerWithCleanLogs, Sandbox, home_check, message_of, restarting_containers

from hal.workstation.layout import Layout


pytestmark = pytest.mark.usefixtures("quiet_pc")

ACTION_TITLE = "PC check: action needed – Services"


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME."""
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


def test_a_nearly_full_disk_is_mailed_as_one_action_under_services_without_a_figure(clean_home, runner, mail_stub, fake_disk) -> None:
    """#460 D1, D2, D3: the sentence and step, no GB figure in the mail, the figures in the report."""
    fake_disk.free_share = 0.09

    home_check(clean_home, runner, disk=fake_disk)

    news = message_of(mail_stub.sent[0])
    assert news["Subject"] == ACTION_TITLE
    assert "1. The disk has less than 10 % free. (new)\n   Next step: Tell an agent: \"The disk is nearly full, please look\".\n" in news.get_content()
    assert "Services: Action needed" in news.get_content()
    assert "GB" not in news.get_content()
    assert "| disk-low | / | 90 GB von 1000 GB frei (9 %) |" in Layout(clean_home).check_report_file.read_text()


def test_a_disk_that_stays_low_sends_no_second_mail_and_one_that_clears_is_mailed_as_done(clean_home, runner, mail_stub, fake_disk) -> None:
    """#460 D1 and #415 Z4, Z5: once on appearing, once on going, however much less is free in between."""
    for free_share in (0.09, 0.07, 0.30):
        fake_disk.free_share = free_share
        home_check(clean_home, runner, disk=fake_disk)

    first, done = mail_stub.sent
    assert "less than 10 % free" in message_of(first).get_content()
    assert message_of(done)["Subject"] == "PC check: all well"
    assert "Done\n\n- The disk has at least 10 % free again." in message_of(done).get_content()


def test_a_nearly_full_disk_and_a_restarting_container_are_two_items_and_one_area_line(clean_home, runner, mail_stub, fake_disk) -> None:
    """#460 D2 (cell E-4): a low disk and a restarting container are two numbered items under one area line."""
    fake_disk.free_share = 0.09
    restarting_containers(runner, "edge-a")

    home_check(clean_home, runner, disk=fake_disk)

    body = message_of(mail_stub.sent[0]).get_content()
    assert "1. " in body
    assert "2. " in body
    assert "less than 10 % free" in body
    assert "edge-a keeps restarting" in body
    assert body.count("Services: Action needed") == 1


def test_a_disk_that_cannot_be_read_breaks_the_platform_check_and_leaves_the_other_findings(clean_home, runner, fake_disk) -> None:
    """#460 Decision, #340 S5: no rule of its own for a filesystem that cannot be read."""
    fake_disk.unreadable = True
    (clean_home / ".secrets" / "gitlab").unlink()

    code = home_check(clean_home, runner, disk=fake_disk)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 3
    assert "check platform failed (OSError)" in report
    assert "| missing | gitlab |" in report
