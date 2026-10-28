"""`apply_command`, the publish phase of `hal apply`, which ends on the host check."""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from edge import catalog, checkout
from edge.external import apply, cloudflare_read, host_check, host_probe, secrets, tofu
from hal_output.command import Verb, command

INTERRUPTED = 130


def apply_command(
    home: Path,
    tree: Path | None = None,
    git: checkout.Runner = checkout.run_git,
    *,
    prober_for: Callable[[str], host_probe.Prober] = host_probe.HttpProber,
) -> int:
    """`tree` is the checkout whose `external/` is applied (this code's own by default): only the
    live one, on a clean main. A plan with changes is refused before `yes` while an enabled host's
    `ready` path does not answer on its target (`prober_for` makes the prober of one target;
    #30 rule 8). An apply that may have changed the services, or found nothing to
    do, ends on the host check (#30 rule 8): its verdict decides the exit.

    The `try` is the first statement and SIGTERM becomes an interrupt as its first step, so every
    setup step is inside both; the one `except KeyboardInterrupt` decides every interrupt that
    reaches it: before the tofu session "nothing was applied"; after it the line of the
    disposition known so far; once the check committed its verdict, that verdict's exit.
    Always exit 1 but the last. Accepted windows, named and not covered: from process start until
    this first statement, while the interrupt handler writes its own line, between
    `check_command`'s return and the mask, between the two handler swaps of `_signals_ignored`,
    and after the mask until the process exits."""
    try:
        with apply.terminate_like_interrupt():
            run = _ApplyRun()
            closing = apply.ClosingCheck.NO_CHECK_REFUSED
            planned_catalog: _ValidCatalog | None = None

            def refusal() -> int | None:
                nonlocal planned_catalog
                if refused := apply.refusal_without_terminal() or _refusal_off_clean_live_main(
                    tree or tofu.PROJECT_ROOT, home, git
                ):
                    return refused
                planned_catalog = _valid_catalog("apply")
                if planned_catalog is None:
                    return apply.ApplyExit.CANNOT_PLAN
                return _refusal_unreadable_cloudflare_read_file(home)

            def plan_and_apply(home: Path, token_values: dict[str, str]) -> int:
                nonlocal closing
                assert planned_catalog is not None  # the refusal, which always runs first, set it
                try:
                    outcome = apply.apply_plan(
                        home,
                        token_values,
                        progress=run.progress,
                        hosts=planned_catalog.hosts,
                        prober_for=prober_for,
                        catalog_bytes=planned_catalog.raw,
                    )
                except apply.StepFailed as failure:
                    closing = failure.closing
                    raise
                closing = outcome.closing
                return outcome.exit

            exit_code = _guarded(
                "apply",
                home,
                secrets.Access.WRITE,
                plan_and_apply,
                apply.ApplyExit.CANNOT_PLAN,
                refusal=refusal,
            )
            if not closing.runs_the_check:
                return exit_code
            return _close_on_host_check(home, closing, exit_code, run)
    except KeyboardInterrupt:
        # an interrupt before the record exists left nothing behind: the fresh record says so
        return _exit_after_interrupt(locals().get("run") or _ApplyRun())


@dataclass
class _ApplyRun:
    """What the interrupt handler of `apply_command` knows, recorded where each fact is true."""

    progress: apply.Progress = field(default_factory=apply.Progress)
    committed_exit: int | None = None


def _exit_after_interrupt(run: _ApplyRun) -> int:
    if run.committed_exit is not None:
        return run.committed_exit
    progress = run.progress
    if progress.interrupted:
        if not progress.reported:  # the signal landed before `apply_plan` printed its line
            apply.report_interrupt(progress, plan_file_deleted=False)
        return apply.ApplyExit.REFUSED
    closing = apply.closing_after(progress)
    if closing.runs_the_check:
        line = _CLOSING_LINES[(closing, INTERRUPTED)]
    else:
        line = "interrupted, nothing was applied"
    print(f"edge external apply: {line}", file=sys.stderr)
    return apply.ApplyExit.REFUSED


