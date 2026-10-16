"""`edge external plan` (exit 0 no changes, 1 changes pending, 2 if unable) and `apply`."""

import argparse
import sys
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path

from edge.external import apply, secrets, tofu


class ExitCode(IntEnum):
    NO_CHANGES = 0
    CHANGES = 1
    CANNOT_PLAN = 2


_PLAN_TO_EXIT = {
    tofu.PlanExit.NO_CHANGES: ExitCode.NO_CHANGES,
    tofu.PlanExit.CHANGES: ExitCode.CHANGES,
    tofu.PlanExit.FAILED: ExitCode.CANNOT_PLAN,
}


def add_subcommand(subcommands: argparse._SubParsersAction) -> None:
    external = subcommands.add_parser("external", help="Cloudflare and GitHub as code (OpenTofu)")
    actions = external.add_subparsers(dest="action", required=True)
    plan = actions.add_parser(
        "plan", help="show what differs between external/ and the live services"
    )
    plan.set_defaults(run=lambda _arguments: plan_command(Path.home()))
    apply_parser = actions.add_parser(
        "apply", help="apply the shown plan to the live services, at a terminal, after `yes`"
    )
    apply_parser.set_defaults(run=lambda _arguments: apply_command(Path.home()))


def plan_command(home: Path) -> int:
    return _guarded("plan", home, secrets.Access.READ, _plan, ExitCode.CANNOT_PLAN)


def apply_command(home: Path) -> int:
    return _guarded(
        "apply",
        home,
        secrets.Access.WRITE,
        apply.apply_plan,
        apply.ApplyExit.CANNOT_PLAN,
        refusal=apply.refusal_without_terminal,
    )


def _guarded(
    command: str,
    home: Path,
    access: secrets.Access,
    run: Callable[[Path, dict[str, str]], int],
    cannot_plan: int,
    refusal: Callable[[], int | None] = lambda: None,
) -> int:
    """Run one command; every failure before the plan is `cannot_plan`, named, without a token.

    The leftover sweep runs first; a `refusal` is asked after it and before any token is read.
    """
    token_values: dict[str, str] = {}
    try:
        apply.remove_leftover_plans(home)
        refused = refusal()
        if refused is not None:
            return refused
        token_values = secrets.read_secrets(home, access)
        return run(home, token_values)
    except (secrets.SecretFileError, tofu.TofuError) as error:
        _report(f"edge external {command}: {error}", error, token_values, command)
    except apply.StepFailed as failure:
        _report(
            f"edge external {command}: {failure.step} failed, {_describe(failure.cause)}",
            failure.cause,
            token_values,
            command,
        )
        return failure.exit_code
    except OSError as error:
        _report(
            f"edge external {command}: cannot plan, {_describe(error)}",
            error,
            token_values,
            command,
        )
    except ValueError as error:
        # Only the type: a decode error's message can quote bytes of a token file.
        _report(
            f"edge external {command}: cannot plan, {type(error).__name__}",
            error,
            token_values,
            command,
        )
    return cannot_plan


def _describe(error: OSError) -> str:
    # The filename can be child-controlled (a failed cleanup names what the child left).
    where = f" ({error.filename})" if error.filename else ""
    return f"{type(error).__name__}: {error.strerror}{where}"


def _report(message: str, error: Exception, token_values: dict[str, str], command: str) -> None:
    print(secrets.redact(message, token_values), file=sys.stderr)
    for note in getattr(error, "__notes__", []):
        print(secrets.redact(f"edge external {command}: {note}", token_values), file=sys.stderr)


def _plan(home: Path, token_values: dict[str, str]) -> int:
    with tofu.session(home, token_values) as session:
        planned = tofu.run(session.tofu, tofu.PLAN_ARGUMENTS, session.environment)
    exit_code = _PLAN_TO_EXIT.get(planned.exit_code, ExitCode.CANNOT_PLAN)
    redacted_output = secrets.redact(planned.output, token_values)
    if exit_code is ExitCode.CANNOT_PLAN:
        print(redacted_output, file=sys.stderr)
        print(f"edge external plan: tofu plan failed (exit {planned.exit_code})", file=sys.stderr)
        return exit_code
    print(redacted_output.rstrip("\n"))
    if exit_code is ExitCode.CHANGES:
        print("edge external plan: changes pending, the live services differ from external/")
    return exit_code
