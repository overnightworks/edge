"""Tests of `hal check`; the mail tests name the cells of the scenario matrix in their docstrings."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from conftest import INSPECT_CANARY, PASSWORD_CANARY, REPORT_DISPLAY, TEST_MANIFEST, ProbeThatCannotStart, RunnerWithCleanLogs, Sandbox, home_check, message_of, no_container_restarts, restarting_containers, set_up_mail, write_backup_status, write_hal_config

from hal.workstation import check, mail
from hal.workstation.layout import Layout, run_command


pytestmark = pytest.mark.usefixtures("quiet_pc")


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME so that `hal setup` can install the hook there."""
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()

ACTION_TITLE = "PC check: action needed – Services"


def test_a_first_run_with_an_action_writes_one_report_and_sends_one_notification(clean_home, runner) -> None:
    """#415 Z1: the popup title is the state in a phrase, its body the sentences and the report."""
    restarting_containers(runner, "presentator-live")

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.startswith("# hal check ")
    assert "| container-restarting |" in report
    assert runner.notifications == [ACTION_TITLE]
    assert runner.notification_bodies == [f"presentator-live keeps restarting and is therefore unreachable.\nEverything in detail: {REPORT_DISPLAY}"]


def test_the_logs_check_runs_inside_home_check_and_its_finding_is_in_the_report(clean_home, runner) -> None:
    runner.container_listing = "/songmaker-api-1\tjson-file\tsongmaker\n"

    code = home_check(clean_home, runner)

    assert code == 1
    assert "container-json-file | songmaker-api-1" in (clean_home / ".local/state/hal/check-report.md").read_text()


def test_a_container_that_turns_unhealthy_is_reported_and_notified_and_leaves_the_report_when_fixed(clean_home, runner) -> None:
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tedge\n"
    home_check(clean_home, runner)
    runner.platform_listing = "/edge-step-ca-1\trunning\thealthy\t0\tedge\n"

    code = home_check(clean_home, runner)

    assert code == 0
    assert "container-unhealthy" not in Layout(clean_home).check_findings_file.read_text()
    assert runner.notifications == [ACTION_TITLE, "PC check: all well"]


def test_neither_report_nor_findings_nor_notification_carry_more_than_the_named_inspect_fields(clean_home, runner) -> None:
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tedge\n"

    home_check(clean_home, runner)

    layout = Layout(clean_home)
    carried = [layout.check_report_file.read_text(), layout.check_findings_file.read_text(), *runner.notifications, *runner.notification_bodies]
    assert not any(INSPECT_CANARY in text for text in carried)
    assert "container-unhealthy | edge" in carried[0]


def test_many_new_actions_give_one_notification_naming_each(clean_home, runner) -> None:
    """#415 Z4: several actions that appear together give one mail naming each."""
    runner.platform_listing = "".join(f"/edge-{n}-1\texited\t\t1\tproject-{n}\n" for n in range(4))

    home_check(clean_home, runner)

    assert len(runner.notifications) == 1
    assert runner.notification_bodies[0].count("stopped with an error.") == 4


def test_a_stale_backup_is_in_the_daily_report(clean_home, runner) -> None:
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))

    code = home_check(clean_home, runner)

    assert code == 1
    assert "backup-stale | songmaker" in Layout(clean_home).check_report_file.read_text()


def test_a_pending_reboot_is_a_note_and_never_notifies(clean_home, runner, tmp_path) -> None:
    """#415 Z4: a note that appears sends nothing."""
    (tmp_path / "reboot-required").touch()

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == []


def test_the_notification_waits_detached_for_a_click_that_opens_the_report(clean_home, runner) -> None:
    """#29 rule 1 (action and report path) and rule 2 (detached: systemd-run takes no waiting option)."""
    restarting_containers(runner)

    home_check(clean_home, runner)

    (waiter,) = runner.notification_calls
    assert waiter[: waiter.index("sh")] == ["systemd-run", "--user", "--collect", "-p", "Type=notify", "-p", "NotifyAccess=all"]
    assert waiter[-1] == str(Layout(clean_home).check_report_file)
    assert f'--action="{check.OPEN_REPORT_ACTION}"' in waiter[waiter.index("-c") + 1]


