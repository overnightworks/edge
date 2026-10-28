"""The system step's Ansible runner: recap reading and the one sudo call around the play."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from home_tools import system_step
from home_tools.system_step import PlaybookRun

RECAP_LINE = "localhost                  : ok=9    changed={changed}    unreachable={unreachable}    failed={failed}    skipped=2"


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, RECAP_LINE.format(changed=3, unreachable=0, failed=0), PlaybookRun(True, 3)),
        (0, RECAP_LINE.format(changed=0, unreachable=0, failed=0), PlaybookRun(True, 0)),
        (2, RECAP_LINE.format(changed=1, unreachable=0, failed=1), PlaybookRun(False, 1)),
        (4, RECAP_LINE.format(changed=0, unreachable=1, failed=0), PlaybookRun(False, 0)),
        (1, "ERROR! the playbook could not be read", PlaybookRun(False, 0)),
    ],
    ids=["changed", "converged", "failed task", "unreachable", "no recap"],
)
def test_a_run_succeeds_only_with_exit_0_and_a_recap_without_failures(returncode: int, output: str, expected: PlaybookRun) -> None:
    """Rule 4 and rule 7: a run succeeds only when Ansible exits 0 and its recap counts no failed or unreachable host."""
    assert system_step.read_recap(returncode, f"PLAY RECAP ***\n{output}\n") == expected


def fake_ansible(tmp_path: Path, script: str) -> Path:
    playbook = tmp_path / "ansible-playbook"
    playbook.write_text(f"#!/bin/sh\n{script}\necho '{RECAP_LINE.format(changed=2, unreachable=0, failed=0)}'\n")
    playbook.chmod(0o755)
    return playbook


class StubSudo:
    """A `sudo` first on PATH: it writes one line per call (its argv, working directory and every ANSIBLE_* it received), then runs the rest.

    `-k` only forgets a ticket, so it is recorded and not executed.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forgetting_exit_code: int = 0) -> None:
        self.log = tmp_path / "sudo.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sudo = bin_dir / "sudo"
        sudo.write_text(
            "#!/bin/sh\n"
            f'echo "$* | $(pwd) | $(env | grep \'^ANSIBLE_\' | tr \'\\n\' \' \')" >> {self.log}\n'
            f'[ "$1" = "-k" ] && exit {forgetting_exit_code}\n'
            'exec "$@"\n'
        )
        sudo.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def calls(self) -> list[list[str]]:
        return [[part.strip() for part in line.split(" | ")] for line in self.log.read_text().splitlines()]


@pytest.fixture
def stub_sudo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubSudo:
    return StubSudo(tmp_path, monkeypatch)


def test_the_runner_shows_ansibles_output_and_reads_its_recap(tmp_path: Path, stub_sudo: StubSudo, capfd: pytest.CaptureFixture[str]) -> None:
    """The output reaches the terminal as it comes, and the run is judged by its recap."""
    playbook = fake_ansible(tmp_path, "echo 'TASK [Write edge files]'")

    run = system_step.run_playbook_as_root([str(playbook)])

    assert run == PlaybookRun(True, 2)
    assert "TASK [Write edge files]" in capfd.readouterr().out


def test_the_play_runs_under_sudo_in_its_own_directory_and_sudo_forgets_the_ticket_after_it(tmp_path: Path, stub_sudo: StubSudo) -> None:
    """Ruled E1: one sudo call runs the play; `sudo -k` follows it, so no ticket outlives `hal setup`."""
    playbook = fake_ansible(tmp_path, "true")

    system_step.run_playbook_as_root([str(playbook), "--diff"])

    play_call, forget_call = stub_sudo.calls()
    assert play_call[0] == f"env PYTHONDONTWRITEBYTECODE=1 {playbook} --diff"
    assert play_call[1] == str(system_step.PLAYBOOK.parent)
    assert forget_call[0] == "-k"


def test_no_ansible_variable_reaches_the_play_and_no_become_prompt_is_asked(
    tmp_path: Path, stub_sudo: StubSudo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sudo's own configuration cannot let the operator's ANSIBLE_CONFIG or callback override the packaged one."""
    monkeypatch.setenv("ANSIBLE_STDOUT_CALLBACK", "json")
    monkeypatch.setenv("ANSIBLE_CONFIG", str(tmp_path / "other.cfg"))
    playbook = fake_ansible(tmp_path, "true")

    system_step.run_playbook_as_root([str(playbook)])

    play_call = stub_sudo.calls()[0]
    assert play_call[2] == ""
    assert "--ask-become-pass" not in play_call[0]


def test_sudo_forgets_the_ticket_after_a_failing_play(tmp_path: Path, stub_sudo: StubSudo) -> None:
    playbook = fake_ansible(tmp_path, "exit 2")

    run = system_step.run_playbook_as_root([str(playbook)])

    assert not run.succeeded
    assert stub_sudo.calls()[-1][0] == "-k"


def test_a_failing_sudo_k_fails_the_step_after_a_successful_play(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruled E1: a ticket that could not be forgotten is never swallowed."""
    StubSudo(tmp_path, monkeypatch, forgetting_exit_code=1)
    playbook = fake_ansible(tmp_path, "true")

    with pytest.raises(system_step.StepFailed) as failure:
        system_step.run_playbook_as_root([str(playbook)])

    assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)


def test_sudo_forgets_the_ticket_after_a_ctrl_c(tmp_path: Path, stub_sudo: StubSudo) -> None:
    playbook = fake_ansible(tmp_path, "kill -INT $PPID")

    with pytest.raises(KeyboardInterrupt):
        system_step.run_playbook_as_root([str(playbook)])

    assert stub_sudo.calls()[-1][0] == "-k"


def test_a_missing_sudo_fails_alone_without_a_ticket_to_forget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(FileNotFoundError) as refusal:
        system_step.run_playbook_as_root(["ansible-playbook"])

    assert refusal.value.__context__ is None

