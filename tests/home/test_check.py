"""Tests of `home check`; the #390 mail tests name the cells of the item's scenario matrix in their docstrings."""

from __future__ import annotations

import email.message
import email.policy
import json
import os
import select
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import ALLOWED_PLATFORM_INSPECT_FORMAT, CANARY_INSPECT_LINE, HOME_README, INSPECT_CANARY, PASSWORD_CANARY, FakeProbe, RunnerWithRealGitAndQuality, Sandbox, SentMail, MailStub, set_up_mail

from home_tools import backup_check, check, cli, logs_check, mail
from home_tools.check import WAIT_FOR_CLICK_SCRIPT
from home_tools.tidy_home import Layout, run_command

READY_DEADLINE_SECONDS = 30  # a pass returns at once; the deadline only bounds a hang under load


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME so that `home setup` can install the hook there."""
    return furnish_clean_home(sandbox.home)


class RunnerWithCleanLogs(RunnerWithRealGitAndQuality):
    """The logs check reads docker and journalctl; this machine runs no container and its journal is young."""

    container_listing = ""
    platform_listing = ""  # what `docker inspect` answers the platform check: name, state, health, exit code, "compose" or nothing per line

    def __call__(self, argv: Sequence[str]) -> str | None:
        if argv[0] == "journalctl":
            return f'[{{"index":0,"first_entry":{int(time.time() * 1_000_000)}}}]'
        if argv[:2] == ["docker", "inspect"] and argv[3] != logs_check.INSPECT_FORMAT:
            self.calls.append(list(argv))
            return self.platform_listing if argv[3] == ALLOWED_PLATFORM_INSPECT_FORMAT else CANARY_INSPECT_LINE
        if argv[0] == "docker" and (argv[1] in {"ps", "inspect"} or "{{.LoggingDriver}}" in argv):
            self.calls.append(list(argv))
            return {"ps": "id0\n" if self.container_listing or self.platform_listing else "", "info": "journald\n", "inspect": self.container_listing or "/quiet-1\tjournald\t\n"}[argv[1]]
        if argv[0] == "sh":
            self.calls.append(list(argv))
            return run_command(argv)
        return super().__call__(argv)


@pytest.fixture(autouse=True)
def quiet_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform check looks at this machine's reboot flag; the tests look at a path that holds none."""
    monkeypatch.setattr(check, "REBOOT_REQUIRED_FILE", tmp_path / "reboot-required")


def write_backup_status(home: Path, state: str = "ok", last_backup: datetime | None = None) -> None:
    last_backup = last_backup or datetime.now().astimezone()
    document = {"schema": 2, "state": state, "last_attempt": last_backup.isoformat(), "failing_app": None, "exit_category": None}
    document["apps"] = {"songmaker": {"last_successful_backup": last_backup.isoformat()}}
    status_file = home / backup_check.STATUS_FILE
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(document))