def test_an_action_that_stays_never_notifies_again_even_as_it_ages(clean_home, runner) -> None:
    """#415 Z5: a backup 9 days and then 10 days stale is the same item and mails once."""
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))
    home_check(clean_home, runner)
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=10))

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == ["PC check: action needed – Backup"]


def test_an_unchanged_finding_about_a_secret_name_that_is_not_utf8_is_remembered_and_never_notifies(clean_home, runner) -> None:
    """#415 Z5: an unchanged item is remembered and never mails again."""
    secrets_dir = os.fsencode(Layout(clean_home).secrets_dir)
    os.close(os.open(secrets_dir + b"/bad\xffname", os.O_CREAT | os.O_WRONLY, 0o600))
    assert home_check(clean_home, runner) == 1

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == []
    assert "unknown-file" in Layout(clean_home).check_findings_file.read_text()


def test_a_second_action_notifies_again_and_the_mail_title_names_both_areas(clean_home, runner) -> None:
    """#415 Z4: a second action that appears mails and the title names both areas."""
    restarting_containers(runner, "edge-a")
    home_check(clean_home, runner)
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))

    home_check(clean_home, runner)

    assert runner.notifications == [ACTION_TITLE, "PC check: action needed – Services, Backup"]


def test_an_action_that_goes_while_another_stays_notifies_the_one_that_stays(clean_home, runner) -> None:
    """#415 Z4: an action that goes mails even while another one stays."""
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))
    restarting_containers(runner, "edge-a")
    home_check(clean_home, runner)
    no_container_restarts(runner)

    home_check(clean_home, runner)

    assert runner.notifications == ["PC check: action needed – Services, Backup", "PC check: action needed – Backup"]


def test_a_clean_home_exits_0_without_notifying(clean_home, runner) -> None:
    code = home_check(clean_home, runner)

    assert code == 0
    assert runner.notifications == []
    assert Layout(clean_home).check_report_file.read_text().startswith("# hal check ")
    assert "| 0 findings" in Layout(clean_home).check_report_file.read_text().splitlines()[0]


def test_an_action_that_returns_after_a_clean_run_notifies_again(clean_home, runner) -> None:
    """#415 Z4: an action that comes back after a clean run mails again."""
    restarting_containers(runner, "edge-a")
    home_check(clean_home, runner)
    no_container_restarts(runner)
    home_check(clean_home, runner)
    restarting_containers(runner, "edge-a")

    home_check(clean_home, runner)

    assert len(runner.notifications) == 3


def test_switched_off_notifications_stay_silent(clean_home, runner) -> None:
    write_hal_config(clean_home, "[check]\nnotify = false\n")
    restarting_containers(runner)

    code = home_check(clean_home, runner)

    assert code == 1
    assert runner.notifications == []


def test_a_failed_notification_is_reported_and_sent_again_on_the_next_run(clean_home, runner, capsys) -> None:
    restarting_containers(runner)
    runner.notify_delivers = False

    failed_code = home_check(clean_home, runner)
    runner.notify_delivers = True
    home_check(clean_home, runner)

    assert failed_code == 1
    assert "popup failed: notify-send did not run; the next run reports again\n" in capsys.readouterr().err.splitlines(keepends=True)
    assert len(runner.notifications) == 2


def test_a_note_joins_the_report_but_sends_nothing(clean_home, runner) -> None:
    """#415 Z4: a missing secret is a note; the report keeps the numbers."""
    (clean_home / ".secrets" / "gitlab").unlink()

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.splitlines()[0].endswith("| 1 finding")
    assert "# hal secrets " in report
    assert "| missing | gitlab |" in report
    assert runner.notifications == []


def home_check_with_a_secrets_check_that_breaks(home: Path, runner) -> int:
    return home_check(home, runner, ProbeThatCannotStart())


