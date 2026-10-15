"""The apps' containers and volumes, found by compose labels, and the throwaway restore server."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from edge_backup import host
from edge_backup.apps import App
from edge_backup.restic import Mount, MountKind, container_argv
from edge_backup.settings import POSTGRES_IMAGE, PROBE_TIMEOUT, RESTIC_IMAGE, Settings

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
VOLUME_LABEL = "com.docker.compose.volume"
# `docker compose run` leaves one-off containers with the same project and service labels.
ONEOFF_LABEL = "com.docker.compose.oneoff"
RESTORE_SERVER = "edge-backup-restore-test"
# Marks a throwaway server as edge-backup's own: only a container carrying it is ever removed.
RESTORE_OWNER_LABEL = "edge-backup.role"
RESTORE_OWNER_ROLE = "restore-test"
RESTORE_DATABASE = "restore_test"
# The image's superuser; `docker exec` runs as root, which is no role in the server.
RESTORE_ROLE = "postgres"
_SCANNED_AUDIO = "/audio"
_READY_STATES = frozenset({"running", "running healthy"})
# What `docker run --detach` prints: the full ID, never a name or a shortened one.
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


class NotReadyError(RuntimeError):
    """Docker or the app's database is not up yet; worth waiting for."""


class TargetError(RuntimeError):
    """The labels do not name exactly one container or volume; waiting will not fix that."""


def require_daemon() -> None:
    try:
        host.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            label="docker info",
            timeout=PROBE_TIMEOUT,
        )
    except host.HostCommandError as error:
        raise NotReadyError(f"Docker antwortet nicht: {error}") from error


def ensure_pinned_images() -> None:
    """`docker image prune -a` removes the pinned images; a missing one is pulled by its digest."""
    for image in (RESTIC_IMAGE, POSTGRES_IMAGE):
        if not _image_present(image):
            _pull(image)


def _image_present(image: str) -> bool:
    try:
        host.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            label="docker image inspect",
            timeout=PROBE_TIMEOUT,
            discard_stderr=True,
        )
    except host.HostCommandError:
        return False
    return True


def _pull(image: str) -> None:
    try:
        host.run(
            ["docker", "pull", "--quiet", image], label="docker pull", discard_stderr=True
        )
    except host.HostCommandError as error:
        raise TargetError(f"das gepinnte Image {image} fehlt und lässt sich nicht laden") from error


def database_container(app: App) -> str:
    try:
        ids = _lines(
            host.run(
                ["docker", "ps", "--all", "--quiet", "--no-trunc",
                 *_label_filter(PROJECT_LABEL, app.compose_project),
                 *_label_filter(SERVICE_LABEL, app.db_service),
                 *_label_filter(ONEOFF_LABEL, "False")],
                label="docker ps",
                timeout=PROBE_TIMEOUT,
            )
        )  # fmt: skip
        if len(ids) > 1:
            raise TargetError(f"{len(ids)} Datenbank-Container für {app.name} statt genau einem")
        if not ids:
            raise NotReadyError(f"kein Datenbank-Container für {app.name}")
        state = host.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", ids[0]],
            label="docker inspect",
            timeout=PROBE_TIMEOUT,
        )  # fmt: skip
    except host.HostCommandError as error:
        raise NotReadyError(f"Docker antwortet nicht: {error}") from error
    if state.decode().strip() not in _READY_STATES:
        raise NotReadyError(f"die Datenbank von {app.name} ist nicht bereit")
    return ids[0]


def audio_volume(app: App) -> str:
    names = _lines(
        host.run(
            ["docker", "volume", "ls", "--quiet",
             *_label_filter(PROJECT_LABEL, app.compose_project),
             *_label_filter(VOLUME_LABEL, app.volume)],
            label="docker volume ls",
            timeout=PROBE_TIMEOUT,
        )
    )  # fmt: skip
    if len(names) != 1:
        raise TargetError(f"{len(names)} Volumes {app.volume} für {app.name} statt genau einem")
    return names[0]


def dump_database(app: App, container_id: str, target: Path) -> None:
    host.run(
        ["docker", "exec", "-i", container_id, "pg_dump",
         "--username", app.db_user, "--format=custom", "--dbname", app.db_name],
        label="pg_dump",
        stdout=target,
    )  # fmt: skip


def count_audio_files(settings: Settings, kind: MountKind, source: str) -> int:
    """Regular files under a volume or host directory, counted read-only in a container.

    The one rule for the manifest's count and the restored copy's; names never leave it.
    """
    listing = host.run(
        [
            *container_argv(settings, Mount(kind, source, _SCANNED_AUDIO), entrypoint="find"),
            _SCANNED_AUDIO, "-type", "f", "-print0",
        ],
        label="Zählen der Audiodateien",
    )  # fmt: skip
    return listing.count(b"\0")


