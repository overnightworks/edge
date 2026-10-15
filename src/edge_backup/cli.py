"""`edge-backup`: exit 0 when the step succeeded, 1 with one German line when it did not."""

import argparse
import os
import signal
import sys
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path

from edge_backup import host, restic, runner
from edge_backup.apps import App, AppsError, parse_apps
from edge_backup.runner import Run, StepFailed
from edge_backup.settings import Settings
from edge_backup.status import ExitCategory, StatusFileError, first_attempt, read_status

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
        ("backup", _backup, "dump, scan and back up every app, then forget, prune and check"),
        ("restore-test", _restore_test, "restore the latest snapshots and prove they are good"),
        ("snapshots", _snapshots, "list the repository's snapshots"),
        ("unlock", _unlock, "remove stale restic locks"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(command=command)
    restore = commands.add_parser("restore", help="restore one app's latest snapshot")
    restore.add_argument("app", help="the app's name in apps.toml")
    restore.add_argument("--target", type=Path, required=True, help="a directory to create")
    restore.set_defaults(command=_restore)
    return parser


def _init(settings: Settings, _: argparse.Namespace) -> int:
    runner.require_password_file(settings)
    settings.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    _command(restic.init, settings)
    print("edge-backup: Repository angelegt")
    return ExitCode.OK


def _backup(settings: Settings, arguments: argparse.Namespace) -> int:
    return _recorded(settings, arguments, runner.backup)


def _restore_test(settings: Settings, arguments: argparse.Namespace) -> int:
    return _recorded(settings, arguments, runner.restore_test)


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
    apps = {app.name: app for app in _apps(arguments.apps)}
    if (app := apps.get(arguments.app)) is None:
        raise StepFailed(ExitCategory.CONFIGURATION, f"{arguments.app} ist nicht deklariert")
    if arguments.target.exists():
        raise StepFailed(ExitCategory.CONFIGURATION, "das Ziel existiert schon")
    snapshot_id = _command(runner.restore, settings, app, arguments.target)
    print(f"edge-backup: Snapshot {snapshot_id[:8]} von {app.name} liegt in {arguments.target}")
    return ExitCode.OK


def _recorded(
    settings: Settings,
    arguments: argparse.Namespace,
    step: Callable[[Settings, tuple[App, ...], Run], None],
) -> int:
    """Every outcome lands in the status file; only a full run is `ok`."""
    runner.sweep_leftover_staging(settings.work_dir)
    try:
        previous = read_status(settings.status_file)
    except StatusFileError as error:
        run = Run(settings.status_file, first_attempt(host.now()))
        run.failed(ExitCategory.CONFIGURATION)
        return _report(StepFailed(ExitCategory.CONFIGURATION, str(error)))
    started = host.now()
    run = Run(
        settings.status_file,
        first_attempt(started) if previous is None else previous.attempt(started),
    )
    try:
        step(settings, _apps(arguments.apps), run)
    except StepFailed as failure:
        run.failed(failure.category)
        return _report(failure)
    except KeyboardInterrupt:
        run.failed(ExitCategory.INTERRUPTED)
        return _report(StepFailed(ExitCategory.INTERRUPTED, "abgebrochen"))
    except BaseException:
        run.failed(ExitCategory.INTERNAL)
        raise
    run.succeeded()
    return ExitCode.OK


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
