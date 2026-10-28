"""The detached unit `hal check` starts for a notification click, run against stub tools (#29 rules 1 to 3)."""

from __future__ import annotations

import select
import shutil
import subprocess
from pathlib import Path

import pytest

from hal.workstation.check import WAIT_FOR_CLICK_SCRIPT

READY_DEADLINE_SECONDS = 30  # a pass returns at once; the deadline only bounds a hang under load


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