_RUN_HOSTS_CHECK = command(Verb.CHECK, "hosts")
_CLOSING_LINES = {
    (apply.ClosingCheck.CHECK_NO_CHANGES, host_check.CheckExit.CANNOT_CHECK): (
        "the host check could not run"
    ),
    (apply.ClosingCheck.CHECK_NO_CHANGES, INTERRUPTED): (
        f"the host check was interrupted; run {_RUN_HOSTS_CHECK}"
    ),
    (apply.ClosingCheck.CHECK_APPLIED, host_check.CheckExit.FINDINGS): (
        "applied; the host check has findings, see above"
    ),
    (apply.ClosingCheck.CHECK_APPLIED, host_check.CheckExit.CANNOT_CHECK): (
        "applied, but the host check could not run"
    ),
    (apply.ClosingCheck.CHECK_APPLIED, INTERRUPTED): (
        f"applied; the host check was interrupted; run {_RUN_HOSTS_CHECK}"
    ),
    (apply.ClosingCheck.CHECK_APPLY_FAILED, host_check.CheckExit.CLEAN): (
        "the apply failed; the host check ran"
    ),
    (apply.ClosingCheck.CHECK_APPLY_FAILED, host_check.CheckExit.FINDINGS): (
        "the apply failed; the host check ran"
    ),
    (apply.ClosingCheck.CHECK_APPLY_FAILED, host_check.CheckExit.CANNOT_CHECK): (
        "the apply failed; the host check could not run"
    ),
    (apply.ClosingCheck.CHECK_APPLY_FAILED, INTERRUPTED): (
        f"the apply failed; the host check was interrupted; run {_RUN_HOSTS_CHECK}"
    ),
}


def _close_on_host_check(
    home: Path, closing: apply.ClosingCheck, apply_exit: int, run: _ApplyRun
) -> int:
    """The check after the tofu session, under the apply's own signal handling. Once
    `check_command` returned its verdict, the mapping and the lines run with the signals ignored
    and the exit is recorded: a clean verdict is never turned into "interrupted"."""
    check_exit = check_command(home)
    with _signals_ignored():
        run.committed_exit = (
            apply_exit if check_exit == host_check.CheckExit.CLEAN else apply.ApplyExit.REFUSED
        )
        if line := _CLOSING_LINES.get((closing, check_exit)):
            print(f"edge external apply: {line}", file=sys.stderr)
        return run.committed_exit


def _refusal_unreadable_cloudflare_read_file(home: Path) -> int | None:
    """Exit 2 naming the file when the host check could not read its token after the apply."""
    try:
        secrets.read_cloudflare_token(home)
    except secrets.SecretFileError as error:
        print(f"edge external apply: {error}", file=sys.stderr)
        return apply.ApplyExit.CANNOT_PLAN
    return None


@dataclass(frozen=True)
class HostCheckOutcome:
    """How one host check ended: the exit code (`host_check.CheckExit`, or `INTERRUPTED`) and, for
    a check that could not run, its cause with every token redacted, one line each."""

    exit_code: int
    cause: tuple[str, ...] = ()


def _write_line_to_stdout(text: str) -> None:
    sys.stdout.write(f"{text}\n")


def run_host_check(
    home: Path,
    *,
    as_json: bool = False,
    cloudflare: Callable[[str], cloudflare_read.CloudflareRead] | None = None,
    prober: Callable[[], host_probe.Prober] | None = None,
    emit: Callable[[str], None] = _write_line_to_stdout,
) -> HostCheckOutcome:
    """Read-only: no tofu, no sweep, no write token. `cloudflare` makes the reader, `prober` the
    unauthenticated host probe, `emit` writes the one output, redacted, with stop signals ignored:
    an interrupt before it leaves stdout empty, none during it cuts the document. The caller
    reports the outcome; nothing here writes to stderr."""
    token_values: dict[str, str] = {}
    verdict_stands: host_check.CheckExit | None = None
    try:
        with apply.terminate_like_interrupt():
            valid = _read_catalog()
            token_values = {"CLOUDFLARE_API_TOKEN": secrets.read_cloudflare_token(home)}
            reader = (cloudflare or cloudflare_read.CloudflareClient)(
                token_values["CLOUDFLARE_API_TOKEN"]
            )
            result = host_check.check_hosts(
                valid.document, reader, (prober or host_probe.HttpProber)()
            )
            rendered = host_check.as_json(result) if as_json else host_check.as_text(result)
            text = secrets.redact(rendered, token_values)
            with _signals_ignored():
                verdict_stands = result.exit_code
                emit(text)
                sys.stdout.flush()
            return HostCheckOutcome(result.exit_code)
    except KeyboardInterrupt:
        return HostCheckOutcome(verdict_stands if verdict_stands is not None else INTERRUPTED)
    except (
        catalog.CatalogError,
        secrets.SecretFileError,
        cloudflare_read.CloudflareReadError,
    ) as error:
        return _cannot_check(str(error), error, token_values)
    except OSError as error:
        return _cannot_check(_describe(error), error, token_values)
    except ValueError as error:
        # Only the type: a decode error's message can quote bytes of a token file.
        return _cannot_check(type(error).__name__, error, token_values)


