from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from conftest import FakeProbe, RunnerWithRealGitAndQuality, Sandbox

from home_tools import cli
from home_tools.check import WAIT_FOR_CLICK_SCRIPT
from home_tools.tidy_home import Layout

READY_DEADLINE_SECONDS = 30  # a pass returns at once; the deadline only bounds a hang under load


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home, inside the sandbox's HOME so that `home setup` can install the hook there."""
    return furnish_clean_home(sandbox.home)


class RunnerWithCleanLogs(RunnerWithRealGitAndQuality):
    """The logs check reads docker and journalctl; this machine runs no container and its journal is young."""

    container_listing = ""

    def __call__(self, argv: Sequence[str]) -> str | None:
        if argv[0] == "journalctl":
            return f'[{{"index":0,"first_entry":{int(time.time() * 1_000_000)}}}]'
        if argv[0] == "docker" and (argv[1] in {"ps", "inspect"} or "{{.LoggingDriver}}" in argv):
            self.calls.append(list(argv))
            return {"ps": "id0\n" if self.container_listing else "", "info": "journald\n", "inspect": self.container_listing}[argv[1]]
        return super().__call__(argv)


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
