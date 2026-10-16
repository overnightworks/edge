"""`edge external plan`: exit 0 no changes, 1 changes pending, 2 if unable."""

import argparse
import sys
from enum import IntEnum
from pathlib import Path

from edge.external import secrets, tofu


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


def plan_command(home: Path) -> int:
    try:
        return _plan(home)
    except (secrets.SecretFileError, tofu.TofuError) as error:
        _report(f"edge external plan: {error}", error)
    except OSError as error:
        # strerror and filename are fixed text and a path, never token content.
        where = f" ({error.filename})" if error.filename else ""
        _report(
            f"edge external plan: cannot plan, {type(error).__name__}: {error.strerror}{where}",
            error,
        )
    except ValueError as error:
        # Only the type: a decode error's message can quote bytes of a token file.
        _report(f"edge external plan: cannot plan, {type(error).__name__}", error)
    return ExitCode.CANNOT_PLAN


def _report(message: str, error: Exception) -> None:
    print(message, file=sys.stderr)
    for note in getattr(error, "__notes__", []):
        print(f"edge external plan: {note}", file=sys.stderr)


def _plan(home: Path) -> int:
    token_values = secrets.read_secrets(home)
    tofu_path = tofu.locate_tofu()
    with secrets.private_temporary_directory(home) as temporary_directory:
        environment = secrets.child_environment(
            tofu=tofu_path,
            home=home,
            temporary_directory=temporary_directory,
            tofurc=tofu.TOFURC,
            secrets=token_values,
        )
        try:
            tofu.require_pinned_version(tofu_path, environment)
        except tofu.TofuError as error:
            raise tofu.TofuError(secrets.redact(str(error), token_values)) from None
        init = tofu.run(tofu_path, tofu.init_arguments(home), environment)
        if init.exit_code != 0:
            print(secrets.redact(init.output, token_values), file=sys.stderr)
            raise tofu.TofuError("tofu init failed")
        planned = tofu.run(tofu_path, tofu.PLAN_ARGUMENTS, environment)
    exit_code = _PLAN_TO_EXIT.get(planned.exit_code, ExitCode.CANNOT_PLAN)
    redacted_output = secrets.redact(planned.output, token_values)
    if exit_code is ExitCode.CANNOT_PLAN:
        print(redacted_output, file=sys.stderr)
        print(f"edge external plan: tofu plan failed (exit {planned.exit_code})", file=sys.stderr)
        return exit_code
    print(redacted_output)
    if exit_code is ExitCode.CHANGES:
        print("edge external plan: changes pending, the live services differ from external/")
    return exit_code
