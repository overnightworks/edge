"""The weekly backup and the monthly restore test, phase by phase, each phase in the status.

A run counts as success only after its last phase; every failure names its phase.
"""

import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Self

from edge_backup import docker, host, restic
from edge_backup.apps import App
from edge_backup.restic import MountKind
from edge_backup.settings import (
    READINESS_POLL_SECONDS,
    READINESS_TIMEOUT,
    RESTORE_SERVER_POLL_SECONDS,
    RESTORE_SERVER_TIMEOUT,
    Settings,
)
from edge_backup.status import (
    ExitCategory,
    Phase,
    Status,
    StatusFileError,
    read_status,
    write_status,
)
from edge_backup.verify import (
    DUMP_NAME,
    MANIFEST_NAME,
    Manifest,
    VerificationError,
    check_audio_count,
    check_audio_references,
)

# A run stages in `staging-<random>` and first creates `staging-<random>.run` beside it: that
# mark, not the name, proves the directory is a run's own, so a sweep removes only what runs made.
STAGING_PREFIX = "staging-"
RUN_MARK_SUFFIX = ".run"
# restic's own short ID length, as `edge-backup snapshots` lists it.
MIN_SNAPSHOT_PREFIX = 8


class StepFailed(Exception):
    """A handled failure: the run is over, the status says `failed`, the message is German."""

    def __init__(self, category: ExitCategory, message: str, *, shared: bool = False) -> None:
        self.category = category
        self.shared = shared
        self.app: str | None = None
        self.phase: Phase | None = None
        super().__init__(message)


class Run:
    """The status file as this run's journal: every phase change is written at once."""

    def __init__(self, path: Path, status: Status) -> None:
        self._path = path
        self._status = status
        self._first_app_failure: StepFailed | None = None
        write_status(path, status)

    @contextmanager
    def phase(
        self, phase: Phase, failing: ExitCategory = ExitCategory.COMMAND_FAILED
    ) -> Iterator[None]:
        self._record(self._status.entering(phase))
        try:
            yield
        except host.HostError as error:
            raise StepFailed(
                failing,
                f"Phase {phase}: {error}",
                shared=isinstance(error, restic.RepositoryUnusableError),
            ) from error
        except VerificationError as error:
            raise StepFailed(ExitCategory.VERIFICATION_FAILED, f"Phase {phase}: {error}") from error

    @contextmanager
    def isolated(self, app: str) -> Iterator[None]:
        """One app's failure is kept for the verdict while the other apps go on."""
        try:
            yield
        except StepFailed as failure:
            if failure.shared:
                raise
            failure.app, failure.phase = app, self._status.phase
            self._first_app_failure = self._first_app_failure or failure

    def raise_first_app_failure(self) -> None:
        if self._first_app_failure is not None:
            raise self._first_app_failure

    def snapshot_of(self, app: str) -> str | None:
        return self._status.snapshot_of(app)

    def attribute_legacy_evidence(self, settings: Settings, apps: tuple[App, ...]) -> None:
        attributed = attributed_status(settings, self._status, apps)
        if attributed is not self._status:
            self._record(attributed)

    def backed_up(self, app: str, snapshot_id: str) -> None:
        self._record(self._status.backed_up(app, host.now(), snapshot_id))

    def restore_tested(self, apps: list[str]) -> None:
        self._record(self._status.restore_tested(host.now(), apps))

    def succeeded(self) -> None:
        self._record(self._status.succeeded())

    def failed(
        self, category: ExitCategory, app: str | None = None, phase: Phase | None = None
    ) -> None:
        self._record(self._status.failed(category, app, phase))

    def _record(self, status: Status) -> None:
        self._status = status
        write_status(self._path, status)


def require_secret_and_repository(settings: Settings) -> None:
    require_password_file(settings)
    if not (settings.repository / "config").is_file():
        raise StepFailed(
            ExitCategory.CONFIGURATION,
            "das restic-Repository ist nicht initialisiert (README: Einrichtung)",
        )