@pytest.fixture(autouse=True)
def good_backup(clean_home: Path) -> None:
    """These tests are about the check's reporting and news; a backup that ran today keeps its check clean (its own tests: test_backup_check.py)."""
    write_backup_status(clean_home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


@pytest.fixture(autouse=True)
def hook_installed(sandbox: Sandbox, clean_home: Path) -> None:
    """These tests are about the check's reporting and news; a set-up hook keeps its check clean (its own tests: test_hook_check.py)."""
    assert cli.main(["setup", "--apply"], home=clean_home) == 0


def home_check(home: Path, runner, probe: FakeProbe | None = None) -> int:
    return cli.main(["check"], home=home, run=runner, probe=probe or FakeProbe())


def test_a_first_run_with_findings_writes_one_report_and_sends_one_notification(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.startswith("# home check ")
    assert "| pasted-images |" in report
    assert len(runner.notifications) == 1
    assert runner.notifications[0] == report.splitlines()[0].removeprefix("# ")
    assert report.splitlines()[0].endswith("| 2 Funde")  # the image is also unknown to ~/README.md
    assert runner.notification_bodies[0] == f"2 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"


def test_the_logs_check_runs_inside_home_check_and_its_finding_is_in_the_report(clean_home, runner) -> None:
    runner.container_listing = "/songmaker-api-1\tjson-file\tsongmaker\n"

    code = home_check(clean_home, runner)

    assert code == 1
    assert "container-json-file | songmaker-api-1" in (clean_home / ".local/state/home/check-report.md").read_text()


def test_a_container_that_turns_unhealthy_is_reported_and_notified_and_leaves_the_report_when_fixed(clean_home, runner) -> None:
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tcompose\n"
    home_check(clean_home, runner)
    runner.platform_listing = "/edge-step-ca-1\trunning\thealthy\t0\tcompose\n"

    code = home_check(clean_home, runner)

    assert code == 0
    assert "container-unhealthy" not in Layout(clean_home).check_findings_file.read_text()
    assert runner.notification_bodies[0].startswith("1 neu, 0 weg.")


def test_neither_report_nor_findings_nor_notification_carry_more_than_the_named_inspect_fields(clean_home, runner) -> None:
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tcompose\n"

    home_check(clean_home, runner)

    layout = Layout(clean_home)
    carried = [layout.check_report_file.read_text(), layout.check_findings_file.read_text(), *runner.notifications, *runner.notification_bodies]
    assert not any(INSPECT_CANARY in text for text in carried)
    assert "container-unhealthy | edge-step-ca-1" in carried[0]


def test_many_new_findings_give_one_notification_with_their_count(clean_home, runner) -> None:
    runner.platform_listing = "".join(f"/edge-{n}-1\texited\t\t1\tcompose\n" for n in range(4))

    home_check(clean_home, runner)

    assert len(runner.notifications) == 1
    assert runner.notification_bodies[0].startswith("4 neu, 0 weg.")


def test_a_stale_backup_is_in_the_daily_report(clean_home, runner) -> None:
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))

    code = home_check(clean_home, runner)

    assert code == 1
    assert "backup-stale | songmaker" in Layout(clean_home).check_report_file.read_text()


def test_a_pending_reboot_notifies_once_and_does_not_nag(clean_home, runner, tmp_path) -> None:
    (tmp_path / "reboot-required").touch()
    home_check(clean_home, runner)
    os.utime(tmp_path / "reboot-required", (1.0, 1.0))

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_the_notification_waits_detached_for_a_click_that_opens_the_report(clean_home, leave_pasted_image, runner) -> None:
    """#29 rule 1 (action and report path) and rule 2 (detached: systemd-run takes no waiting option)."""
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    (waiter,) = runner.notification_calls
    assert waiter[: waiter.index("sh")] == ["systemd-run", "--user", "--collect", "-p", "Type=notify", "-p", "NotifyAccess=all"]
    assert waiter[-1] == str(Layout(clean_home).check_report_file)
    assert '--action="open=Bericht öffnen"' in waiter[waiter.index("-c") + 1]


class WaiterStubs:
    """Runs the detached unit's script against stub tools on an otherwise empty PATH and records what they were asked."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.log = directory / "log"
        (directory / "stdbuf").symlink_to(shutil.which("stdbuf"))
        self.install("systemd-notify", 'echo "ready $*" >> "$LOG"; echo ready')
        self.install("xdg-open", 'echo "open $*" >> "$LOG"')

    def install(self, name: str, body: str) -> None:
        tool = self.directory / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)

    def notify_send_prints(self, *lines: str) -> None:
        self.install("notify-send", "; ".join(f"echo {line}" for line in ("42", *lines)))

    def notify_send_buffers_its_output_until_it_exits(self) -> None:
        """Like libnotify's notify-send: its stdio block-buffers a pipe unless stdbuf makes it line-buffered;
        it exits only once a line arrives on stdin."""
        (self.directory / "sed").symlink_to(shutil.which("sed"))
        self.install("notify-send", "{ echo 42; read -r _; echo open; } | sed -n p")

    def _command(self, report_file: Path) -> tuple[list[str], dict[str, str]]:
        argv = ["/bin/sh", "-c", WAIT_FOR_CLICK_SCRIPT, "sh", "Titel", "Text", str(report_file)]
        return argv, {"PATH": str(self.directory), "LOG": str(self.log)}

    def start(self, report_file: Path) -> subprocess.Popen[str]:
        argv, env = self._command(report_file)
        return subprocess.Popen(argv, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def run(self, report_file: Path) -> subprocess.CompletedProcess[str]:
        argv, env = self._command(report_file)
        return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)

    @property
    def logged(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def stubs(tmp_path: Path) -> WaiterStubs:
    return WaiterStubs(tmp_path)


def test_a_click_on_the_action_opens_the_report(stubs, tmp_path) -> None:
    """#29 rule 1: the click on 'Bericht öffnen' opens the report file, after the unit reported delivery."""
    stubs.notify_send_prints("open")

    result = stubs.run(tmp_path / "report.md")

    assert result.returncode == 0
    assert stubs.logged == ["ready --ready", f"open {tmp_path / 'report.md'}"]


