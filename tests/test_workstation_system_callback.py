"""The packaged `hal_events` callback under real Ansible, run through the system step's own runner without root."""

from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from workstation_support import StubSudo

from hal.workstation import system_step
from hal.workstation.setup import ANSIBLE_SOURCE
from hal.workstation.system_step import PlaybookRun

FAILING_ASSERT = """
    - ansible.builtin.assert:
        that: [false]
        fail_msg: "the file names no tunnel"
"""
RESCUE_THAT_FAILS = """
    - block:
        - ansible.builtin.fail:
            msg: the block failed
      rescue:
        - ansible.builtin.fail:
            msg: the rescue refused
"""
RESCUE_THAT_HOLDS = """
    - block:
        - ansible.builtin.fail:
            msg: the block failed
      rescue:
        - ansible.builtin.debug:
            msg: rescued
"""
IGNORED_FAILURE = """
    - ansible.builtin.fail:
        msg: ignored
      ignore_errors: true
"""
CHANGED_SKIPPED_AND_HANDLED = """
    - ansible.builtin.command: "true"
      changed_when: true
      notify: handler
    - ansible.builtin.debug:
        msg: skipped
      when: false
    - ansible.builtin.meta: flush_handlers
"""
HANDLER = """
  handlers:
    - name: handler
      ansible.builtin.command: "true"
      changed_when: true
"""


def run_scratch_play(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tasks: str, handlers: str = "") -> PlaybookRun:
    StubSudo(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    play = tmp_path / "play.yml"
    play.write_text(f"- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:{tasks}{handlers}")
    return system_step.run_playbook_as_root([str(Path(sys.executable).parent / "ansible-playbook"), str(play), "--inventory", "localhost,"])


@pytest.mark.parametrize(
    ("tasks", "handlers", "expected"),
    [
        (CHANGED_SKIPPED_AND_HANDLED, HANDLER, PlaybookRun(True, 2)),
        (FAILING_ASSERT, "", PlaybookRun(False, 0, "the file names no tunnel")),
        (RESCUE_THAT_FAILS, "", PlaybookRun(False, 0, "the rescue refused")),
        (IGNORED_FAILURE, "", PlaybookRun(True, 0)),
        (RESCUE_THAT_HOLDS, "", PlaybookRun(True, 0, "the block failed")),
    ],
    ids=["changed skipped and handled", "failed assert", "rescue that fails", "ignored failure", "rescued failure"],
)
def test_the_callback_reports_counts_and_the_message_of_the_last_failed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tasks: str, handlers: str, expected: PlaybookRun
) -> None:
    """M18: the packaged cfg selects the callback; the reader gets the counts and the last failure."""
    assert run_scratch_play(tmp_path, monkeypatch, tasks, handlers) == expected


def test_ansibles_own_text_still_shows_and_no_event_line_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    run_scratch_play(tmp_path, monkeypatch, FAILING_ASSERT)

    shown = capfd.readouterr().out
    assert "PLAY RECAP" in shown
    assert system_step.EVENT_MARK not in shown


def test_the_ansible_core_in_the_dev_group_is_the_one_hal_installs() -> None:
    assert f"ansible-core=={version('ansible-core')}" == ANSIBLE_SOURCE
