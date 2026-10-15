"""A scripted Docker host in place of docker and restic, for the edge-backup test modules."""

import fcntl
import json
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from edge_backup import host
from edge_backup.docker import RESTORE_OWNER_LABEL, RESTORE_OWNER_ROLE

CANARY_PASSWORD = "canary-7f3e-never-print-me"
DUMP = b"PGDMP custom archive"
AUDIO = {"u1/one.mp3": b"ID3 one", "u1/two.mp3": b"ID3 two", "u2/cover.png": b"PNG"}
SONGMAKER_CONTAINER = "c0ffee"
RESTORE_SERVER_ID = "deadbeef" * 8
STATUS_KEYS = {
    "schema",
    "state",
    "phase",
    "failing_phase",
    "exit_category",
    "last_attempt",
    "last_successful_backup",
    "last_successful_restore_test",
    "last_snapshot_id",
}
SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
# The real boundary, kept before the `machine` fixture swaps it: exits and files go through it.
REAL_HOST_RUN = host.run


@dataclass
class Machine:
    """Answers each fixed argv the way docker, restic and the throwaway postgres would."""

    audio: dict[str, bytes] = field(default_factory=lambda: dict(AUDIO))
    references: list[str] = field(default_factory=lambda: ["u1/one.mp3", "u1/two.mp3"])
    db_containers: list[str] = field(default_factory=lambda: [SONGMAKER_CONTAINER])
    volumes: list[str] = field(default_factory=lambda: ["songmaker_audiofiles"])
    clock: "Clock" = field(default_factory=lambda: Clock())
    absent_polls: int = 0
    """How many `docker ps` polls find no database container before it appears."""
    hanging: set[str] = field(default_factory=set)
    """Labels of probes that never answer; only a timeout ends them."""
    leftover_servers: list[str] = field(default_factory=list)
    """Throwaway servers a killed restore test left behind; they carry the owner label."""
    created_servers: list[str] = field(default_factory=list)
    """Throwaway servers `docker run` created; they carry the owner label too."""
    foreign_server_name: bool = False
    """A container that is not edge-backup's already holds the throwaway server's name."""
    server_start_output: bytes = f"{RESTORE_SERVER_ID}\n".encode()
    """What `docker run` prints once it has created the throwaway server."""
    snapshot_without: set[str] = field(default_factory=set)
    """Staged files a snapshot lacks, as if they had vanished before restic's scan."""
    starting_server_polls: int = 1
    """How many pg_isready polls find the throwaway server still starting."""
    server_accepting: bool = False
    """pg_isready has said yes since the throwaway server started."""
    written_during_scan: dict[str, bytes] = field(default_factory=dict)
    """Audio that lands in the volume after the manifest's count, before restic's scan."""
    failing: dict[str, int | type[BaseException]] = field(default_factory=dict)
    snapshots: list[dict] = field(default_factory=list)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    status_seen_during_backup: dict | None = None
    status_file: Path | None = None
    lock_free_during: set[str] = field(default_factory=set)
    """Labels of commands that ran while the host lock was free."""
    missing_images: set[str] = field(default_factory=set)
    """Pinned images `docker image prune -a` removed."""
    unpullable_images: set[str] = field(default_factory=set)
    """Pinned images `docker pull` cannot fetch, as if the registry were unreachable."""
    daemon_down_polls: int = 0
    """How many `docker info` polls find the daemon not up yet; until then docker calls exit 1."""
    private_modes: dict[str, int] = field(default_factory=dict)
    """Modes of the staging and restore directories and files, seen while restic used them."""

    def run(
        self,
        argv: Sequence[str],
        *,
        label: str,
        stdin: Path | None = None,
        stdout: Path | None = None,
        timeout: timedelta | None = None,
        discard_stderr: bool = False,
    ) -> bytes:
        argv = list(argv)
        self.calls.append((label, argv))
        assert len(self.calls) < 10_000, "the runner never gives up"
        if label in self.hanging:
            assert timeout is not None, f"{label} would hang forever"
            self.clock.sleep(timeout.total_seconds())
            raise host.HostCommandError(label, "antwortete nicht")
        if self._host_lock_is_free():
            self.lock_free_during.add(label)
        if (failure := self.failing.get(label)) is not None:
            if isinstance(failure, int):
                return exit_with(label, failure)
            raise failure()
        answers = {
            "docker info": self._daemon,
            "docker image inspect": self._image,
            "docker pull": self._pull,
            "docker ps": self._containers,
            "docker inspect": self._state,
            "docker volume ls": self._volumes,
            "pg_dump": self._pg_dump,
            "Zählen der Audiodateien": self._scan,
            "restic backup": self._backup,
            "restic snapshots": self._snapshots,
            "restic restore": self._restore,
            "Start der Wegwerf-Datenbank": self._start_server,
            "pg_isready": self._pg_isready,
            "pg_restore --list": self._pg_restore,
            "pg_restore": self._pg_restore,
            "psql": self._psql,
            "Entfernen der Wegwerf-Datenbank": self._silent,
            "restic init": self._silent,
            "restic forget": self._silent,
            "restic check": self._silent,
            "restic unlock": self._silent,
        }
        if label not in answers:
            pytest.fail(f"the scripted machine does not know {label!r}")
        answer = answers[label](argv, stdin=stdin, discard_stderr=discard_stderr)
        if stdout is None:
            return answer
        return REAL_HOST_RUN(["printf", "%s", answer.decode()], label=label, stdout=stdout)

    def labels(self) -> list[str]:
        return [label for label, _ in self.calls]

    def argv_of(self, label: str) -> list[str]:
        return next(argv for called, argv in self.calls if called == label)

    def _host_lock_is_free(self) -> bool:
        lock = self.status_file.with_name("backup.lock")
        if not lock.exists():
            return True
        with lock.open("rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    def _daemon(self, argv, **_) -> bytes:
        if self.daemon_down_polls:
            self.daemon_down_polls -= 1
            return exit_with("docker info", 1)
        return b"27.3.1\n"

    def _image(self, argv, *, discard_stderr, **_) -> bytes:
        if self.daemon_down_polls:
            return exit_with("docker image inspect", 1)
        if argv[-1] not in self.missing_images:
            return b"sha256:0"
        if not discard_stderr:
            print(f"Error: No such image: {argv[-1]}", file=sys.stderr)
        return exit_with("docker image inspect", 1)

    def _pull(self, argv, *, discard_stderr, **_) -> bytes:
        if argv[-1] in self.unpullable_images:
            if not discard_stderr:
                print("Error response from daemon: Get registry: dial tcp", file=sys.stderr)
            return exit_with("docker pull", 1)
        self.missing_images.discard(argv[-1])
        return b"sha256:0\n"

    def _containers(self, argv, **_) -> bytes:
        if any("edge-backup-restore-test" in argument for argument in argv):
            ours = f"label={RESTORE_OWNER_LABEL}={RESTORE_OWNER_ROLE}" in argv
            foreign = [] if ours or not self.foreign_server_name else ["f0re1gn"]
            return "\n".join([*self._owned_servers(), *foreign]).encode()
        if self.absent_polls:
            self.absent_polls -= 1
            return b""
        return "\n".join(self.db_containers).encode()

    def _state(self, argv, **_) -> bytes:
        return b"running healthy\n"

    def _volumes(self, argv, **_) -> bytes:
        return "\n".join(self.volumes).encode()

    def _pg_dump(self, argv, **_) -> bytes:
        return DUMP

    def _scan(self, argv, **_) -> bytes:
        """`find /audio -type f -print0` over the live volume or a restored directory."""
        mount = mount_of(argv, "/audio")
        if mount["type"] == "volume":
            names = list(self.audio)
        else:
            root = Path(mount["source"])
            names = [
                str(path.relative_to(root))
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            ]
        return b"".join(name.encode() + b"\0" for name in names)

    def _backup(self, argv, **_) -> bytes:
        self.status_seen_during_backup = json.loads(self.status_file.read_text())
        staging = Path(mount_source(argv, "/backup/songmaker/db"))
        self.private_modes["staging"] = mode_of(staging)
        self.private_modes |= {path.name: mode_of(path) for path in staging.iterdir()}
        snapshot_id = f"{len(self.snapshots) + 1:064x}"
        self.snapshots.append(
            {
                "id": snapshot_id,
                "db": {
                    path.name: path.read_bytes()
                    for path in staging.iterdir()
                    if path.name not in self.snapshot_without
                },
                "audio": self.audio | self.written_during_scan,
            }
        )
        status = {"message_type": "status", "percent_done": 1}
        summary = {"message_type": "summary", "snapshot_id": snapshot_id}
        return f"{json.dumps(status)}\n{json.dumps(summary)}\n".encode()

    def _snapshots(self, argv, **_) -> bytes:
        if "--json" in argv:
            tag = argv[argv.index("--tag") + 1]
            tagged = [s for s in self.snapshots if s.get("app", "songmaker") == tag]
            listed = tagged[-1:] if "--latest" in argv else tagged
            return json.dumps([{"id": s["id"]} for s in listed]).encode()
        return f"{len(self.snapshots)} snapshots\n".encode()

    def _restore(self, argv, **_) -> bytes:
        snapshot_id = argv[argv.index("restore") + 1].split(":")[0]
        snapshot = next(s for s in self.snapshots if s["id"] == snapshot_id)
        target = Path(mount_source(argv, "/restore"))
        self.private_modes["restored"] = mode_of(target)
        for folder in ("db", "audio"):
            for name, content in snapshot[folder].items():
                (target / folder / name).parent.mkdir(parents=True, exist_ok=True)
                (target / folder / name).write_bytes(content)
        return b""

    def _silent(self, argv, **_) -> bytes:
        return b""

    def _start_server(self, argv, **_) -> bytes:
        if self.foreign_server_name or self._owned_servers():
            return exit_with("Start der Wegwerf-Datenbank", 125)
        self.server_accepting = False
        self.created_servers.append(RESTORE_SERVER_ID)
        return self.server_start_output

    def _owned_servers(self) -> list[str]:
        removed = set(removed_servers(self))
        return [s for s in [*self.leftover_servers, *self.created_servers] if s not in removed]

    def _pg_isready(self, argv, **_) -> bytes:
        assert argv[argv.index("pg_isready") + 1 :] == ["--host", "127.0.0.1", "--quiet"]
        if self.starting_server_polls:
            self.starting_server_polls -= 1
            return exit_with("pg_isready", 2)
        self.server_accepting = True
        return b""

    def _pg_restore(self, argv, *, stdin, **_) -> bytes:
        assert self.server_accepting, "pg_restore before pg_isready said the server accepts"
        if not stdin.read_bytes().startswith(b"PGDMP"):
            return exit_with("pg_restore", 1)
        return b""

    def _psql(self, argv, **_) -> bytes:
        assert self.server_accepting, "psql before pg_isready said the server accepts"
        sql = argv[argv.index("--command") + 1]
        answers = {
            'SELECT count(*) FROM "songs"': b"42\n",
            'SELECT "mp3_path" FROM "generations"'
            ' WHERE "mp3_path" IS NOT NULL AND "mp3_path" <> \'\'': b"".join(
                reference.encode() + b"\0" for reference in self.references
            ),
        }
        if sql not in answers:
            pytest.fail(f"the throwaway server was asked an unexpected query: {sql}")
        return answers[sql]


def exit_with(label: str, exit_code: int) -> bytes:
    """A real process exits with the code, so host.run decides what it means."""
    return REAL_HOST_RUN(["sh", "-c", f"exit {exit_code}"], label=label)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def removed_servers(machine: Machine) -> list[str]:
    return [argv[-1] for label, argv in machine.calls if label == "Entfernen der Wegwerf-Datenbank"]


def no_restore_server_left(machine: Machine) -> bool:
    started = machine.labels().count("Start der Wegwerf-Datenbank")
    return started == removed_servers(machine).count(RESTORE_SERVER_ID)


def mount_of(argv: list[str], target: str) -> dict[str, str]:
    mounts = [argv[i + 1] for i, argument in enumerate(argv) if argument == "--mount"]
    return next(
        dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
        for mount in mounts
        if f"target={target}," in mount + ","
    )


def mount_source(argv: list[str], target: str) -> str:
    return mount_of(argv, target)["source"]


@dataclass
class Clock:
    moment: datetime = datetime(2026, 10, 18, 3, 0, tzinfo=UTC)
    elapsed: float = 0.0
    wall_drift_per_sleep: timedelta = timedelta(0)
    """How far someone sets the wall clock while the runner sleeps (NTP, an operator)."""

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds
        self.moment += timedelta(seconds=seconds) + self.wall_drift_per_sleep


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    out: str
    err: str
    status: dict | None
