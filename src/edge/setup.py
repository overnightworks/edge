"""Whether a unit is set up: runs its `<setup> --check` and words the refusal.

The one owner of that question for `edge apply --closed`, `edge start` and any later caller.
"""

import subprocess
from collections.abc import Callable
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


def refusal(unit: catalog.Unit, repository: Path, check: SetupCheck, again: str) -> str | None:
    """None when the unit is set up, else the refusal naming `again`; raises SetupUnclear."""
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
        return (
            f"{unit.key} is not set up: run `{setup}`, then `{again}` again; {_last_line(result)}"
        )
    raise SetupUnclear(f"`{setup} --check` cannot tell: {_last_line(result)}")
