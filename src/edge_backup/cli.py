"""`edge-backup`: exit 0 when the step succeeded, 1 with one German line when it did not."""

import argparse
import os
import signal
import sys
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path

from edge_backup import host, restic, runner
from edge_backup.apps import PRE_APPLY_TAG, App, AppsError, parse_apps
from edge_backup.runner import Run, StepFailed
from edge_backup.settings import Settings
from edge_backup.status import ExitCategory, first_attempt

# uv installs the project editable, so this file sits in the checkout: src/edge_backup/cli.py.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APPS = _PROJECT_ROOT / "backup" / "apps.toml"


class ExitCode(IntEnum):
    OK = 0
    FAILED = 1


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    settings = Settings.for_home(Path.home(), os.getuid(), os.getgid())
    # systemd stops a unit with SIGTERM: as KeyboardInterrupt, cleanup runs and the status fails.
    previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        # Every command holds the one host lock, so no two edge-backup runs share the repository.
        with host.exclusive_lock(settings.lock_file):
            return arguments.command(settings, arguments)
    except StepFailed as failure:
        return _report(failure)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edge-backup",
        description="Encrypted restic backups of the declared apps, and their restore test.",
    )
    parser.add_argument(
        "--apps",
        type=Path,
        default=DEFAULT_APPS,
        help=f"the declared apps (default: {DEFAULT_APPS})",
    )
    commands = parser.add_subparsers(required=True, metavar="COMMAND")
    for name, command, help_text in (
        ("init", _init, "create the restic repository once"),
        ("snapshots", _snapshots, "list the repository's snapshots"),
        ("unlock", _unlock, "remove stale restic locks"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(command=command)
    backup = commands.add_parser(
        "backup", help="dump, scan and back up every app (or one), then forget, prune and check"
    )
    backup.add_argument("app", nargs="?", help="back up this app only")
    backup.add_argument(
        "--pre-apply",
        action="store_true",
        help=f"tag the snapshot {PRE_APPLY_TAG} and keep the last {restic.KEEP_PRE_APPLY} "
        "such snapshots of the app (needs the app's name)",
    )
    backup.set_defaults(command=_backup)
    restore_test = commands.add_parser(
        "restore-test",
        help="restore the last successful backup's snapshot of every app (or one) and prove it "
        "is good",
    )
    restore_test.add_argument("app", nargs="?", help="test this app only, without reading data")
    restore_test.add_argument(
        "--snapshot",
        help="test this snapshot of the app (ID from `snapshots`) instead of the last "
        "successful backup's (needs the app's name)",
    )
    restore_test.set_defaults(command=_restore_test)
    restore = commands.add_parser("restore", help="restore one app's last successful backup")
    restore.add_argument("app", help="the app's name in apps.toml")
    restore.add_argument("--target", type=Path, required=True, help="a directory to create")
    restore.add_argument(
        "--snapshot",
        help="restore this snapshot of the app (ID from `snapshots`) instead of the last "
        "successful backup's: the way when no status file survives",
    )
    restore.set_defaults(command=_restore)
    return parser


def _init(settings: Settings, _: argparse.Namespace) -> int:
    runner.require_password_file(settings)
    settings.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    _command(restic.init, settings)
    print("edge-backup: Repository angelegt")
    return ExitCode.OK


def _backup(settings: Settings, arguments: argparse.Namespace) -> int:
    if arguments.pre_apply and arguments.app is None:
        raise StepFailed(ExitCategory.CONFIGURATION, "--pre-apply braucht den Namen einer App")
    _require_declared(arguments)

    def step(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
        runner.backup(settings, apps, run, only=arguments.app, pre_apply=arguments.pre_apply)

    def announce(run: Run) -> None:
        if arguments.pre_apply:
            print(f"edge-backup: pre-apply-Snapshot {run.snapshot_of(arguments.app)}")

    return _recorded(settings, arguments, step, announce)


def _restore_test(settings: Settings, arguments: argparse.Namespace) -> int:
    if arguments.snapshot is not None and arguments.app is None:
        raise StepFailed(ExitCategory.CONFIGURATION, "--snapshot braucht den Namen einer App")
    _require_declared(arguments)

    def step(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
        runner.restore_test(
            settings, apps, run, only=arguments.app, snapshot_prefix=arguments.snapshot
        )

    return _recorded(settings, arguments, step)


def _snapshots(settings: Settings, _: argparse.Namespace) -> int:
    runner.require_secret_and_repository(settings)
    listing = _command(restic.snapshots, settings)
    print(listing, end="")
    return ExitCode.OK


def _unlock(settings: Settings, _: argparse.Namespace) -> int:
    runner.require_secret_and_repository(settings)
    _command(restic.unlock, settings)
    return ExitCode.OK


def _restore(settings: Settings, arguments: argparse.Namespace) -> int:
    apps = _apps(arguments.apps)
    app = runner.declared_app(apps, arguments.app)
    snapshot_id = _command(
        runner.restore, settings, apps, app, arguments.target, arguments.snapshot
    )
    print(f"edge-backup: Snapshot {snapshot_id[:8]} von {app.name} liegt in {arguments.target}")
    return ExitCode.OK


def _recorded(
    settings: Settings,
    arguments: argparse.Namespace,
    step: Callable[[Settings, tuple[App, ...], Run], None],
    announce: Callable[[Run], None] = lambda _: None,
) -> int:
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
        return _report(failure)
    except KeyboardInterrupt:
        run.failed(ExitCategory.INTERRUPTED)
        return _report(StepFailed(ExitCategory.INTERRUPTED, "abgebrochen"))
    except BaseException:
        run.failed(ExitCategory.INTERNAL)
        raise
    run.succeeded()
    announce(run)
    return ExitCode.OK


def _require_declared(arguments: argparse.Namespace) -> None:
    """A mistyped app is refused before any status is written: nothing ran."""
    if arguments.app is not None:
        runner.declared_app(_apps(arguments.apps), arguments.app)


def _apps(path: Path) -> tuple[App, ...]:
    try:
        return parse_apps(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StepFailed(
            ExitCategory.CONFIGURATION, f"apps.toml ist nicht lesbar: {error.strerror}"
        ) from error
    except AppsError as error:
        raise StepFailed(ExitCategory.CONFIGURATION, str(error)) from error


def _command[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return operation(*args, **kwargs)
    except host.HostError as error:
        raise StepFailed(ExitCategory.COMMAND_FAILED, str(error)) from error


def _report(failure: StepFailed) -> int:
    print(f"edge-backup: fehlgeschlagen: {failure}", file=sys.stderr)
    return ExitCode.FAILED
