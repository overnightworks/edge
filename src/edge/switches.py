"""The emergency switch: one marker file per switched-off unit, and the lock that orders stops.

`~/.local/state/edge/stopped/<unit>` holds the switch time (ISO 8601 with offset). The file is
the record (P7): every start path of `edge` reads it, and only `edge start` clears it. A unit key
reaches this module only after the catalog resolved it, so no path is built from unchecked text.
The state directory is an argument; only the CLI entry resolves the real one.
"""

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STOPPED_DIRECTORY = "stopped"
LOCK_FILE = "switch.lock"
UNKNOWN_SWITCH_TIME = "unknown time"
_PRIVATE_DIRECTORY = 0o700


class SwitchError(Exception):
    """The marker or the lock cannot be read or written; the message is one line."""


def default_state_directory() -> Path:
    return Path.home() / ".local" / "state" / "edge"


def describe_since(since: str) -> str:
    """`2026-10-17T22:10:04+02:00` as `17.10. 22:10`; text that is no time is shown as it is."""
    try:
        return datetime.fromisoformat(since).strftime("%d.%m. %H:%M")
    except ValueError:
        return since


@dataclass(frozen=True)
class Mark:
    """A marker as one reader saw it: its line and the file's identity.

    A repeated stop rewrites the line unchanged into a new file, so only the identity tells a
    start waiting at its prompt that a stop came in (rule 5).
    """

    since: str
    inode: int
    modified_ns: int


@dataclass(frozen=True)
class Switches:
    directory: Path

    @property
    def _stopped(self) -> Path:
        return self.directory / STOPPED_DIRECTORY

    def mark(self, unit: str) -> Mark | None:
        """The marker as it is now, or None when the unit is not switched off.

        The file is the record: an empty one still means switched off, at an unknown time.
        """
        try:
            with (self._stopped / unit).open() as file:
                status = os.fstat(file.fileno())
                line = file.read().strip()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SwitchError(f"cannot read the switch of {unit} ({error.strerror})") from error
        return Mark(line or UNKNOWN_SWITCH_TIME, status.st_ino, status.st_mtime_ns)

    def since(self, unit: str) -> str | None:
        """The time the unit was switched off, or None when it is not switched off."""
        mark = self.mark(unit)
        return mark.since if mark else None

    def record(self, unit: str, now: datetime) -> None:
        """Write the marker into a new file; a unit keeps the time of its first switch."""
        recorded = self.since(unit)
        line = recorded if recorded and recorded != UNKNOWN_SWITCH_TIME else None
        try:
            self._make_private_directories()
            _write_atomically(self._stopped / unit, line or now.isoformat(timespec="seconds"))
        except OSError as error:
            raise SwitchError(f"cannot record the switch of {unit} ({error.strerror})") from error

    def clear(self, unit: str, expected: Mark) -> bool:
        """Remove the marker only if it is still the one the caller read."""
        if self.mark(unit) != expected:
            return False
        try:
            (self._stopped / unit).unlink()
        except OSError as error:
            raise SwitchError(f"cannot clear the switch of {unit} ({error.strerror})") from error
        return True

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive; held only around "read the marker, then `up`" and around a `stop`."""
        try:
            self._make_private_directories()
            descriptor = os.open(self.directory / LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError as error:
            raise SwitchError(f"cannot take the switch lock ({error.strerror})") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _make_private_directories(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stopped.mkdir(mode=_PRIVATE_DIRECTORY, exist_ok=True)
        self._stopped.chmod(_PRIVATE_DIRECTORY)


def _write_atomically(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(f"{text}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