def _cannot_check(message: str, error: Exception, token_values: dict[str, str]) -> HostCheckOutcome:
    lines = (message, *getattr(error, "__notes__", ()))
    cause = tuple(secrets.redact(line, token_values) for line in lines)
    return HostCheckOutcome(host_check.CheckExit.CANNOT_CHECK, cause)


def check_command(
    home: Path,
    *,
    cloudflare: Callable[[str], cloudflare_read.CloudflareRead] | None = None,
    prober: Callable[[], host_probe.Prober] | None = None,
    emit: Callable[[str], None] = _write_line_to_stdout,
) -> int:
    """The host check that closes `edge external apply`: its findings on stdout, why it could not
    run on stderr."""
    outcome = run_host_check(home, cloudflare=cloudflare, prober=prober, emit=emit)
    for line in outcome.cause:
        print(f"edge external apply: host check: cannot check, {line}", file=sys.stderr)
    return outcome.exit_code


# The protected region of the result's write: inside it, the verdict's exit code stands. A name of
# this module, so the timing cells wrap the closing's mask and not the interrupt line's.
_signals_ignored = apply.signals_ignored


def _refusal_off_clean_live_main(tree: Path, home: Path, git: checkout.Runner) -> int | None:
    """Exit 2, naming why, unless `tree` is the live checkout on a clean main (#30 rules 2, 3)."""
    if not_live := checkout.not_live_checkout_line(tree, home):
        refusals = [not_live]
    elif refused := checkout.not_clean_main(checkout.Git(tree, git)):
        refusals = _refusal_lines(refused)
    else:
        refusals = []
    for line in refusals:
        print(f"edge external apply: {line}", file=sys.stderr)
    return apply.ApplyExit.CANNOT_PLAN if refusals else None


def _refusal_lines(refusal: checkout.Refusal) -> list[str]:
    if not refusal.paths:
        return [refusal.message]
    return [
        f"{refusal.message}; {checkout.PULL_REQUEST_ROUTE}:",
        *(f"  {path}" for path in refusal.paths),
    ]


@dataclass(frozen=True)
class _ValidCatalog:
    """One validated read of the catalog: its bytes, document and enabled hosts stand together."""

    raw: bytes
    document: Any
    hosts: tuple[catalog.Host, ...]


def _read_catalog() -> _ValidCatalog:
    file = tofu.PROJECT_ROOT / catalog.FLEET_FILE
    raw = catalog.read_bytes(file)
    document = catalog.parse(raw, file.name)
    return _ValidCatalog(raw, document, catalog.hosts(document))


def _valid_catalog(command: str) -> _ValidCatalog | None:
    """The catalog tofu reads, or None after naming the key when it is not valid (#131 rule 5)."""
    try:
        return _read_catalog()
    except catalog.CatalogError as error:
        print(f"edge external {command}: {error}", file=sys.stderr)
        return None


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


def _describe(error: Exception) -> str:
    if not isinstance(error, OSError):
        return type(error).__name__  # another error's message may quote what tofu printed
    # The filename can be child-controlled (a failed cleanup names what the child left).
    where = f" ({error.filename})" if error.filename else ""
    return f"{type(error).__name__}: {error.strerror}{where}"


def _report(message: str, error: Exception, token_values: dict[str, str], command: str) -> None:
    print(secrets.redact(message, token_values), file=sys.stderr)
    for note in getattr(error, "__notes__", []):
        print(secrets.redact(f"edge external {command}: {note}", token_values), file=sys.stderr)
