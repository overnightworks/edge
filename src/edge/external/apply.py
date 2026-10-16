"""`edge external apply`: the plan is shown, written encrypted, applied only after `yes`.

Exit 0 applied or nothing to apply, 1 refused, aborted or failed, 2 cannot plan.
The guard stops accidental and non-interactive runs; it does not identify the operator.
"""

import signal
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from edge.external import secrets, tofu

PLANS_DIRECTORY = secrets.OPENTOFU_DIRECTORY / "plans"
CONFIRMATION = "yes"
REFUSED = 1
_CANNOT_PLAN = 2


def has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def remove_leftover_plans(home: Path) -> None:
    """A plan file survives only a killed run (SIGKILL); name it and delete it."""
    for leftover in (home / PLANS_DIRECTORY).glob("*"):
        leftover.unlink()
        print(f"edge external: removed the leftover plan file {leftover}", file=sys.stderr)


@contextmanager
def _private_plan_file(home: Path) -> Iterator[Path]:
    directory = home / PLANS_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    plan_file = directory / f"{uuid.uuid4().hex}.tfplan"
    try:
        yield plan_file
    finally:
        plan_file.unlink(missing_ok=True)


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
    try:
        with (
            _terminate_like_interrupt(),
            tofu.session(home, token_values) as session,
            _private_plan_file(home) as plan_file,
        ):
            return _show_confirm_apply(session, plan_file, token_values)
    except KeyboardInterrupt:
        print("\nedge external apply: interrupted, the plan file is deleted", file=sys.stderr)
        return REFUSED


def _show_confirm_apply(
    session: tofu.Session, plan_file: Path, token_values: dict[str, str]
) -> int:
    planned = tofu.run(
        session.tofu, [*tofu.PLAN_ARGUMENTS, f"-out={plan_file}"], session.environment
    )
    shown = secrets.redact(planned.output, token_values)
    if planned.exit_code == tofu.PlanExit.NO_CHANGES:
        print(shown.rstrip("\n"))
        return 0
    if planned.exit_code != tofu.PlanExit.CHANGES:
        print(shown, file=sys.stderr)
        print(f"edge external apply: tofu plan failed (exit {planned.exit_code})", file=sys.stderr)
        return _CANNOT_PLAN
    print(shown.rstrip("\n"))
    if not _confirmed():
        print("edge external apply: aborted, nothing applied", file=sys.stderr)
        return REFUSED
    applied = tofu.run(session.tofu, ["apply", str(plan_file)], session.environment)
    print(secrets.redact(applied.output, token_values).rstrip("\n"))
    if applied.exit_code != 0:
        print(f"edge external apply: tofu apply failed (exit {applied.exit_code})", file=sys.stderr)
        return REFUSED
    return 0


def _confirmed() -> bool:
    try:
        return input(f"Type {CONFIRMATION!r} to apply exactly this plan: ") == CONFIRMATION
    except EOFError:
        return False
