"""`edge external apply`: the plan is shown, written encrypted, applied only after `yes`.

Exit 0 applied or nothing to apply, 1 refused, aborted or failed, 2 cannot plan.
The guard stops accidental and non-interactive runs; it does not identify the operator.
"""

import fcntl
import os
import signal
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from edge.external import secrets, tofu

PLANS_DIRECTORY = secrets.OPENTOFU_DIRECTORY / "plans"
CONFIRMATION = "yes"
_PLAN_SUFFIX = ".tfplan"
_LOCK_SUFFIX = ".lock"
_OWNED_SUFFIXES = {_PLAN_SUFFIX, _LOCK_SUFFIX}


class ApplyExit(IntEnum):
    APPLIED_OR_NOTHING = 0
    REFUSED = 1
    CANNOT_PLAN = 2


class Step(StrEnum):
    PLAN = "preparing the plan"
    CONFIRMATION = "the confirmation"
    APPLY = "tofu apply"
    CLEANUP = "the cleanup"


@dataclass
class Progress:
    step: Step = Step.PLAN
    apply_reached: bool = False


class StepFailed(Exception):
    """A step after the plan failed on the machine; `cause` is the OSError."""

    exit_code = ApplyExit.REFUSED

    def __init__(self, step: Step, cause: OSError) -> None:
        super().__init__(step)
        self.step = step
        self.cause = cause


def refusal_without_terminal() -> ApplyExit | None:
    """The refusal of a run that is not at a terminal; asked before any token is read."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return None
    print("edge external apply: refused, stdin and stdout must be a terminal", file=sys.stderr)
    return ApplyExit.REFUSED


def _lock_without_waiting(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def remove_leftover_plans(
    home: Path, take_lock: Callable[[int], None] = _lock_without_waiting
) -> None:
    """A plan file whose run is gone (SIGKILL) is named and deleted; a live run's is kept."""
    directory = home / PLANS_DIRECTORY
    stems = {entry.stem for entry in directory.glob("*") if entry.suffix in _OWNED_SUFFIXES}
    for stem in sorted(stems):
        lock = directory / f"{stem}{_LOCK_SUFFIX}"
        plan_file = directory / f"{stem}{_PLAN_SUFFIX}"
        try:
            descriptor = os.open(lock, os.O_RDWR)
        except FileNotFoundError:
            # A live run takes its lock before its plan exists and deletes the plan first, so
            # a bare plan is dead; claim the stem before deleting it, as a live run would block.
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                continue  # a run created the lock since the glob
        try:
            try:
                take_lock(descriptor)
            except BlockingIOError:
                continue
            if not _names_the_open_file(descriptor, lock):
                continue  # the lock was replaced after the open: a live run owns the new one
            _remove_leftover(plan_file)
            lock.unlink()
        finally:
            os.close(descriptor)


def _remove_leftover(plan_file: Path) -> None:
    if plan_file.exists():
        plan_file.unlink()
        print(f"edge external: removed the leftover plan file {plan_file}", file=sys.stderr)


def _names_the_open_file(descriptor: int, path: Path) -> bool:
    try:
        return os.fstat(descriptor).st_ino == path.stat().st_ino
    except FileNotFoundError:
        return False


def _hold_lock(lock: Path) -> int:
    """Take the exclusive lock of a file this run still owns; the cleanup skips what is locked."""
    while True:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if _names_the_open_file(descriptor, lock):
            return descriptor
        os.close(descriptor)  # a cleanup deleted the lock between open and flock


@contextmanager
def _private_plan_file(home: Path) -> Iterator[Path]:
    directory = home / PLANS_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    name = uuid.uuid4().hex
    lock = directory / f"{name}{_LOCK_SUFFIX}"
    plan_file = directory / f"{name}{_PLAN_SUFFIX}"
    descriptor = _hold_lock(lock)
    try:
        yield plan_file
    finally:
        try:
            plan_file.unlink(missing_ok=True)
        finally:
            lock.unlink(missing_ok=True)
            os.close(descriptor)


def _raise_interrupt(_signal_number: int, _frame: object) -> None:
    raise KeyboardInterrupt


@contextmanager
def _terminate_like_interrupt() -> Iterator[None]:
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def apply_plan(home: Path, token_values: dict[str, str]) -> int:
    progress = Progress()
    try:
        with (
            _terminate_like_interrupt(),
            tofu.session(home, token_values) as session,
            _private_plan_file(home) as plan_file,
        ):
            result = _show_confirm_apply(session, plan_file, token_values, progress)
            progress.step = Step.CLEANUP
            return result
    except tofu.Interrupted as interrupted:
        print(secrets.redact(interrupted.completed.output, token_values), file=sys.stderr)
        _report_interrupt(progress)
        return ApplyExit.REFUSED
    except KeyboardInterrupt:
        _report_interrupt(progress)
        return ApplyExit.REFUSED
    except OSError as error:
        if progress.step is Step.PLAN:
            raise
        raise StepFailed(progress.step, error) from error


def _report_interrupt(progress: Progress) -> None:
    step = progress.step
    consequence = (
        "live services may be partly changed, check `edge external plan`"
        if progress.apply_reached
        else "nothing was applied"
    )
    print(
        f"\nedge external apply: interrupted during {step}, {consequence}; "
        "the plan file is deleted",
        file=sys.stderr,
    )


def _show_confirm_apply(
    session: tofu.Session, plan_file: Path, token_values: dict[str, str], progress: Progress
) -> int:
    planned = tofu.run(
        session.tofu, [*tofu.PLAN_ARGUMENTS, f"-out={plan_file}"], session.environment
    )
    shown = secrets.redact(planned.output, token_values)
    if planned.exit_code == tofu.PlanExit.NO_CHANGES:
        print(shown.rstrip("\n"))
        return ApplyExit.APPLIED_OR_NOTHING
    if planned.exit_code != tofu.PlanExit.CHANGES:
        print(shown, file=sys.stderr)
        print(f"edge external apply: tofu plan failed (exit {planned.exit_code})", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    print(shown.rstrip("\n"))
    progress.step = Step.CONFIRMATION
    if not _confirmed():
        print("edge external apply: aborted, nothing applied", file=sys.stderr)
        return ApplyExit.REFUSED
    progress.step = Step.APPLY
    progress.apply_reached = True
    applied = tofu.run(session.tofu, ["apply", str(plan_file)], session.environment)
    progress.step = Step.CLEANUP
    print(secrets.redact(applied.output, token_values).rstrip("\n"))
    if applied.exit_code != 0:
        print(f"edge external apply: tofu apply failed (exit {applied.exit_code})", file=sys.stderr)
        return ApplyExit.REFUSED
    return ApplyExit.APPLIED_OR_NOTHING


def _confirmed() -> bool:
    try:
        return input(f"Type {CONFIRMATION!r} to apply exactly this plan: ") == CONFIRMATION
    except EOFError:
        return False
