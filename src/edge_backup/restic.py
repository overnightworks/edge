"""restic, run from its pinned image: the repository and the password file are mounted, never read.

The password reaches restic only as a read-only file behind `--password-file`.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from edge_backup import host
from edge_backup.apps import PRE_APPLY_TAG
from edge_backup.settings import RESTIC_HOSTNAME, RESTIC_IMAGE, Settings

REPOSITORY = "/repo"
PASSWORD_FILE = "/run/secrets/restic-password"
RESTORE_TARGET = "/restore"
SCRATCH = "/scratch"
RETRY_LOCK = "5m"
KEEP_WEEKLY = 4
KEEP_MONTHLY = 3
# A snapshot taken right before a change is applied carries this tag besides its app's name.
KEEP_PRE_APPLY = 10
CHECK_READ_DATA_SUBSET = "10%"
# The snapshot layout under an app's source path: the dump and manifest, and the audio.
DB_FOLDER = "db"
AUDIO_FOLDER = "audio"


class MountKind(StrEnum):
    BIND = "bind"
    VOLUME = "volume"


@dataclass(frozen=True)
class Mount:
    kind: MountKind
    source: str
    """A host path for a bind mount (it must exist; Docker never creates it), else a volume."""
    target: str
    readonly: bool = True

    def as_argument(self) -> str:
        argument = f"type={self.kind},source={self.source},target={self.target}"
        return f"{argument},readonly" if self.readonly else argument


def app_source(app: str) -> str:
    """The fixed in-container path an app's snapshot holds: DB_FOLDER and AUDIO_FOLDER."""
    return f"/backup/{app}"


