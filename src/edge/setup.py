"""Whether a unit is set up: runs its `<setup> --check`, words the refusal and runs the setup.

The one owner of that question for `edge apply --closed`, `edge start` and any later caller.
"""

import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from edge import catalog

SETUP_CHECK_SECONDS = 300


class SetupCheckExit(IntEnum):
    SET_UP = 0
    NOT_SET_UP = 1


class SetupUnclear(Exception):
    """A setup check could not tell; the message is its own line."""


SetupCheck = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_setup_check(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=SETUP_CHECK_SECONDS
    )


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no output"


def not_set_up_line(unit: catalog.Unit, repository: Path, check: SetupCheck) -> str | None:
    """The check's last line when the unit is not set up, else None; raises SetupUnclear."""
    setup = unit.setup
    if setup is None:
        return None
    try:
        result = check([str(repository / setup), "--check"])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupUnclear(f"`{setup} --check` did not run ({type(error).__name__})") from error
    if result.returncode == SetupCheckExit.SET_UP:
        return None
    if result.returncode == SetupCheckExit.NOT_SET_UP:
        return _last_line(result)
    raise SetupUnclear(f"`{setup} --check` cannot tell: {_last_line(result)}")


def refusal(unit: catalog.Unit, repository: Path, check: SetupCheck, again: str) -> str | None:
    """None when the unit is set up, else the refusal naming `again`; raises SetupUnclear."""
    line = not_set_up_line(unit, repository, check)
    if line is None:
        return None
    return f"{unit.key} is not set up: run `{unit.setup}`, then `{again}` again; {line}"


@dataclass(frozen=True)
class SetupRun:
    exit_code: int
    interrupted: bool


SetupRunner = Callable[[Path, Path], SetupRun]


def run_setup(command: Path, repository: Path) -> SetupRun:
    """Run the setup in the caller's terminal: inherited streams and environment, no time limit.

    Ctrl-C reaches the setup through the terminal; this process waits for it to finish its own
    interrupt handling instead of dying first. Raises OSError when the setup cannot start.
    """
    interrupted = False

    def note_interrupt(_signal: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    previous = signal.signal(signal.SIGINT, note_interrupt)
    try:
        exit_code = subprocess.run([str(command)], cwd=repository, check=False).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
    return SetupRun(exit_code, interrupted or exit_code == -signal.SIGINT)
