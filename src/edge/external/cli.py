"""`edge external plan` (exit 0 no changes, 1 changes pending, 2 if unable), `apply` and `check`."""

import argparse
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path
from typing import Any

from edge import catalog, checkout
from edge.external import apply, cloudflare_read, host_check, secrets, tofu

INTERRUPTED = 130
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


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
    check = actions.add_parser(
        "check", help="compare what Cloudflare holds with the hosts fleet.yaml declares (read-only)"
    )
    check.add_argument(
        "--hosts",
        action="store_true",
        required=True,
        help="the host check: records and Access apps",
    )
    check.add_argument("--json", action="store_true", help="print the findings as a JSON list")
    check.set_defaults(run=lambda arguments: check_command(Path.home(), as_json=arguments.json))


def plan_command(home: Path) -> int:
    return _guarded(
        "plan",
        home,
        secrets.Access.READ,
        _plan,
        ExitCode.CANNOT_PLAN,
        refusal=lambda: _refusal_invalid_catalog("plan"),
    )


def apply_command(
    home: Path, tree: Path | None = None, git: checkout.Runner = checkout.run_git
) -> int:
    """`tree` is the checkout whose `external/` is applied (this code's own by default): only the
    live one, on a clean main."""

    def refusal() -> int | None:
        return (
            apply.refusal_without_terminal()
            or _refusal_off_clean_live_main(tree or tofu.PROJECT_ROOT, home, git)
            or _refusal_invalid_catalog("apply")
        )

    return _guarded(
        "apply",
        home,
        secrets.Access.WRITE,
        apply.apply_plan,
        apply.ApplyExit.CANNOT_PLAN,
        refusal=refusal,
    )


def check_command(
    home: Path,
    *,
    as_json: bool = False,
    cloudflare: Callable[[str], cloudflare_read.CloudflareRead] = cloudflare_read.CloudflareClient,
    emit: Callable[[str], None] = lambda text: sys.stdout.write(f"{text}\n"),
) -> int:
    """Read-only: no tofu, no sweep, no write token. `cloudflare` makes the reader, `emit` writes
    the one output: an interrupt before it leaves stdout empty, none during it cuts the document."""
    token_values: dict[str, str] = {}
    verdict_stands: host_check.CheckExit | None = None
    try:
        with apply.terminate_like_interrupt():
            document = _valid_catalog("check")
            if document is None:
                return host_check.CheckExit.CANNOT_CHECK
            token_values = {"CLOUDFLARE_API_TOKEN": secrets.read_cloudflare_token(home)}
            result = host_check.check_hosts(
                document, cloudflare(token_values["CLOUDFLARE_API_TOKEN"])
            )
            rendered = host_check.as_json(result) if as_json else host_check.as_text(result)
            text = secrets.redact(rendered, token_values)
            with _signals_ignored():
                verdict_stands = result.exit_code
                emit(text)
                sys.stdout.flush()
            return result.exit_code
    except KeyboardInterrupt:
        if verdict_stands is not None:
            return verdict_stands
        print("edge external check: interrupted", file=sys.stderr)
        return INTERRUPTED
    except (secrets.SecretFileError, cloudflare_read.CloudflareReadError) as error:
        _report(f"edge external check: cannot check, {error}", error, token_values, "check")
    except OSError as error:
        _report(
            f"edge external check: cannot check, {_describe(error)}", error, token_values, "check"
        )
    except ValueError as error:
        # Only the type: a decode error's message can quote bytes of a token file.
        _report(
            f"edge external check: cannot check, {type(error).__name__}",
            error,
            token_values,
            "check",
        )
    return host_check.CheckExit.CANNOT_CHECK


@contextmanager
def _signals_ignored() -> Iterator[None]:
    """The protected region of the result's write: inside it, the verdict's exit code stands."""
    previous: dict[int, Any] = {}
    try:
        for number in _STOP_SIGNALS:
            previous[number] = signal.signal(number, signal.SIG_IGN)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _refusal_off_clean_live_main(tree: Path, home: Path, git: checkout.Runner) -> int | None:
    """Exit 2, naming why, unless `tree` is the live checkout on a clean main (#30 rules 2, 3)."""
    if not_live := checkout.not_live_checkout_line(tree, home):
        refusals = [not_live]
    else:
        refusals = checkout.not_clean_main(checkout.Git(tree, git))
    for line in refusals:
        print(f"edge external apply: {line}", file=sys.stderr)
    return ExitCode.CANNOT_PLAN if refusals else None


def _valid_catalog(command: str) -> Any | None:
    """The catalog tofu reads, or None after naming the key when it is not valid (#131 rule 5)."""
    try:
        document = catalog.read(tofu.PROJECT_ROOT / catalog.FLEET_FILE)
        catalog.hosts(document)
    except catalog.CatalogError as error:
        print(f"edge external {command}: {error}", file=sys.stderr)
        return None
    return document


def _refusal_invalid_catalog(command: str) -> int | None:
    return ExitCode.CANNOT_PLAN if _valid_catalog(command) is None else None


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
    try:
        with apply.terminate_like_interrupt(), tofu.session(home, token_values) as session:
            planned = tofu.run(session.tofu, tofu.PLAN_ARGUMENTS, session.environment)
    except tofu.Interrupted as interrupted:
        apply.print_interrupted_output(interrupted, token_values)
        print("edge external plan: interrupted during the plan, no plan was made", file=sys.stderr)
        return ExitCode.CANNOT_PLAN
    except KeyboardInterrupt:
        print("edge external plan: interrupted during the plan, no plan was made", file=sys.stderr)
        return ExitCode.CANNOT_PLAN
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
