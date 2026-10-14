from __future__ import annotations

from pathlib import Path

from home_tools import cli
from home_tools.tidy_home import Layout


def home_check(home: Path, runner) -> int:
    return cli.main(["check"], home=home, run=runner)


def test_a_first_run_with_findings_writes_one_report_and_sends_one_notification(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.startswith("# home check ")
    assert "| pasted-images |" in report
    assert len(runner.notifications) == 1
    assert runner.notifications[0] == report.splitlines()[0].removeprefix("# ")


def test_unchanged_findings_never_notify_again_even_as_they_age(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home, age_days=30)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home, age_days=40)

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_a_changed_set_of_findings_notifies_again(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home / "git")

    home_check(clean_home, runner)

    assert len(runner.notifications) == 2


def test_a_clean_home_exits_0_without_notifying(clean_home, runner) -> None:
    code = home_check(clean_home, runner)

    assert code == 0
    assert runner.notifications == []
    assert "Nichts zu tun." in Layout(clean_home).check_report_file.read_text()


def test_findings_that_return_after_a_clean_run_notify_again(clean_home, leave_pasted_image, runner) -> None:
    image = leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    image.unlink()
    home_check(clean_home, runner)
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    assert len(runner.notifications) == 2


def test_switched_off_notifications_stay_silent(clean_home, leave_pasted_image, runner) -> None:
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[notify]\nenabled = false\n")
    leave_pasted_image(clean_home)

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == []


def test_a_failed_notification_is_reported_and_sent_again_on_the_next_run(clean_home, leave_pasted_image, runner, capsys) -> None:
    leave_pasted_image(clean_home)
    runner.notify_delivers = False

    failed_code = home_check(clean_home, runner)
    runner.notify_delivers = True
    home_check(clean_home, runner)

    assert failed_code == 1
    assert "notify-send" in capsys.readouterr().err
    assert len(runner.notifications) == 2