def require_password_file(settings: Settings) -> None:
    """The file is only stat-ed here; its content never passes through this process."""
    try:
        mode = settings.password_file.stat().st_mode
    except FileNotFoundError:
        raise StepFailed(
            ExitCategory.CONFIGURATION, "die Passwortdatei fehlt (README: Einrichtung)"
        ) from None
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
        raise StepFailed(
            ExitCategory.CONFIGURATION, "die Passwortdatei muss eine Datei mit Modus 600 sein"
        )


@dataclass(frozen=True)
class _Deadline:
    """One waiting budget that several consecutive waits draw from."""

    timeout: timedelta
    expires_at: float

    @classmethod
    def start(cls, timeout: timedelta = READINESS_TIMEOUT) -> Self:
        return cls(timeout, host.monotonic() + timeout.total_seconds())


def backup(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
    with run.phase(Phase.PREPARE, failing=ExitCategory.CONFIGURATION):
        require_secret_and_repository(settings)
    first_readiness = _Deadline.start(READINESS_TIMEOUT)
    _require_docker_with_pinned_images(run, first_readiness)
    with run.phase(Phase.PREPARE):
        run.attribute_legacy_evidence(settings, apps)
    for index, app in enumerate(apps):
        with run.isolated(app.name):
            _back_up(settings, app, run, first_readiness if index == 0 else _Deadline.start())
    run.raise_first_app_failure()
    for app in apps:
        with run.phase(Phase.FORGET):
            restic.forget(settings, app.name)
    with run.phase(Phase.CHECK):
        restic.check(settings, read_data=False)


def restore_test(settings: Settings, apps: tuple[App, ...], run: Run) -> None:
    with run.phase(Phase.PREPARE, failing=ExitCategory.CONFIGURATION):
        require_secret_and_repository(settings)
    _require_docker_with_pinned_images(run, _Deadline.start())
    with run.phase(Phase.PREPARE):
        run.attribute_legacy_evidence(settings, apps)
    verified = []
    for app in apps:
        with run.isolated(app.name):
            _restore_test(settings, app, run)
            verified.append(app.name)
    with run.phase(Phase.CHECK):
        restic.check(settings, read_data=True)
    run.restore_tested(verified)
    run.raise_first_app_failure()


def _require_docker_with_pinned_images(run: Run, readiness: _Deadline) -> None:
    """A daemon that is still starting is waited for; only then are missing images pulled."""
    with run.phase(Phase.READINESS):
        _wait_until_ready(docker.require_daemon, readiness, READINESS_POLL_SECONDS)
    with run.phase(Phase.PREPARE, failing=ExitCategory.CONFIGURATION):
        _resolved(docker.ensure_pinned_images)


def previous_status(settings: Settings) -> Status | None:
    """The file holds the only record of past successes: unreadable, it is left untouched."""
    try:
        return read_status(settings.status_file)
    except StatusFileError as error:
        raise StepFailed(
            ExitCategory.CONFIGURATION, f"{error}; die Datei bleibt unverändert"
        ) from error


def attributed_status(settings: Settings, status: Status, apps: tuple[App, ...]) -> Status:
    """Legacy evidence belongs to the one declared app only if restic knows its snapshot there."""
    legacy = status.unattributed
    if legacy is None or legacy.last_snapshot_id is None or len(apps) != 1:
        return status
    [app] = apps
    if app.name in status.apps or not restic.has_snapshot(
        settings, app.name, legacy.last_snapshot_id
    ):
        return status
    return status.attributed_to(app.name)


def restore(
    settings: Settings,
    apps: tuple[App, ...],
    app: App,
    target: Path,
    snapshot_prefix: str | None,
) -> str:
    """The operator's restore into a new private directory: the last successful backup's snapshot,
    or the one of this app that the operator names.

    Never inside the work directory: the operator's plaintext stays out of what runs own and sweep.
    """
    if target.resolve().is_relative_to(settings.work_dir.resolve()):
        raise StepFailed(
            ExitCategory.CONFIGURATION, "das Ziel liegt im Arbeitsverzeichnis von edge-backup"
        )
    if target.exists():
        raise StepFailed(ExitCategory.CONFIGURATION, "das Ziel existiert schon")
    require_secret_and_repository(settings)
    snapshot_id = (
        _last_successful_snapshot(settings, app, _recorded_snapshot(settings, apps, app))
        if snapshot_prefix is None
        else _chosen_snapshot(settings, app, snapshot_prefix)
    )
    target.mkdir(mode=0o700, parents=True)
    restic.restore(settings, app.name, snapshot_id, target)
    return snapshot_id


def _back_up(settings: Settings, app: App, run: Run, readiness: _Deadline) -> None:
    with run.phase(Phase.READINESS):
        container = _wait_until_ready(
            lambda: docker.database_container(app), readiness, READINESS_POLL_SECONDS
        )
        volume = _resolved(lambda: docker.audio_volume(app))
    with _private_directory(settings.work_dir) as staging:
        with run.phase(Phase.DUMP):
            docker.dump_database(app, container, staging / DUMP_NAME)
        with run.phase(Phase.MANIFEST):
            audio_files = docker.count_audio_files(settings, MountKind.VOLUME, volume)
            manifest = Manifest(app.name, audio_files)
            _write_private(staging / MANIFEST_NAME, manifest.as_json())
        with run.phase(Phase.BACKUP):
            run.backed_up(app.name, restic.backup(settings, app.name, staging, volume))


def _restore_test(settings: Settings, app: App, run: Run) -> None:
    with run.phase(Phase.RESTORE):
        snapshot_id = _last_successful_snapshot(settings, app, run.snapshot_of(app.name))
    with _private_directory(settings.work_dir) as restored:
        with run.phase(Phase.RESTORE):
            restic.restore(settings, app.name, snapshot_id, restored)
        with run.phase(Phase.VERIFY):
            _verify(settings, app, restored, run)


def _verify(settings: Settings, app: App, restored: Path, run: Run) -> None:
    """Only what the copy shows fails verification; a container that will not run is a command."""
    restored_db, restored_audio = restored / restic.DB_FOLDER, restored / restic.AUDIO_FOLDER
    manifest_file = _restored_file(restored_db / MANIFEST_NAME, "kein Manifest")
    manifest = Manifest.from_json(manifest_file.read_text(encoding="utf-8"))
    dump = _restored_file(restored_db / DUMP_NAME, "keinen Datenbank-Dump")
    check_audio_count(
        manifest, docker.count_audio_files(settings, MountKind.BIND, str(restored_audio))
    )
    with docker.restore_server() as server:
        _wait_until_ready(
            lambda: _ready_or_raise(server),
            _Deadline.start(RESTORE_SERVER_TIMEOUT),
            RESTORE_SERVER_POLL_SECONDS,
        )
        with run.phase(Phase.VERIFY, failing=ExitCategory.VERIFICATION_FAILED):
            server.list_dump(dump)
            server.load_dump(dump)
            server.row_count(app.restore_test.count_table)
            references = server.column_values(
                app.restore_test.audio_reference_table, app.restore_test.audio_reference_column
            )
    check_audio_references(references, restored_audio)


def _recorded_snapshot(settings: Settings, apps: tuple[App, ...], app: App) -> str | None:
    """A restore only reads the status: legacy evidence is attributed in memory, never written."""
    status = previous_status(settings)
    if status is None:
        return None
    return attributed_status(settings, status, apps).snapshot_of(app.name)


def _last_successful_snapshot(settings: Settings, app: App, recorded: str | None) -> str:
    """Never a newer snapshot a failed run left behind; if it is gone, the newest is only named."""
    newest = _latest_snapshot(settings, app)
    named = f"der neueste Snapshot ist {newest[:8]}; wählbar mit: " + (
        f"edge-backup restore {app.name} --snapshot <ID> --target <Ordner>"
    )
    if recorded is None:
        raise StepFailed(
            ExitCategory.CONFIGURATION,
            f"kein erfolgreiches Backup von {app.name} aufgezeichnet; {named}",
        )
    if not restic.has_snapshot(settings, app.name, recorded):
        raise StepFailed(
            ExitCategory.CONFIGURATION,
            f"der aufgezeichnete Snapshot {recorded[:8]} von {app.name} fehlt im Repository; "
            f"{named}",
        )
    return recorded


def _chosen_snapshot(settings: Settings, app: App, prefix: str) -> str:
    """Only this app's snapshots count, so another app's ID is as unknown as a mistyped one."""
    matches = [
        snapshot_id
        for snapshot_id in restic.snapshot_ids(settings, app.name)
        if len(prefix) >= MIN_SNAPSHOT_PREFIX and snapshot_id.startswith(prefix)
    ]
    if len(matches) != 1:
        raise StepFailed(
            ExitCategory.CONFIGURATION,
            f"{prefix} nennt keinen einzelnen Snapshot von {app.name} "
            f"(mindestens {MIN_SNAPSHOT_PREFIX} Zeichen der ID aus `edge-backup snapshots`)",
        )
    return matches[0]


def _latest_snapshot(settings: Settings, app: App) -> str:
    snapshot_id = restic.latest_snapshot(settings, app.name)
    if snapshot_id is None:
        raise StepFailed(ExitCategory.VERIFICATION_FAILED, f"kein Snapshot von {app.name}")
    return snapshot_id


def _wait_until_ready[T](probe: Callable[[], T], deadline: _Deadline, poll_seconds: float) -> T:
    while True:
        try:
            return _resolved(probe)
        except docker.NotReadyError as error:
            if host.monotonic() >= deadline.expires_at:
                minutes = int(deadline.timeout.total_seconds() // 60)
                raise StepFailed(
                    ExitCategory.NOT_READY, f"nach {minutes} Minuten nicht bereit: {error}"
                ) from error
            host.sleep(poll_seconds)


def _resolved[T](probe: Callable[[], T]) -> T:
    try:
        return probe()
    except docker.TargetError as error:
        raise StepFailed(ExitCategory.CONFIGURATION, str(error)) from error


def _ready_or_raise(server: docker.RestoreServer) -> None:
    if not server.is_ready():
        raise docker.NotReadyError("die Wegwerf-Datenbank startet nicht")


def _restored_file(path: Path, missing: str) -> Path:
    if not path.is_file():
        raise VerificationError(f"der Snapshot enthält {missing}")
    return path


def _write_private(path: Path, text: str) -> None:
    path.touch(mode=0o600, exist_ok=False)
    path.write_text(text, encoding="utf-8")


def sweep_leftover_staging(parent: Path) -> None:
    """A killed run never reaches its cleanup, so every run first removes the plaintext it left,
    whether or not it gets far enough to stage anything itself.

    Only directories with a run's mark go; anything else here is left alone.
    The host lock guarantees no other run is using them.
    """
    try:
        for mark in parent.glob(f"{STAGING_PREFIX}*{RUN_MARK_SUFFIX}"):
            _remove_staging(mark)
    except OSError as error:
        raise StepFailed(
            ExitCategory.INTERNAL,
            f"der Klartext eines abgebrochenen Laufs lässt sich nicht entfernen: {error.strerror}",
        ) from error


@contextmanager
def _private_directory(parent: Path) -> Iterator[Path]:
    """A 0700 directory for dumps and restored copies, removed on success and on failure.

    The directory is reserved under a fresh name before it is marked, so the mark never
    claims a directory this run did not create; plaintext enters only once it is marked.
    """
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    mark = _run_mark(directory)
    try:
        mark.touch(mode=0o600, exist_ok=False)
    except BaseException:
        directory.rmdir()
        raise
    try:
        yield directory
    finally:
        _remove_staging(mark)


def _run_mark(directory: Path) -> Path:
    return directory.with_name(directory.name + RUN_MARK_SUFFIX)


def _staged_directory(mark: Path) -> Path:
    return mark.with_name(mark.name.removesuffix(RUN_MARK_SUFFIX))


def _remove_staging(mark: Path) -> None:
    """The directory goes before its mark, so a run killed in between leaves it findable."""
    directory = _staged_directory(mark)
    if directory.exists():
        shutil.rmtree(directory)
    mark.unlink()