def test_one_failing_checker_leaves_the_others_findings_in_the_report_and_exits_3(clean_home, runner, capsys) -> None:
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tedge\n"

    code = home_check_with_a_secrets_check_that_breaks(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    captured = capsys.readouterr()
    error = captured.err
    assert code == 3
    assert captured.out.splitlines() == [
        "  ! services        1 action       edge is running but reports being unwell",
        "  ✓ backup          ok",
        "  ✓ commit hook     ok",
        "  ✗ secrets         not checked    the check did not run · details in the report",
        "  ✓ server logs     ok",
        "  ✓ exposure        ok",
    ]
    assert "| failed: secrets" in report.splitlines()[0]
    assert "# secrets" in report.splitlines()
    assert "check secrets failed (PermissionError)" in report
    assert "container-unhealthy | edge" in report
    error_lines = error.splitlines()
    assert error_lines[0] == "error: 1 of 6 areas could not check · news sent"
    assert error_lines[1].startswith("next: ")
    assert "`" not in error_lines[1]
    assert not error_lines[1].endswith(".")
    assert len(error_lines) == 2
    assert len(runner.notifications) == 1


def test_a_broken_checker_counts_its_unobserved_findings_as_neither_new_nor_gone(clean_home, runner, mail_stub, use_manifest) -> None:
    use_manifest(TEST_MANIFEST.replace('kind = "token"', 'kind = "human-password"'))
    (clean_home / ".secrets" / "gitlab").unlink()
    home_check(clean_home, runner)
    broken_codes = [home_check_with_a_secrets_check_that_breaks(clean_home, runner) for _ in range(2)]
    home_check(clean_home, runner)

    password = "The backup password is missing; without it nothing can be restored."
    first, broke, recovered = (message_of(mail).get_content() for mail in mail_stub.sent)
    assert broken_codes == [3, 3]
    assert f"{password} (new)" in first
    assert f"{password} (new)" not in broke
    assert "The Secrets check did not run. (new)" in broke
    assert "Done\n\n- The Secrets check runs again." in recovered
    assert password not in recovered.split("Done")[1]


def test_a_broken_checker_that_recovers_into_a_clean_home_notifies_the_recovery(clean_home, runner) -> None:
    home_check_with_a_secrets_check_that_breaks(clean_home, runner)

    code = home_check(clean_home, runner)

    assert code == 0
    assert runner.notifications == ["PC check: action needed – Secrets", "PC check: all well"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_recovery_into_a_clean_home_whose_notification_fails_is_sent_again(clean_home, runner) -> None:
    home_check_with_a_secrets_check_that_breaks(clean_home, runner)
    runner.notify_delivers = False
    home_check(clean_home, runner)
    runner.notify_delivers = True

    home_check(clean_home, runner)

    assert len(runner.notifications) == 3
    assert runner.notifications[2] == "PC check: all well"


class RunnerWithEmptyDockerInfo(RunnerWithCleanLogs):
    """`docker info` prints nothing, so the logs check finds a default driver with an empty subject."""

    def __call__(self, argv: Sequence[str]) -> str | None:
        return "" if argv[:2] == ["docker", "info"] else super().__call__(argv)


def test_a_finding_without_a_subject_survives_the_memory_and_the_next_run_exits_as_its_findings_say(clean_home, capsys) -> None:
    runner = RunnerWithEmptyDockerInfo()
    layout = Layout(clean_home)

    first = home_check(clean_home, runner)
    remembered = layout.check_findings_file.read_text()
    second = home_check(clean_home, runner)

    assert (first, second) == (1, 1)
    assert "logs\\tdocker-default-driver\\t" in remembered
    assert "unexpected error" not in capsys.readouterr().err


def memory_text(text: str) -> Callable[[Path], None]:
    return lambda path: path.write_text(text)


@pytest.mark.parametrize(
    ("plant_memory", "refusal"),
    [
        (memory_text('["platform\\t'), "JSONDecodeError"),
        (memory_text('["platform\\tleaked-secret"]'), "ValueError"),
        (memory_text('["platform\\tcontainer-dead"]'), "ValueError"),
        (memory_text('["platform\\tno-such-rule\\tx"]'), "ValueError"),
        (memory_text('["platform\\tcheck-failed\\tx"]'), "ValueError"),
        (memory_text('{"platform": 1}'), "ValueError"),
        (os.mkfifo, "ValueError"),
    ],
    ids=["cut off", "not a finding identity", "a finding without the subject separator", "a rule without a level", "a failure marker with a subject", "not a list", "a fifo that never gets a writer"],
)
def test_a_corrupt_findings_memory_still_leaves_the_report_written(
    clean_home, runner, capsys, plant_memory: Callable[[Path], None], refusal: str
) -> None:
    layout = Layout(clean_home)
    layout.hal_state_dir.mkdir(parents=True, exist_ok=True)
    plant_memory(layout.check_findings_file)

    code = home_check(clean_home, runner)

    assert code == 3
    assert capsys.readouterr().err.startswith(f"error: unexpected error ({refusal})")
    assert layout.check_report_file.read_text().startswith("# hal check ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_symlinked_findings_memory_is_refused_and_never_carried_into_the_state(clean_home, runner, tmp_path) -> None:
    layout = Layout(clean_home)
    layout.hal_state_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text('["platform\\tcontainer-dead\\tleaked-secret"]')
    layout.check_findings_file.symlink_to(outside)
    code = home_check_with_a_secrets_check_that_breaks(clean_home, runner)

    state_files = [path for path in layout.hal_state_dir.iterdir() if path.is_file() and not path.is_symlink()]
    assert code == 3
    assert state_files
    assert not any("leaked-secret" in path.read_text() for path in state_files)


def test_the_first_news_is_mailed_once_with_the_title_as_subject_and_the_password_read_by_msmtp(clean_home, runner, mail_stub, capsys) -> None:
    """#390 C1, T1 and C9, R1 and #415 Z1: one mail per news, and the password canary reaches no output channel."""
    restarting_containers(runner)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    layout = Layout(clean_home)
    (sent,) = mail_stub.sent
    news = message_of(sent)
    assert news["Subject"] == ACTION_TITLE == runner.notifications[0]
    assert (news["From"], news["To"]) == ("home@example.test", "me@example.test")
    assert sent.arguments[-2:] == ["--", "me@example.test"]
    assert sent.password == PASSWORD_CANARY
    captured = capsys.readouterr()
    outputs = [captured.out, captured.err, layout.check_report_file.read_text(), layout.check_findings_file.read_text(), sent.stdin, *sent.arguments]
    assert not any(PASSWORD_CANARY in text for text in outputs)


@pytest.mark.parametrize("sees_notes", [True, False], ids=["with notes", "clean"])
def test_an_action_that_goes_is_mailed_as_done_and_the_title_is_the_new_state(clean_home, runner, mail_stub, sees_notes) -> None:
    """#415 Z4, mail C: the action goes and the PC is clean or only notes remain; one mail says so."""
    restarting_containers(runner, "presentator-live")
    if sees_notes:
        (clean_home / ".secrets" / "gitlab").unlink()
    home_check(clean_home, runner)
    no_container_restarts(runner)

    home_check(clean_home, runner)

    news = message_of(mail_stub.sent[1])
    assert news["Subject"] == ("PC check: all well – notes only" if sees_notes else "PC check: all well")
    assert "Done\n\n- presentator-live no longer keeps restarting." in news.get_content()
    assert "Action needed\n\n1." not in news.get_content()
    assert "keeps restarting and" not in news.get_content()


def test_a_missing_human_password_is_an_action_and_its_secret_name_stays_out_of_the_mail(clean_home, runner, mail_stub, use_manifest) -> None:
    """#415 Z3, Z6: the one missing secret that is an action says so without naming the secret."""
    use_manifest(TEST_MANIFEST.replace('kind = "token"', 'kind = "human-password"'))
    (clean_home / ".secrets" / "gitlab").unlink()

    home_check(clean_home, runner)

    news = message_of(mail_stub.sent[0])
    assert news["Subject"] == "PC check: action needed – Secrets"
    assert "1. The backup password is missing; without it nothing can be restored. (new)\n   Next step: Run `hal secrets`." in news.get_content()
    assert "gitlab" not in news.get_content()


def test_a_note_that_appears_or_goes_sends_no_mail(clean_home, runner, mail_stub) -> None:
    """#415 Z4: only notes change, so nothing is mailed."""
    secret = clean_home / ".secrets" / "gitlab"
    secret.rename(clean_home / "gitlab-aside")
    home_check(clean_home, runner)
    (clean_home / "gitlab-aside").rename(secret)

    home_check(clean_home, runner)

    assert mail_stub.sent == []


def test_a_restarting_container_that_then_exits_is_a_new_item(clean_home, runner, mail_stub) -> None:
    """#415 Z5: the rule is part of the identity, so restarting then exited mails again and marks the item new."""
    restarting_containers(runner, "edge-a")
    home_check(clean_home, runner)
    runner.platform_listing = "/edge-a-1\texited\t\t1\tedge-a\n"

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[1]).get_content()
    assert "1. edge-a stopped with an error. (new)" in body
    assert "Done" not in body


def test_twenty_one_actions_name_twenty_and_count_the_rest(clean_home, runner, mail_stub) -> None:
    """#415 Z7: 21 actions give 20 items, one more-line and the five area lines."""
    runner.platform_listing = "".join(f"/edge-{n:03}-1\texited\t\t1\tproject-{n:03}\n" for n in range(21))

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[0]).get_content()
    assert body.count("stopped with an error.") == 20
    assert "… and 1 more, see report" in body
    assert body.count("OK") == 5


def test_a_hostile_subject_never_reaches_the_mail_unless_the_rule_names_it_and_runs_nothing(tmp_path, monkeypatch) -> None:
    """#390 C10 and #415 Z6: shell syntax in a container name reaches stdin verbatim and runs nothing; a secret path appears nowhere."""
    monkeypatch.chdir(tmp_path)
    stub = set_up_mail(tmp_path / "home", tmp_path, monkeypatch)
    hostile_container = "platform\tcontainer-dead\t$(touch PWNED) `touch PWNED`\nBcc: x@example.test"
    secret_path = "secrets\tloose-mode\t~/.secrets/songmaker/token"
    account = mail.load_account(Layout(tmp_path / "home"))
    body = check.mail_body(frozenset({hostile_container, secret_path}), None, REPORT_DISPLAY, datetime(2026, 10, 26, 14, 54), check.MailFacts())

    run_command(mail.sendmail_command(account, Layout(tmp_path / "home").secrets_dir, mail.compose(account, "s", body)))

    (sent,) = stub.sent
    content = message_of(sent).get_content()
    assert "$(touch PWNED) `touch PWNED`\n" in content
    assert "songmaker" not in content
    assert not list(tmp_path.glob("PWNED"))


def test_unchanged_actions_and_a_home_that_was_never_in_trouble_send_no_second_mail_and_no_first(clean_home, runner, mail_stub) -> None:
    """#390 C3 and #415 Z4, Z5: an unchanged action mails once; a clean home mails nothing."""
    home_check(clean_home, runner)
    restarting_containers(runner)
    home_check(clean_home, runner)
    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 1


def test_the_report_and_the_findings_memory_land_in_the_hal_state_directory(clean_home, runner) -> None:
    restarting_containers(runner)

    home_check(clean_home, runner)

    hal_state = clean_home / ".local/state/hal"
    assert (hal_state / "check-report.md").is_file()
    assert (hal_state / "check-findings.txt").is_file()
    assert not (clean_home / ".local/state/home/check-report.md").exists()
    assert not (clean_home / ".local/state/home/check-findings.txt").exists()


def test_a_findings_memory_left_behind_in_the_old_directory_is_ignored_so_the_run_is_news_again(clean_home, runner, mail_stub) -> None:
    restarting_containers(runner)
    home_check(clean_home, runner)
    layout = Layout(clean_home)
    old_memory = clean_home / ".local/state/home/check-findings.txt"
    old_memory.parent.mkdir(parents=True, exist_ok=True)
    old_memory.write_text(layout.check_findings_file.read_text())
    layout.check_findings_file.unlink()

    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 2


def test_the_report_speaks_english_in_its_header_section_headers_and_table_head(clean_home, runner) -> None:
    restarting_containers(runner)

    home_check(clean_home, runner)

    lines = Layout(clean_home).check_report_file.read_text().splitlines()
    assert lines[0].endswith("| 1 finding")
    assert lines[0].startswith("# hal check ")
    assert any(line.startswith("# platform ") and line.endswith("| 1 finding") for line in lines)
    assert "| Rule | Subject | Finding |" in lines


def test_a_recovered_check_is_mailed(clean_home, runner, mail_stub) -> None:
    """#390 C3 and #415 Z4: a recovered check is an action that went and is mailed."""
    layout = Layout(clean_home)
    layout.hal_state_dir.mkdir(parents=True, exist_ok=True)
    layout.check_findings_file.write_text('["platform\\tcheck-failed"]')

    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 1
    assert "Done\n\n- The Services check runs again." in message_of(mail_stub.sent[0]).get_content()


def test_a_remembered_tidy_finding_is_dropped_without_a_mail_and_the_run_exits_0(clean_home, runner, mail_stub) -> None:
    """#486 D4: identities of the removed tidy check are neither news nor recovered, and the next write forgets them."""
    layout = Layout(clean_home)
    layout.hal_state_dir.mkdir(parents=True, exist_ok=True)
    layout.check_findings_file.write_text('["tidy\\tcheck-failed", "tidy\\tpasted-images\\tx.png"]')

    code = home_check(clean_home, runner)

    assert code == 0
    assert mail_stub.sent == []
    assert "tidy" not in layout.check_findings_file.read_text()


def test_a_missing_account_sends_nothing_and_says_nothing(clean_home, runner, capsys) -> None:
    """#390 C5, N1: an absent account.toml sends nothing and prints nothing."""
    restarting_containers(runner)

    home_check(clean_home, runner)

    assert not [call for call in runner.calls if call[0] == "sh"]
    assert "msmtp" not in capsys.readouterr().err


INVALID_ACCOUNTS = {
    "string port": 'port = "587"\n',
    "a password key": f'port = 587\npassword = "{PASSWORD_CANARY}"\n',
    "no TOML": f"this is {PASSWORD_CANARY}\n",
}


@pytest.mark.parametrize("extra", INVALID_ACCOUNTS.values(), ids=INVALID_ACCOUNTS)
def test_an_invalid_account_sends_nothing_names_the_problem_and_keeps_the_news_for_the_next_run(clean_home, runner, mail_stub, capsys, extra) -> None:
    """#390 C6, V1: an invalid account sends nothing, names the problem on stderr and keeps the set."""
    restarting_containers(runner)
    (Layout(clean_home).secrets_dir / "smtp" / "account.toml").write_text('host = "smtp.example.test"\nuser = "home@example.test"\nto = "me@example.test"\n' + extra)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    errors = capsys.readouterr().err
    assert mail_stub.sent == []
    assert errors.count("mail not sent: account.toml:") == 2
    assert PASSWORD_CANARY not in errors
    assert len(runner.notifications) == 2


def test_switched_off_notifications_send_no_mail_and_no_popup_and_advance(clean_home, runner, mail_stub) -> None:
    """notify = false sends no mail and no popup, and the set advances."""
    write_hal_config(clean_home, "[check]\nnotify = false\n")
    restarting_containers(runner)

    home_check(clean_home, runner)

    assert (mail_stub.sent, runner.notifications) == ([], [])
    assert Layout(clean_home).check_findings_file.exists()


@pytest.mark.parametrize("session_active", [True, False], ids=["session", "logged-out"])
@pytest.mark.parametrize("mail_state", ["ok", "failing", "absent"])
def test_the_news_advances_only_when_every_set_up_channel_delivered(clean_home, runner, mail_stub, capsys, session_active, mail_state) -> None:
    """#390 C8, F1: the set advances iff every set-up channel delivered; zero channels keep it, silently."""
    restarting_containers(runner)
    runner.session_active = session_active
    if mail_state == "failing":
        mail_stub.exit_with(1)
    if mail_state == "absent":
        (Layout(clean_home).secrets_dir / "smtp" / "account.toml").unlink()

    code = home_check(clean_home, runner)

    errors = capsys.readouterr().err
    channels = [session_active, mail_state != "absent"]
    advanced = any(channels) and mail_state != "failing"
    assert code == 1
    assert (len(runner.notifications), len(mail_stub.sent)) == (int(session_active), int(mail_state != "absent"))
    assert Layout(clean_home).check_findings_file.exists() == advanced
    assert ("mail failed: msmtp could not deliver via smtp.example.test:587; the next run reports again" in errors) == (mail_state == "failing")
    assert "me@example.test" not in errors
    assert (errors == "") == (advanced or not any(channels))


def test_a_failed_send_keeps_the_news_and_the_next_run_mails_it(clean_home, runner, mail_stub) -> None:
    """#390 C4, F1: a failed send keeps the news and the next run mails it."""
    restarting_containers(runner)
    mail_stub.exit_with(1)
    home_check(clean_home, runner)
    mail_stub.exit_with(0)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 2


def test_a_failing_send_prints_its_failure_line_but_never_the_password(clean_home, runner, mail_stub, capsys) -> None:
    """#390 C9, R1: a failed send reports on stderr without the password canary."""
    restarting_containers(runner)
    mail_stub.exit_with(1)

    home_check(clean_home, runner)

    captured = capsys.readouterr()
    assert "mail failed: msmtp could not deliver via smtp.example.test:587; the next run reports again" in captured.err
    assert PASSWORD_CANARY not in captured.out + captured.err


def test_an_account_path_that_is_a_directory_is_an_invalid_account_and_keeps_the_news(clean_home, runner, mail_stub, capsys) -> None:
    """#390 C6, V1, F1: a present but unusable account.toml is reported, not taken for an unset mail channel."""
    restarting_containers(runner)
    account = Layout(clean_home).secrets_dir / "smtp" / "account.toml"
    account.unlink()
    account.mkdir()

    home_check(clean_home, runner)

    assert "mail not sent: account.toml:" in capsys.readouterr().err
    assert not Layout(clean_home).check_findings_file.exists()


def test_a_missing_msmtp_is_one_stderr_line_and_the_news_is_kept(clean_home, runner, mail_stub, tmp_path, monkeypatch, capsys) -> None:
    """#390 C4, F1: a missing msmtp is one stderr line and the set stays."""
    restarting_containers(runner)
    only_sh = tmp_path / "only-sh"
    only_sh.mkdir()
    (only_sh / "sh").symlink_to("/bin/sh")
    monkeypatch.setenv("PATH", str(only_sh))

    code = home_check(clean_home, runner)

    assert code == 1
    assert "mail failed: msmtp could not deliver via" in capsys.readouterr().err
    assert not Layout(clean_home).check_findings_file.exists()


@pytest.mark.parametrize(("port", "starttls"), [(2525, "on"), (465, "off")])
def test_the_port_of_the_account_decides_between_starttls_and_implicit_tls(clean_home, runner, tmp_path, monkeypatch, port, starttls) -> None:
    """#390 C1, T1: ports 2525 and 465 choose STARTTLS on or off."""
    stub = set_up_mail(clean_home, tmp_path, monkeypatch, port=port)
    restarting_containers(runner)

    home_check(clean_home, runner)

    assert f"--tls-starttls={starttls}" in stub.sent[0].arguments


def test_a_home_with_a_space_and_a_quote_still_lets_msmtp_read_the_password(tmp_path, runner, use_manifest, furnish_clean_home, monkeypatch, capsys) -> None:
    """#390 C9, R1: a home path with a space and a quote still lets msmtp read the password, which appears in no channel."""
    odd_home = furnish_clean_home(tmp_path / "ho me's")
    stub = set_up_mail(odd_home, tmp_path, monkeypatch)
    restarting_containers(runner)

    home_check(odd_home, runner)

    (sent,) = stub.sent
    assert sent.password == PASSWORD_CANARY
    captured = capsys.readouterr()
    assert PASSWORD_CANARY not in captured.out + captured.err + "".join(sent.arguments) + sent.stdin


def test_the_mail_says_the_action_with_one_step_and_one_line_per_area_and_no_number(clean_home, runner, mail_stub, use_manifest) -> None:
    """#415 Z2, Z6: mail A; the secret and the counts stay in the report."""
    use_manifest(TEST_MANIFEST.replace('read_by = [".bashrc"]', 'read_by = ["hopin"]'))
    restarting_containers(runner, "presentator-live")
    (clean_home / ".secrets" / "gitlab").unlink()
    write_backup_status(clean_home, last_backup=local_afternoon(days_ago=0))

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[0]).get_content()
    stamp = body.split("How the PC stands (")[1].split(")")[0]
    assert body == f"""Action needed

1. presentator-live keeps restarting and is therefore unreachable. (new)
   Next step: Tell an agent: "presentator-live keeps restarting, please look".

How the PC stands ({stamp})

Services: Action needed
Backup: OK, last backup today 14:15
Commit protection: OK
Secrets: Note – Credentials for hopin are still missing
Server logs: OK
Exposure: OK

Notes are not urgent.
Everything in detail: {REPORT_DISPLAY}
"""