def test_a_dismissed_notification_opens_nothing_and_ends_cleanly(stubs, tmp_path) -> None:
    """#29 rule 1: no click, no xdg-open; the unit does not end as failed."""
    stubs.notify_send_prints()

    result = stubs.run(tmp_path / "report.md")

    assert result.returncode == 0
    assert stubs.logged == ["ready --ready"]


def test_a_notify_send_that_delivers_nothing_fails_the_unit_before_it_is_ready(stubs, tmp_path) -> None:
    """#29 rule 3: a missing or failing notify-send fails the start, so the check reports NOTIFY_FAILED."""
    result = stubs.run(tmp_path / "report.md")

    assert result.returncode != 0
    assert stubs.logged == []


def test_a_notify_send_that_prints_id_zero_and_fails_does_not_become_ready(stubs, tmp_path) -> None:
    """#29 rule 3: ID 0 is what a failed show prints; it is no delivery, so the start fails and NOTIFY_FAILED follows."""
    stubs.install("notify-send", "echo 0; exit 1")

    result = stubs.run(tmp_path / "report.md")

    assert result.returncode != 0
    assert stubs.logged == []


def test_the_unit_is_ready_while_the_notification_is_still_open(stubs, tmp_path) -> None:
    """#29 rule 2: the check never waits for the click, even though notify-send holds its ID until the click."""
    stubs.notify_send_buffers_its_output_until_it_exits()
    waiter = stubs.start(tmp_path / "report.md")
    try:
        ready_before_click, _, _ = select.select([waiter.stdout], [], [], READY_DEADLINE_SECONDS)
        logged_before_click = stubs.logged
    finally:
        waiter.communicate("\n", timeout=READY_DEADLINE_SECONDS)

    assert ready_before_click
    assert logged_before_click == ["ready --ready"]


def test_unchanged_findings_never_notify_again_even_as_they_age(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home, age_days=30)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home, age_days=40)

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_an_unchanged_finding_about_a_secret_name_that_is_not_utf8_notifies_once(clean_home, runner) -> None:
    secrets_dir = os.fsencode(Layout(clean_home).secrets_dir)
    os.close(os.open(secrets_dir + b"/bad\xffname", os.O_CREAT | os.O_WRONLY, 0o600))
    assert home_check(clean_home, runner) == 1

    code = home_check(clean_home, runner)

    assert code == 1
    assert len(runner.notifications) == 1


