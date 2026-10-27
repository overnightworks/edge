"""The steps behind `hal backup` and `hal restore`: each returns when it succeeded and raises
`StepFailed` when it did not. `hal.backup_commands` owns the lock, the
signal and the exit."""

import argparse
import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from edge_backup import host, restic, runner
from edge_backup.apps import App, AppsError, parse_apps
from edge_backup.runner import Interrupted, Run, StepFailed
from edge_backup.settings import Settings
from edge_backup.status import ExitCategory, first_attempt
from hal_output import Glyph, Verdict, write_row, write_verdict

# uv installs the project editable, so this file sits in the checkout: src/edge_backup/cli.py.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APPS = _PROJECT_ROOT / "backup" / "apps.toml"


def init(settings: Settings, _: argparse.Namespace) -> None:
    runner.require_password_file(settings)
    settings.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    _command(restic.init, settings)
    print("hal backup: Repository angelegt")


def backup(settings: Settings, arguments: argparse.Namespace) -> None:
    """With `--json` only the snapshot is printed; otherwise a row per app, one for the
    repository and the verdict."""
    _require_declared(arguments)
    started = host.monotonic()
    rows = not arguments.json

    def step(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
        try:
            runner.backup(
                settings,
                apps,
                run,
                only=arguments.app,
                pre_apply=arguments.pre_apply,
                report=write_row if rows else lambda _: None,
            )
        except StepFailed as failure:
            if failure.app is not None:
                _name_failed_apps(failure, run, rows=rows)
            raise

    def announce(run: Run) -> None:
        if arguments.json:
            print(json.dumps({"snapshot": run.snapshot_of(arguments.app)}))
        else:
            noun = Noun.APP if run.backed_up_apps == 1 else Noun.APPS
            write_verdict(
                Verdict(
                    Glyph.DONE,
                    "backup done",
                    ((run.backed_up_apps, noun),),
                    elapsed_seconds=host.monotonic() - started,
                )
            )

    _recorded(settings, arguments, step, announce)


class Noun(StrEnum):
    APP = "app"
    APPS = "apps"


def _name_failed_apps(failure: StepFailed, run: Run, *, rows: bool) -> None:
    """The rows show each reason, so the error line counts; `--json` has no rows, so its error
    line carries the reason, which the realm step reads."""
    failed = run.failed_apps
    total = run.backed_up_apps + len(failed)
    noun = Noun.APP if total == 1 else Noun.APPS
    failure.error_line = (
        f"{len(failed)} of {total} {noun} not backed up" if rows else f"{failure.app}: {failure}"
    )
    failure.next_step = f"hal backup {failed[0]}"


def restore_test(settings: Settings, arguments: argparse.Namespace) -> None:
    _require_declared(arguments)

    def step(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
        runner.restore_test(
            settings, apps, run, only=arguments.app, snapshot_prefix=arguments.snapshot
        )

    _recorded(settings, arguments, step)


def snapshots(settings: Settings, _: argparse.Namespace) -> None:
    runner.require_secret_and_repository(settings)
    listing = _command(restic.snapshots, settings)
    print(listing, end="")


def unlock(settings: Settings, _: argparse.Namespace) -> None:
    runner.require_secret_and_repository(settings)
    _command(restic.unlock, settings)


def restore(settings: Settings, arguments: argparse.Namespace) -> None:
    apps = _apps(arguments.apps)
    app = runner.declared_app(apps, arguments.app)
    snapshot_id = _command(
        runner.restore, settings, apps, app, arguments.target, arguments.snapshot
    )
    print(f"hal restore: Snapshot {snapshot_id[:8]} von {app.name} liegt in {arguments.target}")


def _recorded(
    settings: Settings,
    arguments: argparse.Namespace,
    step: Callable[[Settings, tuple[App, ...], Run], None],
    announce: Callable[[Run], None] = lambda _: None,
) -> None:
    """Every outcome lands in the status file; only a run that went through is `ok`."""
    started = host.now()
    try:
        previous = runner.previous_status(settings)
    except StepFailed:
        runner.sweep_leftover_staging(settings.work_dir)
        raise
    run = Run(
        settings.status_file,
        first_attempt(started) if previous is None else previous.attempt(started),
    )
    try:
        # Even a run that fails at once sweeps first, so a killed run's plaintext never waits.
        runner.sweep_leftover_staging(settings.work_dir)
        step(settings, _apps(arguments.apps), run)
    except StepFailed as failure:
        run.failed(failure.category, failure.app, failure.phase)
        raise
    except KeyboardInterrupt as interrupt:
        run.failed(ExitCategory.INTERRUPTED)
        raise Interrupted(run.interrupted_app) from interrupt
    except BaseException:
        run.failed(ExitCategory.INTERNAL)
        raise
    run.succeeded()
    announce(run)


def _require_declared(arguments: argparse.Namespace) -> None:
    """A mistyped app is refused before any status is written: nothing ran."""
    if arguments.app is not None:
        runner.declared_app(_apps(arguments.apps), arguments.app)


def _apps(path: Path) -> tuple[App, ...]:
    try:
        return parse_apps(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StepFailed(
            ExitCategory.CONFIGURATION, f"apps.toml is not readable: {error.strerror}"
        ) from error
    except AppsError as error:
        raise StepFailed(ExitCategory.CONFIGURATION, str(error)) from error


def _command[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return operation(*args, **kwargs)
    except host.HostError as error:
        raise StepFailed(ExitCategory.COMMAND_FAILED, str(error)) from error
