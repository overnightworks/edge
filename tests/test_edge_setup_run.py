"""`setup.run_setup` against a real child process: the terminal's Ctrl-C reaches the setup, and
the parent waits for the setup's own interrupt handling instead of dying first."""

import os
import signal
import stat
import threading
from pathlib import Path

from edge import setup

SETUP_SCRIPT = """#!/bin/sh
trap 'sleep 0.3; echo cleaned > cleaned; exit 130' INT
echo $$ > ready
while :; do sleep 0.05; done
"""


def write_setup(repository: Path) -> Path:
    command = repository / "init"
    command.write_text(SETUP_SCRIPT)
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return command


def type_ctrl_c_when_ready(repository: Path) -> None:
    """What the terminal does on Ctrl-C: SIGINT to the parent and to the running setup."""
    ready = repository / "ready"

    def type_ctrl_c() -> None:
        while not ready.exists() or not ready.read_text().endswith("\n"):
            threading.Event().wait(0.02)
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(int(ready.read_text()), signal.SIGINT)

    threading.Thread(target=type_ctrl_c, daemon=True).start()


def test_an_interrupt_lets_the_setup_finish_its_own_handling_before_the_parent_goes_on(
    tmp_path: Path,
):
    command = write_setup(tmp_path)
    type_ctrl_c_when_ready(tmp_path)

    ran = setup.run_setup(command, tmp_path)

    assert ran == setup.SetupRun(exit_code=130, interrupted=True)
    assert (tmp_path / "cleaned").read_text() == "cleaned\n"


def test_a_setup_runs_in_the_repository_with_the_inherited_environment(tmp_path: Path, monkeypatch):
    command = tmp_path / "init"
    command.write_text('#!/bin/sh\npwd > where\nprintf %s "$EDGE_TEST_MARK" > mark\n')
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("EDGE_TEST_MARK", "inherited")

    ran = setup.run_setup(command, tmp_path)

    assert ran == setup.SetupRun(exit_code=0, interrupted=False)
    assert Path((tmp_path / "where").read_text().strip()).resolve() == tmp_path.resolve()
    assert (tmp_path / "mark").read_text() == "inherited"