@contextmanager
def restore_server() -> Iterator["RestoreServer"]:
    """A PostgreSQL without network or volume of ours; removed again whatever happens.

    Removal goes by container ID and only to containers with our owner label: a foreign
    container holding the name makes the start fail instead of being deleted.
    """
    _remove_owned_restore_servers()
    started = host.run(
        ["docker", "run", "--detach", "--name", RESTORE_SERVER, "--network", "none",
         "--label", f"{RESTORE_OWNER_LABEL}={RESTORE_OWNER_ROLE}",
         # trust is safe here: the server has no network and holds only the restored copy.
         "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", f"POSTGRES_DB={RESTORE_DATABASE}",
         POSTGRES_IMAGE],
        label="Start der Wegwerf-Datenbank",
    )  # fmt: skip
    try:
        yield RestoreServer(_started_container(started))
    finally:
        # By name and owner label, not by the reported ID: the server goes even when
        # docker run's output could not be read.
        _remove_owned_restore_servers()


@dataclass(frozen=True)
class RestoreServer:
    container_id: str

    def is_ready(self) -> bool:
        """Asked over TCP: the image's init server listens on the socket only, then restarts."""
        try:
            self._exec(
                ["pg_isready", "--host", "127.0.0.1", "--quiet"],
                label="pg_isready",
                timeout=PROBE_TIMEOUT,
            )
        except host.HostCommandError:
            return False
        return True

    def list_dump(self, dump: Path) -> None:
        self._exec(["pg_restore", "--list"], label="pg_restore --list", stdin=dump)

    def load_dump(self, dump: Path) -> None:
        self._exec(
            ["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner",
             "--no-privileges", "--username", RESTORE_ROLE, "--dbname", RESTORE_DATABASE],
            label="pg_restore",
            stdin=dump,
        )  # fmt: skip

    def row_count(self, table: str) -> int:
        output = self._query(f'SELECT count(*) FROM "{table}"')
        try:
            return int(output.strip(b"\0\n"))
        except ValueError as error:
            raise host.UnreadableOutputError(f"die Zeilenzahl von {table} ist unlesbar") from error

    def column_values(self, table: str, column: str) -> list[str]:
        """The non-empty values; identifiers are validated by apps.toml, so quoting suffices."""
        output = self._query(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" <> \'\''
        )
        return [value.decode() for value in output.strip(b"\n").split(b"\0") if value]

    def _query(self, sql: str) -> bytes:
        return self._exec(
            ["psql", "--username", RESTORE_ROLE, "--dbname", RESTORE_DATABASE, "--no-psqlrc",
             "--tuples-only", "--no-align", "--record-separator-zero",
             "--set", "ON_ERROR_STOP=1", "--command", sql],
            label="psql",
        )  # fmt: skip

    def _exec(
        self,
        command: list[str],
        *,
        label: str,
        stdin: Path | None = None,
        timeout: timedelta | None = None,
    ) -> bytes:
        interactive = ["-i"] if stdin else []
        return host.run(
            ["docker", "exec", *interactive, self.container_id, *command],
            label=label,
            stdin=stdin,
            timeout=timeout,
        )


def _started_container(run_output: bytes) -> str:
    match _lines(run_output):
        case [container_id] if _CONTAINER_ID.fullmatch(container_id):
            return container_id
        case _:
            raise host.UnreadableOutputError("docker run meldete keine Container-ID")


def _remove_owned_restore_servers() -> None:
    for container_id in _lines(
        host.run(
            ["docker", "ps", "--all", "--quiet", "--no-trunc",
             "--filter", f"name=^{RESTORE_SERVER}$",
             *_label_filter(RESTORE_OWNER_LABEL, RESTORE_OWNER_ROLE)],
            label="docker ps",
        )
    ):  # fmt: skip
        _remove_restore_server(container_id)


def _remove_restore_server(container_id: str) -> None:
    host.run(
        ["docker", "rm", "--force", "--volumes", container_id],
        label="Entfernen der Wegwerf-Datenbank",
    )


def _label_filter(label: str, value: str) -> list[str]:
    return ["--filter", f"label={label}={value}"]


def _lines(output: bytes) -> list[str]:
    try:
        return output.decode().split()
    except UnicodeDecodeError as error:
        raise host.UnreadableOutputError("docker meldete Ausgabe, die kein UTF-8 ist") from error
