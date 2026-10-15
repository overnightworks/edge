"""The only place that touches the host: processes with fixed argv, the lock and the clock.

Child stderr goes straight to the journal; the runner's own verdict is its last line.
"""

import fcntl
import os
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class HostError(RuntimeError):
    """A host step failed in a way every command reports as one German line, not a traceback."""


class HostCommandError(HostError):
    """A command could not run or exited non-zero; the message names the step, never argv."""

    def __init__(self, label: str, exit_code: int | None) -> None:
        self.exit_code = exit_code
        reason = "ist nicht installiert" if exit_code is None else f"endete mit Exit {exit_code}"
        super().__init__(f"{label} {reason}")


class UnreadableOutputError(HostError):
    """A command succeeded but printed something the runner cannot read."""


def run(
    argv: Sequence[str], *, label: str, stdin: Path | None = None, stdout: Path | None = None
) -> bytes:
    """Run argv without a shell; stdout goes to a new 0600 file when given, else is returned."""
    with ExitStack() as files:
        source = files.enter_context(stdin.open("rb")) if stdin else subprocess.DEVNULL
        sink = files.enter_context(_new_private_file(stdout)) if stdout else subprocess.PIPE
        try:
            completed = subprocess.run(argv, stdin=source, stdout=sink, check=False)
        except FileNotFoundError as error:
            raise HostCommandError(label, None) from error
    if completed.returncode != 0:
        raise HostCommandError(label, completed.returncode)
    return completed.stdout or b""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Blocks until no other edge-backup run holds the lock; the kernel frees it on any exit."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def _new_private_file(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