def test_a_changed_set_of_findings_notifies_again(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    leave_pasted_image(clean_home / "git")

    home_check(clean_home, runner)

    assert len(runner.notifications) == 2
    assert runner.notification_bodies[1].startswith("2 neu, 0 weg.")  # the image, and git/ litter


def test_a_finding_that_goes_while_another_stays_notifies_it_as_gone(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home)
    second = leave_pasted_image(clean_home / "git")
    home_check(clean_home, runner)
    second.unlink()

    home_check(clean_home, runner)

    assert runner.notification_bodies[1].startswith("0 neu, 2 weg.")


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
    assert "notify-send fehlgeschlagen" in capsys.readouterr().err
    assert len(runner.notifications) == 2


def test_secrets_findings_join_the_one_report_and_the_one_notification(clean_home, leave_pasted_image, runner) -> None:
    leave_pasted_image(clean_home / "git")
    (clean_home / ".secrets" / "gitlab").unlink()

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    assert code == 1
    assert report.splitlines()[0].endswith("| 3 Funde")
    assert "# home tidy " in report
    assert "# home secrets " in report
    assert "| missing | gitlab |" in report
    assert runner.notification_bodies == [f"3 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"]


def break_tidy(home: Path) -> Path:
    (home / "git").mkdir(exist_ok=True)
    (home / "git" / "stray.txt").write_text("litter")
    (home / ".secrets" / "gitlab").unlink()  # a secrets finding that must stay visible
    return home / "git"


class ProbeThatCannotStart(FakeProbe):
    """`gh` is there but not executable: starting the probe raises, and the secrets checker breaks on it."""

    def __call__(self, argv: Sequence[str]) -> int | None:
        raise PermissionError(argv[0])


@contextmanager
def broken_tidy(home: Path) -> Iterator[FakeProbe]:
    locked = break_tidy(home)
    locked.chmod(0)
    try:
        yield FakeProbe()
    finally:
        locked.chmod(0o700)


@contextmanager
def broken_secrets(home: Path) -> Iterator[FakeProbe]:
    shutil.copy(home / "README.md", home / "Eingefügtes Bild.png")
    os.utime(home / "Eingefügtes Bild.png", (0, 0))  # a tidy finding that must stay visible
    yield ProbeThatCannotStart()


def home_check_with_a_secrets_check_that_breaks(home: Path, runner) -> None:
    home_check(home, runner, ProbeThatCannotStart())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.parametrize(
    ("break_one", "broken", "still_visible"),
    [(broken_tidy, "tidy", "| missing | gitlab |"), (broken_secrets, "secrets", "| pasted-images |")],
    ids=["tidy breaks", "secrets breaks"],
)
def test_one_failing_checker_leaves_the_others_findings_in_the_report_and_exits_3(
    clean_home, runner, capsys, break_one: Callable[[Path], AbstractContextManager[FakeProbe]], broken: str, still_visible: str
) -> None:
    with break_one(clean_home) as probe:
        code = home_check(clean_home, runner, probe)

    report = Layout(clean_home).check_report_file.read_text()
    error = capsys.readouterr().err
    assert code == 3
    assert f"| fehlgeschlagen: {broken}" in report.splitlines()[0]
    assert f"Prüfung {broken} fehlgeschlagen (PermissionError)" in report
    assert still_visible in report
    assert error.startswith("home: unerwarteter Fehler (CheckFailed): ")
    assert error.count("\n") == 1
    assert len(runner.notifications) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_broken_checker_counts_its_unobserved_findings_as_neither_new_nor_gone(clean_home, runner) -> None:
    locked = break_tidy(clean_home)
    home_check(clean_home, runner)
    locked.chmod(0)
    try:
        broken_codes = [home_check(clean_home, runner) for _ in range(2)]
    finally:
        locked.chmod(0o700)
    home_check(clean_home, runner)

    unchanged = f"0 neu, 0 weg. Bericht: {Layout(clean_home).check_report_file}"
    assert broken_codes == [3, 3]
    # the locked ~/git also stops the hook check from scanning
    assert runner.notifications[1].endswith("| 1 Fund | fehlgeschlagen: tidy, hook")
    assert runner.notification_bodies[0] != unchanged
    assert runner.notification_bodies[1:] == [unchanged, unchanged]  # one for the break, one for the recovery


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_broken_checker_that_recovers_into_a_clean_home_notifies_the_recovery(clean_home, runner) -> None:
    home_check_with_a_secrets_check_that_breaks(clean_home, runner)

    code = home_check(clean_home, runner)

    assert code == 0
    assert len(runner.notifications) == 2
    assert runner.notifications[1].endswith("| 0 Funde")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_recovery_into_a_clean_home_whose_notification_fails_is_sent_again(clean_home, runner) -> None:
    home_check_with_a_secrets_check_that_breaks(clean_home, runner)
    runner.notify_delivers = False
    home_check(clean_home, runner)
    runner.notify_delivers = True

    home_check(clean_home, runner)

    assert len(runner.notifications) == 3
    assert runner.notifications[2].endswith("| 0 Funde")


def test_an_invalid_manifest_leaves_the_tidy_findings_in_the_report_and_exits_2(
    clean_home, leave_pasted_image, runner, use_manifest, capsys
) -> None:
    leave_pasted_image(clean_home)
    manifest = use_manifest('[[secret]]\nname = "e"\n')

    code = home_check(clean_home, runner)

    report = Layout(clean_home).check_report_file.read_text()
    error = capsys.readouterr().err
    assert code == 2
    assert "| fehlgeschlagen: secrets" in report.splitlines()[0]
    assert "Prüfung secrets fehlgeschlagen (ManifestError)" in report
    assert "| pasted-images |" in report
    assert error.startswith(f"home: {manifest}: ")
    assert error.count("\n") == 1


def memory_text(text: str) -> Callable[[Path], None]:
    return lambda path: path.write_text(text)


@pytest.mark.parametrize(
    ("plant_memory", "refusal"),
    [
        (memory_text('["tidy\\t'), "JSONDecodeError"),
        (memory_text('["tidy\\tleaked-secret"]'), "ValueError"),
        (memory_text('{"tidy": 1}'), "ValueError"),
        (os.mkfifo, "ValueError"),
    ],
    ids=["cut off", "not a finding identity", "not a list", "a fifo that never gets a writer"],
)
def test_a_corrupt_findings_memory_still_leaves_the_report_written(
    clean_home, runner, capsys, plant_memory: Callable[[Path], None], refusal: str
) -> None:
    layout = Layout(clean_home)
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    plant_memory(layout.check_findings_file)

    code = home_check(clean_home, runner)

    assert code == 3
    assert capsys.readouterr().err.startswith(f"home: unerwarteter Fehler ({refusal})")
    assert layout.check_report_file.read_text().startswith("# home check ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_symlinked_findings_memory_is_refused_and_never_carried_into_the_state(clean_home, runner, tmp_path) -> None:
    layout = Layout(clean_home)
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text('["tidy\\tpasted-images\\tleaked-secret"]')
    layout.check_findings_file.symlink_to(outside)
    locked = break_tidy(clean_home)
    locked.chmod(0)
    try:
        code = home_check(clean_home, runner)
    finally:
        locked.chmod(0o700)

    state_files = [path for path in layout.state_dir.iterdir() if path.is_file() and not path.is_symlink()]
    assert code == 3
    assert state_files
    assert not any("leaked-secret" in path.read_text() for path in state_files)


@pytest.fixture
def mail_stub(clean_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MailStub:
    return set_up_mail(clean_home, tmp_path, monkeypatch)


def message_of(sent: SentMail) -> email.message.EmailMessage:
    return email.message_from_string(sent.stdin, policy=email.policy.default)  # type: ignore[return-value]


def test_the_first_news_is_mailed_once_with_the_summary_as_subject_and_the_password_read_by_msmtp(clean_home, leave_pasted_image, runner, mail_stub, capsys) -> None:
    """#390 C1, E2, T1 and C9, R1: one mail per news, and the password canary reaches no output channel."""
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    layout = Layout(clean_home)
    (sent,) = mail_stub.sent
    news = message_of(sent)
    assert news["Subject"] == runner.notifications[0]
    assert (news["From"], news["To"]) == ("home@example.test", "me@example.test")
    assert sent.arguments[-2:] == ["--", "me@example.test"]
    assert sent.password == PASSWORD_CANARY
    captured = capsys.readouterr()
    outputs = [captured.out, captured.err, layout.check_report_file.read_text(), layout.check_findings_file.read_text(), sent.stdin, *sent.arguments]
    assert not any(PASSWORD_CANARY in text for text in outputs)


def test_the_mail_names_counts_and_new_platform_and_backup_findings_but_never_a_file_or_the_report(clean_home, leave_pasted_image, runner, mail_stub) -> None:
    """#390 C2, E2: the body holds counts and new platform and backup lines, no tidy file name and no report text."""
    leave_pasted_image(clean_home)
    runner.platform_listing = "/edge-step-ca-1\trunning\tunhealthy\t0\tcompose\n"
    write_backup_status(clean_home, last_backup=datetime.now().astimezone() - timedelta(days=9))

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[0]).get_content()
    assert body.startswith(f"{runner.notification_bodies[0].partition('.')[0]}.")
    assert f"Bericht: {Layout(clean_home).check_report_file}" in body
    assert "  platform: 1" in body
    assert "container-unhealthy | edge-step-ca-1" in body
    assert "backup-stale | songmaker" in body
    assert "Eingefügtes Bild" not in body
    assert "# home" not in body


def test_two_hundred_new_findings_name_twenty_and_count_the_rest(clean_home, runner, mail_stub) -> None:
    """#390 C2, E2: 200 new lines name 20 and count the rest."""
    runner.platform_listing = "".join(f"/edge-{n:03}-1\texited\t\t1\tcompose\n" for n in range(200))

    home_check(clean_home, runner)

    body = message_of(mail_stub.sent[0]).get_content()
    assert body.count("container-exited |") == 20
    assert "… und 180 weitere" in body


def test_a_subject_with_shell_syntax_or_a_newline_reaches_the_mail_verbatim_and_runs_nothing(tmp_path, monkeypatch) -> None:
    """#390 C10, E2: shell syntax and a newline in a subject reach stdin verbatim and run nothing."""
    monkeypatch.chdir(tmp_path)
    stub = set_up_mail(tmp_path / "home", tmp_path, monkeypatch)
    hostile = "platform\tcontainer-dead\t$(touch PWNED) `touch PWNED`\nBcc: x@example.test"
    account = mail.load_account(Layout(tmp_path / "home"))
    body = check.mail_body(frozenset({hostile}), None, tmp_path / "report.md")

    run_command(mail.sendmail_command(account, Layout(tmp_path / "home").secrets_dir, mail.compose(account, "s", body)))

    (sent,) = stub.sent
    assert "$(touch PWNED) `touch PWNED`\n" in message_of(sent).get_content()
    assert not list(tmp_path.glob("PWNED"))


def test_unchanged_findings_and_a_clean_home_send_no_mail(clean_home, leave_pasted_image, runner, mail_stub) -> None:
    """#390 C3, E1: an unchanged set and a clean home send no mail."""
    image = leave_pasted_image(clean_home)
    home_check(clean_home, runner)
    home_check(clean_home, runner)
    image.unlink()
    (clean_home / "README.md").write_text(HOME_README)

    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 1


def test_a_recovered_check_is_mailed(clean_home, runner, mail_stub) -> None:
    """#390 C3, E1: a recovered check is news and is mailed."""
    layout = Layout(clean_home)
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.check_findings_file.write_text('["tidy\\tcheck-failed"]')

    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 1


def test_a_missing_account_sends_nothing_and_says_nothing(clean_home, leave_pasted_image, runner, capsys) -> None:
    """#390 C5, N1: an absent account.toml sends nothing and prints nothing."""
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    assert not [call for call in runner.calls if call[0] == "sh"]
    assert "msmtp" not in capsys.readouterr().err


INVALID_ACCOUNTS = {
    "string port": 'port = "587"\n',
    "a password key": f'port = 587\npassword = "{PASSWORD_CANARY}"\n',
    "no TOML": f"this is {PASSWORD_CANARY}\n",
}


@pytest.mark.parametrize("extra", INVALID_ACCOUNTS.values(), ids=INVALID_ACCOUNTS)
def test_an_invalid_account_sends_nothing_names_the_problem_and_keeps_the_news_for_the_next_run(clean_home, leave_pasted_image, runner, mail_stub, capsys, extra) -> None:
    """#390 C6, V1: an invalid account sends nothing, names the problem on stderr and keeps the set."""
    leave_pasted_image(clean_home)
    (Layout(clean_home).secrets_dir / "smtp" / "account.toml").write_text('host = "smtp.example.test"\nuser = "home@example.test"\nto = "me@example.test"\n' + extra)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    errors = capsys.readouterr().err
    assert mail_stub.sent == []
    assert errors.count("home: keine Mail, account.toml:") == 2
    assert PASSWORD_CANARY not in errors
    assert len(runner.notifications) == 2


def test_switched_off_notifications_send_no_mail_and_no_popup_and_advance(clean_home, leave_pasted_image, runner, mail_stub) -> None:
    """#390 C7, F1: enabled = false sends no mail and no popup, and the set advances."""
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[notify]\nenabled = false\n")
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    assert (mail_stub.sent, runner.notifications) == ([], [])
    assert Layout(clean_home).check_findings_file.exists()


@pytest.mark.parametrize("session_active", [True, False], ids=["session", "logged-out"])
@pytest.mark.parametrize("mail_state", ["ok", "failing", "absent"])
def test_the_news_advances_only_when_every_set_up_channel_delivered(clean_home, leave_pasted_image, runner, mail_stub, capsys, session_active, mail_state) -> None:
    """#390 C8, F1: the set advances iff every set-up channel delivered; zero channels keep it, silently."""
    leave_pasted_image(clean_home)
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
    assert ("msmtp fehlgeschlagen (smtp.example.test:587)" in errors) == (mail_state == "failing")
    assert "me@example.test" not in errors
    assert (errors == "") == (advanced or not any(channels))


def test_a_failed_send_keeps_the_news_and_the_next_run_mails_it(clean_home, leave_pasted_image, runner, mail_stub) -> None:
    """#390 C4, F1: a failed send keeps the news and the next run mails it."""
    leave_pasted_image(clean_home)
    mail_stub.exit_with(1)
    home_check(clean_home, runner)
    mail_stub.exit_with(0)

    home_check(clean_home, runner)
    home_check(clean_home, runner)

    assert len(mail_stub.sent) == 2


def test_a_failing_send_prints_its_failure_line_but_never_the_password(clean_home, leave_pasted_image, runner, mail_stub, capsys) -> None:
    """#390 C9, R1: a failed send reports on stderr without the password canary."""
    leave_pasted_image(clean_home)
    mail_stub.exit_with(1)

    home_check(clean_home, runner)

    captured = capsys.readouterr()
    assert "msmtp fehlgeschlagen (smtp.example.test:587)" in captured.err
    assert PASSWORD_CANARY not in captured.out + captured.err


def test_an_account_path_that_is_a_directory_is_an_invalid_account_and_keeps_the_news(clean_home, leave_pasted_image, runner, mail_stub, capsys) -> None:
    """#390 C6, V1, F1: a present but unusable account.toml is reported, not taken for an unset mail channel."""
    leave_pasted_image(clean_home)
    account = Layout(clean_home).secrets_dir / "smtp" / "account.toml"
    account.unlink()
    account.mkdir()

    home_check(clean_home, runner)

    assert "home: keine Mail, account.toml:" in capsys.readouterr().err
    assert not Layout(clean_home).check_findings_file.exists()


def test_a_missing_msmtp_is_one_stderr_line_and_the_news_is_kept(clean_home, leave_pasted_image, runner, mail_stub, tmp_path, monkeypatch, capsys) -> None:
    """#390 C4, F1: a missing msmtp is one stderr line and the set stays."""
    leave_pasted_image(clean_home)
    only_sh = tmp_path / "only-sh"
    only_sh.mkdir()
    (only_sh / "sh").symlink_to("/bin/sh")
    monkeypatch.setenv("PATH", str(only_sh))

    code = home_check(clean_home, runner)

    assert code == 1
    assert "msmtp fehlgeschlagen" in capsys.readouterr().err
    assert not Layout(clean_home).check_findings_file.exists()


@pytest.mark.parametrize(("port", "starttls"), [(2525, "on"), (465, "off")])
def test_the_port_of_the_account_decides_between_starttls_and_implicit_tls(clean_home, leave_pasted_image, runner, tmp_path, monkeypatch, port, starttls) -> None:
    """#390 C1, T1: ports 2525 and 465 choose STARTTLS on or off."""
    stub = set_up_mail(clean_home, tmp_path, monkeypatch, port=port)
    leave_pasted_image(clean_home)

    home_check(clean_home, runner)

    assert f"--tls-starttls={starttls}" in stub.sent[0].arguments


def test_a_home_with_a_space_and_a_quote_still_lets_msmtp_read_the_password(tmp_path, leave_pasted_image, runner, use_manifest, furnish_clean_home, monkeypatch, capsys) -> None:
    """#390 C9, R1: a home path with a space and a quote still lets msmtp read the password, which appears in no channel."""
    odd_home = furnish_clean_home(tmp_path / "ho me's")
    stub = set_up_mail(odd_home, tmp_path, monkeypatch)
    leave_pasted_image(odd_home)

    home_check(odd_home, runner)

    (sent,) = stub.sent
    assert sent.password == PASSWORD_CANARY
    captured = capsys.readouterr()
    assert PASSWORD_CANARY not in captured.out + captured.err + "".join(sent.arguments) + sent.stdin
