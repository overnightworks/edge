"""Where edge-backup keeps things on this PC, and the pinned images it runs."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from edge_backup.apps import PostgresMajor

RESTIC_IMAGE = (
    "restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510"
)
# The restore test loads a dump into a throwaway server of the app's own major, so it proves the
# version the app really runs; an older server cannot read a newer archive. Major 17 is
# compose.yaml's keycloak-db pin.
RESTORE_IMAGES = {
    PostgresMajor.SIXTEEN: (
        "postgres:16.15-alpine"
        "@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
    ),
    PostgresMajor.SEVENTEEN: (
        "postgres:17.11-alpine"
        "@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"
    ),
}

# Fixed so restic sees one host and the same source paths every run (parent snapshots, forget).
RESTIC_HOSTNAME = "edge-backup"
READINESS_TIMEOUT = timedelta(minutes=10)
READINESS_POLL_SECONDS = 15
# One readiness probe (docker info/ps/inspect, pg_isready); a hung daemon must not outlast the wait.
PROBE_TIMEOUT = timedelta(seconds=30)
RESTORE_SERVER_TIMEOUT = timedelta(minutes=2)
RESTORE_SERVER_POLL_SECONDS = 1


@dataclass(frozen=True)
class Settings:
    repository: Path
    password_file: Path
    status_file: Path
    lock_file: Path
    work_dir: Path
    """Private staging and restore directories; on the home disk, not a possible tmpfs."""
    uid: int
    gid: int

    @classmethod
    def for_home(cls, home: Path, uid: int, gid: int) -> "Settings":
        state = home / ".local/state/edge"
        return cls(
            repository=home / "backups/restic",
            password_file=home / ".secrets/restic/password",
            status_file=state / "backup-status.json",
            lock_file=state / "backup.lock",
            work_dir=home / ".cache/edge-backup",
            uid=uid,
            gid=gid,
        )