def container_argv(settings: Settings, *mounts: Mount, entrypoint: str | None = None) -> list[str]:
    """`docker run` with no network, no privileges and a read-only root, as the operator's user."""
    argv = [
        "docker", "run", "--rm",
        "--network", "none",
        "--hostname", RESTIC_HOSTNAME,
        "--user", f"{settings.uid}:{settings.gid}",
        "--read-only",
        # restic stages pack files in TMPDIR: a tmpfs only this container's user can enter.
        "--tmpfs", f"{SCRATCH}:mode=0700,uid={settings.uid},gid={settings.gid}",
        "--env", f"TMPDIR={SCRATCH}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    ]  # fmt: skip
    argv += [argument for mount in mounts for argument in ("--mount", mount.as_argument())]
    if entrypoint is not None:
        argv += ["--entrypoint", entrypoint]
    return [*argv, RESTIC_IMAGE]


def restic_argv(settings: Settings, command: list[str], *mounts: Mount) -> list[str]:
    repository = Mount(MountKind.BIND, str(settings.repository), REPOSITORY, readonly=False)
    password = Mount(MountKind.BIND, str(settings.password_file), PASSWORD_FILE)
    return [
        *container_argv(settings, repository, password, *mounts),
        "--repo", REPOSITORY,
        "--password-file", PASSWORD_FILE,
        "--no-cache",
        "--retry-lock", RETRY_LOCK,
        *command,
    ]  # fmt: skip


def init(settings: Settings) -> None:
    _restic(settings, "restic init", ["init"])


def backup(
    settings: Settings,
    app: str,
    staging: Path,
    audio_volume: str | None,
    *,
    pre_apply: bool = False,
) -> str:
    """One snapshot holding dump, manifest and audio (none for a DB-only app); any non-zero
    exit (3 too) is a failure."""
    mounts = [Mount(MountKind.BIND, str(staging), f"{app_source(app)}/{DB_FOLDER}")]
    if audio_volume is not None:
        mounts.append(Mount(MountKind.VOLUME, audio_volume, f"{app_source(app)}/{AUDIO_FOLDER}"))
    output = _restic(
        settings,
        "restic backup",
        ["backup", "--json", *_tag(app, pre_apply=pre_apply), app_source(app)],
        *mounts,
    )
    return _snapshot_id(output)


def forget(settings: Settings, app: str, *, pre_apply: bool = False) -> None:
    """The weekly policy never deletes a pre-apply snapshot; a pre-apply run thins only those."""
    _restic(
        settings,
        "restic forget",
        [
            "forget", *_scope(app, pre_apply=pre_apply), "--group-by", "host,tags",
            *(_pre_apply_policy() if pre_apply else _weekly_policy()),
            "--prune",
        ],
    )  # fmt: skip


def _pre_apply_policy() -> list[str]:
    return ["--keep-last", str(KEEP_PRE_APPLY)]


def _weekly_policy() -> list[str]:
    return [
        "--keep-weekly", str(KEEP_WEEKLY), "--keep-monthly", str(KEEP_MONTHLY),
        "--keep-tag", PRE_APPLY_TAG,
    ]  # fmt: skip


def check(settings: Settings, *, read_data: bool) -> None:
    subset = [f"--read-data-subset={CHECK_READ_DATA_SUBSET}"] if read_data else []
    _restic(settings, "restic check", ["check", *subset])


def latest_snapshot(settings: Settings, app: str) -> str | None:
    ids = snapshot_ids(settings, app, "--latest", "1")
    return ids[-1] if ids else None


def has_snapshot(settings: Settings, app: str, snapshot_id: str) -> bool:
    return snapshot_id in snapshot_ids(settings, app)


def snapshot_ids(settings: Settings, app: str, *options: str) -> list[str]:
    output = _restic(settings, "restic snapshots", ["snapshots", "--json", *options, *_scope(app)])
    try:
        return [snapshot["id"] for snapshot in json.loads(output)]
    except (ValueError, KeyError, TypeError) as error:
        raise host.UnreadableOutputError("restic snapshots ist unlesbar") from error


def restore(settings: Settings, app: str, snapshot_id: str, target: Path) -> None:
    """The app's source path lands directly in target: DB_FOLDER and AUDIO_FOLDER below it."""
    _restic(
        settings,
        "restic restore",
        ["restore", f"{snapshot_id}:{app_source(app)}", "--target", RESTORE_TARGET],
        Mount(MountKind.BIND, str(target), RESTORE_TARGET, readonly=False),
    )


def snapshots(settings: Settings) -> str:
    return _restic(settings, "restic snapshots", ["snapshots"]).decode()


def unlock(settings: Settings) -> None:
    """Removes only stale restic locks; a live run's lock stays."""
    _restic(settings, "restic unlock", ["unlock"])


# restic's own exit codes for a repository that does not exist, is locked, or rejects the password.
REPOSITORY_EXIT_CODES = frozenset({10, 11, 12})


class RepositoryUnusableError(host.HostCommandError):
    """restic says the repository, not the app being worked on, is why it failed."""


def _restic(settings: Settings, label: str, command: list[str], *mounts: Mount) -> bytes:
    try:
        return host.run(restic_argv(settings, command, *mounts), label=label)
    except host.HostCommandError as error:
        if error.exit_code in REPOSITORY_EXIT_CODES:
            raise RepositoryUnusableError(
                label, str(error).removeprefix(f"{label} "), error.exit_code
            ) from error
        raise


def _scope(app: str, *, pre_apply: bool = False) -> list[str]:
    return ["--host", RESTIC_HOSTNAME, *_tag(app, pre_apply=pre_apply)]


def _tag(app: str, *, pre_apply: bool = False) -> list[str]:
    """Each app's snapshots carry its name as their tag; restic reads a comma list as all of them
    when it selects and as the whole set when it tags."""
    return ["--tag", f"{app},{PRE_APPLY_TAG}" if pre_apply else app]


def _snapshot_id(backup_output: bytes) -> str:
    try:
        messages = [json.loads(line) for line in backup_output.splitlines() if line.strip()]
        return next(m["snapshot_id"] for m in messages if m.get("message_type") == "summary")
    except (ValueError, KeyError, AttributeError, StopIteration) as error:
        raise host.UnreadableOutputError("restic backup meldete keinen Snapshot") from error
