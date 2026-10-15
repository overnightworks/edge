"""The status contract a Gatus light and `home check` read: versioned JSON, written atomically.

It never carries raw output, command lines, paths, file names or content.
"""

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

SCHEMA = 1


class State(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class Phase(StrEnum):
    PREPARE = "prepare"
    READINESS = "readiness"
    DUMP = "dump"
    MANIFEST = "manifest"
    BACKUP = "backup"
    FORGET = "forget"
    CHECK = "check"
    RESTORE = "restore"
    VERIFY = "verify"


class ExitCategory(StrEnum):
    CONFIGURATION = "configuration"
    NOT_READY = "not-ready"
    COMMAND_FAILED = "command-failed"
    VERIFICATION_FAILED = "verification-failed"
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"


class StatusFileError(ValueError):
    """The previous status file cannot be read, so its history cannot be carried over."""


@dataclass(frozen=True)
class Status:
    state: State
    phase: Phase
    last_attempt: datetime
    last_successful_backup: datetime | None = None
    last_successful_restore_test: datetime | None = None
    last_snapshot_id: str | None = None
    failing_phase: Phase | None = None
    exit_category: ExitCategory | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "state": self.state.value,
            "phase": self.phase.value,
            "failing_phase": _optional_value(self.failing_phase),
            "exit_category": _optional_value(self.exit_category),
            "last_attempt": self.last_attempt.isoformat(),
            "last_successful_backup": _optional_time(self.last_successful_backup),
            "last_successful_restore_test": _optional_time(self.last_successful_restore_test),
            "last_snapshot_id": self.last_snapshot_id,
        }

    def attempt(self, started: datetime) -> "Status":
        """A new run starts: history carries over, the verdict does not."""
        return Status(
            state=State.RUNNING,
            phase=Phase.PREPARE,
            last_attempt=started,
            last_successful_backup=self.last_successful_backup,
            last_successful_restore_test=self.last_successful_restore_test,
            last_snapshot_id=self.last_snapshot_id,
        )

    def entering(self, phase: Phase) -> "Status":
        return replace(self, phase=phase)

    def backed_up(self, at: datetime, snapshot_id: str) -> "Status":
        return replace(self, last_successful_backup=at, last_snapshot_id=snapshot_id)

    def restore_tested(self, at: datetime) -> "Status":
        return replace(self, last_successful_restore_test=at)

    def failed(self, category: ExitCategory) -> "Status":
        return replace(self, state=State.FAILED, failing_phase=self.phase, exit_category=category)

    def succeeded(self) -> "Status":
        return replace(self, state=State.OK)


def first_attempt(started: datetime) -> Status:
    return Status(state=State.RUNNING, phase=Phase.PREPARE, last_attempt=started)


def read_status(path: Path) -> Status | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StatusFileError(
            f"die bisherige Statusdatei ist unlesbar: {error.strerror}"
        ) from error
    try:
        return _parse(json.loads(raw))
    except (ValueError, KeyError, TypeError) as error:
        raise StatusFileError("die bisherige Statusdatei ist unlesbar") from error


def write_status(path: Path, status: Status) -> None:
    """Temp file in the same directory, then rename: a reader never sees half a status."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(status.as_json(), handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parse(document: dict) -> Status:
    if document["schema"] != SCHEMA:
        raise ValueError(f"schema {document['schema']!r}")
    return Status(
        state=State(document["state"]),
        phase=Phase(document["phase"]),
        last_attempt=datetime.fromisoformat(document["last_attempt"]),
        last_successful_backup=_parsed_time(document["last_successful_backup"]),
        last_successful_restore_test=_parsed_time(document["last_successful_restore_test"]),
        last_snapshot_id=document["last_snapshot_id"],
        failing_phase=_parsed(Phase, document["failing_phase"]),
        exit_category=_parsed(ExitCategory, document["exit_category"]),
    )


def _optional_value(member: StrEnum | None) -> str | None:
    return None if member is None else member.value


def _optional_time(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _parsed_time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _parsed[E: StrEnum](enum: type[E], text: str | None) -> E | None:
    return None if text is None else enum(text)
