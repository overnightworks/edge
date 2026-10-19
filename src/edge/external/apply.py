"""`edge external apply`: the plan is shown, written encrypted, applied only after `yes`.

Exit 0 applied or nothing to apply, 1 refused, aborted or failed, 2 cannot plan; `ClosingCheck`
says whether the host check follows, and `cli.apply_command` maps its verdict into the exit.
The guard stops accidental and non-interactive runs; it does not identify the operator.
"""

import fcntl
import os
import signal
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import Enum, IntEnum, StrEnum, auto
from pathlib import Path

from edge import terminal
from edge.external import secrets, tofu

PLANS_DIRECTORY = secrets.OPENTOFU_DIRECTORY / "plans"
_PLAN_SUFFIX = ".tfplan"
_LOCK_SUFFIX = ".lock"
_OWNED_SUFFIXES = {_PLAN_SUFFIX, _LOCK_SUFFIX}
UNCHECKED_CONSEQUENCE = "live services may be partly changed; run edge external check --hosts"


class ApplyExit(IntEnum):
    APPLIED_OR_NOTHING = 0
    REFUSED = 1
    CANNOT_PLAN = 2


class Step(StrEnum):
    PLAN = "preparing the plan"
    CONFIRMATION = "the confirmation"
    APPLY = "tofu apply"
    CLEANUP = "the cleanup"


class ClosingCheck(Enum):
    """What follows the tofu session: the host check, or why it does not run."""

    NO_CHECK_REFUSED = auto()  # nothing was applied and no plan was empty
    CHECK_NO_CHANGES = auto()
    CHECK_APPLIED = auto()
    CHECK_APPLY_FAILED = auto()  # tofu apply was reached and did not exit 0
    NO_CHECK_INTERRUPTED = auto()  # interrupted after tofu apply was reached

    @property
    def runs_the_check(self) -> bool:
        return self.name.startswith("CHECK_")


@dataclass(frozen=True)
class ApplyOutcome:
    exit: ApplyExit
    closing: ClosingCheck


@dataclass
class Progress:
    step: Step = Step.PLAN
    apply_reached: bool = False
    plan_was_empty: bool = False
    apply_exit_code: int | None = None


def closing_after(progress: Progress, *, interrupted: bool) -> ClosingCheck:
    """The one decider of the closing; cleanup runs after it, so a cleanup failure keeps it."""
    if progress.apply_reached:
        if interrupted:
            return ClosingCheck.NO_CHECK_INTERRUPTED
        if progress.apply_exit_code == 0:
            return ClosingCheck.CHECK_APPLIED
        return ClosingCheck.CHECK_APPLY_FAILED
    if progress.plan_was_empty and not interrupted:
        return ClosingCheck.CHECK_NO_CHANGES
    return ClosingCheck.NO_CHECK_REFUSED


class StepFailed(Exception):
    """A step after the plan failed on the machine; `cause` is the OSError."""

    exit_code = ApplyExit.REFUSED

    def __init__(self, step: Step, cause: OSError, closing: ClosingCheck) -> None:
        super().__init__(step)
        self.step = step
        self.cause = cause
        self.closing = closing


def _interrupt_in_flight(error: BaseException) -> bool:
    """A cleanup that fails while an interrupt unwinds carries that interrupt as its context."""
    context = error.__context__
    while context is not None:
        if isinstance(context, tofu.Interrupted | KeyboardInterrupt):
            return True
        context = context.__context__
    return False


def refusal_without_terminal() -> ApplyExit | None:
    """The refusal of a run that is not at a terminal; asked before any token is read."""
    if terminal.refused_without_terminal("edge external apply"):
        return ApplyExit.REFUSED
    return None


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
def private_plan_file(home: Path) -> Iterator[Path]:
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
def terminate_like_interrupt() -> Iterator[None]:
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def print_interrupted_output(interrupted: tofu.Interrupted, token_values: dict[str, str]) -> None:
    print(secrets.redact(interrupted.completed.output, token_values), file=sys.stderr)


def apply_plan(
    home: Path,
    token_values: dict[str, str],
    plan_file_scope: Callable[[Path], AbstractContextManager[Path]] = private_plan_file,
) -> ApplyOutcome:
    progress = Progress()
    try:
        with (
            terminate_like_interrupt(),
            tofu.session(home, token_values) as session,
            plan_file_scope(home) as plan_file,
        ):
            result = _show_confirm_apply(session, plan_file, token_values, progress)
            progress.step = Step.CLEANUP
    except tofu.Interrupted as interrupted:
        print_interrupted_output(interrupted, token_values)
        return _interrupted(progress)
    except KeyboardInterrupt:
        return _interrupted(progress)
    except OSError as error:
        if progress.step is Step.PLAN:
            raise
        closing = closing_after(progress, interrupted=_interrupt_in_flight(error))
        raise StepFailed(progress.step, error, closing) from error
    return ApplyOutcome(result, closing_after(progress, interrupted=False))


def _interrupted(progress: Progress) -> ApplyOutcome:
    _report_interrupt(progress)
    return ApplyOutcome(ApplyExit.REFUSED, closing_after(progress, interrupted=True))


def _report_interrupt(progress: Progress) -> None:
    step = progress.step
    consequence = UNCHECKED_CONSEQUENCE if progress.apply_reached else "nothing was applied"
    print(
        f"\nedge external apply: interrupted during {step}, {consequence}; "
        "the plan file is deleted",
        file=sys.stderr,
    )


def _show_confirm_apply(
    session: tofu.Session, plan_file: Path, token_values: dict[str, str], progress: Progress
) -> ApplyExit:
    planned = tofu.run(
        session.tofu, [*tofu.PLAN_ARGUMENTS, f"-out={plan_file}"], session.environment
    )
    shown = secrets.redact(planned.output, token_values)
    if planned.exit_code == tofu.PlanExit.NO_CHANGES:
        progress.plan_was_empty = True
        print(shown.rstrip("\n"))
        return ApplyExit.APPLIED_OR_NOTHING
    if planned.exit_code != tofu.PlanExit.CHANGES:
        print(shown, file=sys.stderr)
        print(f"edge external apply: tofu plan failed (exit {planned.exit_code})", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    print(shown.rstrip("\n"))
    progress.step = Step.CONFIRMATION
    if not terminal.confirmed("apply exactly this plan"):
        print("edge external apply: aborted, nothing applied", file=sys.stderr)
        return ApplyExit.REFUSED
    progress.step = Step.APPLY
    progress.apply_reached = True
    applied = tofu.run(session.tofu, ["apply", str(plan_file)], session.environment)
    progress.apply_exit_code = applied.exit_code
    progress.step = Step.CLEANUP
    print(secrets.redact(applied.output, token_values).rstrip("\n"))
    if applied.exit_code != 0:
        print(f"edge external apply: tofu apply failed (exit {applied.exit_code})", file=sys.stderr)
        return ApplyExit.REFUSED
    return ApplyExit.APPLIED_OR_NOTHING