def local_afternoon(days_ago: int) -> datetime:
    return datetime.combine(date.today() - timedelta(days=days_ago), time(14, 15)).astimezone()


def mailed_area_lines(clean_home, runner, mail_stub) -> list[str]:
    restarting_containers(runner, "presentator-live")
    home_check(clean_home, runner)
    body = message_of(mail_stub.sent[0]).get_content()
    return body.split("\n\n")[3].splitlines()


@pytest.mark.parametrize(
    ("read_by", "line"),
    [
        ('["hopin"]', "Secrets: Note – Credentials for hopin are still missing"),
        ("[]", "Secrets: Note – Credentials are still missing"),
        ('[".bashrc", "git/gmail-cleanup", "SONAR_TOKEN in x"]', "Secrets: Note – Credentials are still missing"),
    ],
    ids=["names the app that reads it", "names nobody when the manifest has no reader", "names no file, path or secret"],
)
def test_a_missing_secret_names_the_app_that_reads_it_and_never_the_secret(clean_home, runner, mail_stub, use_manifest, read_by, line) -> None:
    """#415 Z2, Z6: the area line says whose credentials are missing from the manifest's read_by."""
    use_manifest(TEST_MANIFEST.replace('read_by = [".bashrc"]', f"read_by = {read_by}"))
    (clean_home / ".secrets" / "gitlab").unlink()

    assert line in mailed_area_lines(clean_home, runner, mail_stub)
    assert not {".bashrc", "gmail-cleanup", "SONAR_TOKEN", "gitlab"} & set(message_of(mail_stub.sent[0]).get_content().replace("/", " ").split())


def test_the_logs_note_reads_as_in_the_picture(clean_home, runner, mail_stub) -> None:
    """#415 Z2: the area line is the clause of the picture, lower case after the dash and without a final period."""
    runner.container_listing = "/songmaker-api-1\tjson-file\tsongmaker\n"

    assert "Server logs: Note – not yet deleted everywhere after 30 days" in mailed_area_lines(clean_home, runner, mail_stub)


@pytest.mark.parametrize(("days_ago", "said"), [(0, "today"), (1, "yesterday")], ids=["today", "yesterday"])
def test_the_backup_line_says_when_the_last_backup_succeeded(clean_home, runner, mail_stub, days_ago, said) -> None:
    """#415 Z2: a healthy backup area still shows the last success, read from the status the backup check reads."""
    write_backup_status(clean_home, last_backup=local_afternoon(days_ago))

    assert f"Backup: OK, last backup {said} 14:15" in mailed_area_lines(clean_home, runner, mail_stub)

