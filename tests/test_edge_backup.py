"""edge-backup driven through its CLI, with a scripted Docker host in place of docker and restic."""

import configparser
import fcntl
import json
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from edge_backup import cli, host
from edge_backup.apps import AppsError, DumpKind, parse_apps
from edge_backup.docker import RESTORE_OWNER_LABEL, RESTORE_OWNER_ROLE
from edge_backup.settings import POSTGRES_IMAGE, RESTIC_IMAGE

CANARY_PASSWORD = "canary-7f3e-never-print-me"
DUMP = b"PGDMP custom archive"
AUDIO = {"u1/one.mp3": b"ID3 one", "u1/two.mp3": b"ID3 two", "u2/cover.png": b"PNG"}
SONGMAKER_CONTAINER = "c0ffee"
RESTORE_SERVER_ID = "deadbeef"
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
    leftover_servers: list[str] = field(default_factory=list)
    """Throwaway servers a killed restore test left behind; they carry the owner label."""
    foreign_server_name: bool = False
    """A container that is not edge-backup's already holds the throwaway server's name."""
    starting_server_polls: int = 1
    """How many pg_isready polls find the throwaway server still starting."""
    server_accepting: bool = False
    """Labels of probes that never answer; only a timeout ends them."""
    written_during_scan: dict[str, bytes] = field(default_factory=dict)
    """Audio that lands in the volume after the manifest's count, before restic's scan."""
    failing: dict[str, int | type[BaseException]] = field(default_factory=dict)
    snapshots: list[dict] = field(default_factory=list)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    status_seen_during_backup: dict | None = None
    status_file: Path | None = None
    lock_free_during: set[str] = field(default_factory=set)
    """Labels of commands that ran while the host lock was free."""
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
                return _exit(label, failure)
            raise failure()
        answers = {
            "docker info": self._daemon,
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
        answer = answers[label](argv, stdin=stdin)
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
        return b"27.3.1\n"

    def _containers(self, argv, **_) -> bytes:
        if any("edge-backup-restore-test" in argument for argument in argv):
            ours = f"label={RESTORE_OWNER_LABEL}={RESTORE_OWNER_ROLE}" in argv
            foreign = [] if ours or not self.foreign_server_name else ["f0re1gn"]
            return "\n".join([*self.leftover_servers, *foreign]).encode()
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
        mount = _mount(argv, "/audio")
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
        staging = Path(_mount_source(argv, "/backup/songmaker/db"))
        self.private_modes["staging"] = _mode(staging)
        self.private_modes |= {path.name: _mode(path) for path in staging.iterdir()}
        snapshot_id = f"{len(self.snapshots) + 1:064x}"
        self.snapshots.append(
            {
                "id": snapshot_id,
                "db": {path.name: path.read_bytes() for path in staging.iterdir()},
                "audio": self.audio | self.written_during_scan,
            }
        )
        status = {"message_type": "status", "percent_done": 1}
        summary = {"message_type": "summary", "snapshot_id": snapshot_id}
        return f"{json.dumps(status)}\n{json.dumps(summary)}\n".encode()

    def _snapshots(self, argv, **_) -> bytes:
        if "--json" in argv:
            return json.dumps([{"id": s["id"]} for s in self.snapshots[-1:]]).encode()
        return f"{len(self.snapshots)} snapshots\n".encode()

    def _restore(self, argv, **_) -> bytes:
        snapshot_id = argv[argv.index("restore") + 1].split(":")[0]
        snapshot = next(s for s in self.snapshots if s["id"] == snapshot_id)
        target = Path(_mount_source(argv, "/restore"))
        self.private_modes["restored"] = _mode(target)
        for folder in ("db", "audio"):
            for name, content in snapshot[folder].items():
                (target / folder / name).parent.mkdir(parents=True, exist_ok=True)
                (target / folder / name).write_bytes(content)
        return b""

    def _silent(self, argv, **_) -> bytes:
        return b""

    def _start_server(self, argv, **_) -> bytes:
        name_taken = self.foreign_server_name or set(self.leftover_servers) - set(
            _removed_servers(self)
        )
        if name_taken:
            return _exit("Start der Wegwerf-Datenbank", 125)
        self.server_accepting = False
        return f"{RESTORE_SERVER_ID}\n".encode()

    def _pg_isready(self, argv, **_) -> bytes:
        assert argv[argv.index("pg_isready") + 1 :] == ["--host", "127.0.0.1", "--quiet"]
        if self.starting_server_polls:
            self.starting_server_polls -= 1
            return _exit("pg_isready", 2)
        self.server_accepting = True
        return b""

    def _pg_restore(self, argv, *, stdin, **_) -> bytes:
        assert self.server_accepting, "pg_restore before pg_isready said the server accepts"
        if not stdin.read_bytes().startswith(b"PGDMP"):
            return _exit("pg_restore", 1)
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


def _exit(label: str, exit_code: int) -> bytes:
    """A real process exits with the code, so host.run decides what it means."""
    return REAL_HOST_RUN(["sh", "-c", f"exit {exit_code}"], label=label)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _removed_servers(machine: Machine) -> list[str]:
    return [argv[-1] for label, argv in machine.calls if label == "Entfernen der Wegwerf-Datenbank"]


def _no_restore_server_left(machine: Machine) -> bool:
    started = machine.labels().count("Start der Wegwerf-Datenbank")
    return started == _removed_servers(machine).count(RESTORE_SERVER_ID)


def _mount(argv: list[str], target: str) -> dict[str, str]:
    mounts = [argv[i + 1] for i, argument in enumerate(argv) if argument == "--mount"]
    return next(
        dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
        for mount in mounts
        if f"target={target}," in mount + ","
    )


def _mount_source(argv: list[str], target: str) -> str:
    return _mount(argv, target)["source"]


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


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "backups/restic").mkdir(parents=True)
    (tmp_path / "backups/restic/config").write_text("restic repository config")
    password = tmp_path / ".secrets/restic/password"
    password.parent.mkdir(parents=True, mode=0o700)
    password.write_text(CANARY_PASSWORD)
    password.chmod(0o600)
    return tmp_path


@pytest.fixture
def machine(monkeypatch, home) -> Machine:
    machine = Machine(status_file=home / ".local/state/edge/backup-status.json")
    monkeypatch.setattr(host, "run", machine.run)
    monkeypatch.setattr(host, "now", machine.clock.now)
    monkeypatch.setattr(host, "monotonic", machine.clock.monotonic)
    monkeypatch.setattr(host, "sleep", machine.clock.sleep)
    return machine


@pytest.fixture
def edge_backup(machine, home, capsys):
    def run(*arguments: str) -> Outcome:
        exit_code = cli.main(list(arguments))
        captured = capsys.readouterr()
        status_file = home / ".local/state/edge/backup-status.json"
        status = json.loads(status_file.read_text()) if status_file.exists() else None
        return Outcome(exit_code, captured.out, captured.err, status)

    return run


def test_backup_puts_dump_manifest_and_audio_into_one_snapshot(edge_backup, machine, home):
    outcome = edge_backup("backup")

    assert outcome.exit_code == 0, outcome.err
    [snapshot] = machine.snapshots
    assert snapshot["db"] == {"db.dump": DUMP, "manifest.json": snapshot["db"]["manifest.json"]}
    assert json.loads(snapshot["db"]["manifest.json"]) == {
        "schema": 1,
        "app": "songmaker",
        "audio_files": len(AUDIO),
    }
    assert snapshot["audio"] == AUDIO
    assert outcome.status["state"] == "ok"
    assert outcome.status["last_snapshot_id"] == snapshot["id"]
    assert outcome.status["last_successful_backup"] == "2026-10-18T03:00:00+00:00"
    assert list((home / ".cache/edge-backup").iterdir()) == []


def test_backup_forgets_by_host_and_app_tag_then_checks(edge_backup, machine):
    edge_backup("backup")

    assert machine.labels()[-2:] == ["restic forget", "restic check"]
    forget = machine.argv_of("restic forget")
    expected_scope = ["--host", "edge-backup", "--tag", "songmaker", "--group-by", "host,tags"]
    assert forget[forget.index("forget") + 1 :] == [
        *expected_scope,
        *["--keep-weekly", "4", "--keep-monthly", "3", "--prune"],
    ]
    assert "--retry-lock" in forget
    assert "--no-lock" not in forget


def test_dump_runs_fixed_pg_dump_in_the_container_found_by_compose_labels(edge_backup, machine):
    edge_backup("backup")

    assert machine.argv_of("docker ps") == [
        "docker", "ps", "--all", "--quiet", "--no-trunc",
        "--filter", "label=com.docker.compose.project=songmaker",
        "--filter", "label=com.docker.compose.service=postgres",
    ]  # fmt: skip
    assert machine.argv_of("pg_dump") == [
        "docker", "exec", "-i", SONGMAKER_CONTAINER, "pg_dump",
        "--username", "songmaker", "--format=custom", "--dbname", "songmaker",
    ]  # fmt: skip


def test_every_container_runs_a_digest_pinned_image(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    images = {argument for _, argv in machine.calls for argument in argv if "@sha256:" in argument}
    docker_runs = [argv for _, argv in machine.calls if argv[:2] == ["docker", "run"]]
    assert images == {RESTIC_IMAGE, POSTGRES_IMAGE}
    assert all(any(image in argv for image in images) for argv in docker_runs)


def test_status_says_running_while_the_backup_runs(edge_backup, machine):
    edge_backup("backup")

    assert machine.status_seen_during_backup["state"] == "running"
    assert machine.status_seen_during_backup["phase"] == "backup"


def test_restore_test_restores_loads_and_verifies_the_latest_snapshot(edge_backup, machine, home):
    edge_backup("backup")
    backed_up = edge_backup("backup").status["last_successful_backup"]

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 0, outcome.err
    assert outcome.status["state"] == "ok"
    # The throwaway server answered the second pg_isready poll, one second in.
    assert outcome.status["last_successful_restore_test"] == "2026-10-18T03:00:01+00:00"
    assert outcome.status["last_successful_backup"] == backed_up
    restore = machine.argv_of("restic restore")
    assert f"{machine.snapshots[-1]['id']}:/backup/songmaker" in restore
    assert machine.argv_of("pg_restore") == [
        "docker", "exec", "-i", RESTORE_SERVER_ID, "pg_restore", "--exit-on-error",
        "--single-transaction", "--no-owner", "--no-privileges",
        "--username", "postgres", "--dbname", "restore_test",
    ]  # fmt: skip
    assert machine.labels()[-1] == "restic check"
    assert machine.labels().count("pg_isready") == 2
    assert _no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


def test_the_restored_audio_is_counted_by_the_manifests_own_rule(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    scans = [argv for label, argv in machine.calls if label == "Zählen der Audiodateien"]
    assert [_mount(argv, "/audio")["type"] for argv in scans] == ["volume", "bind"]
    assert len({tuple(argv[argv.index("--entrypoint") :]) for argv in scans}) == 1
    assert all(any(m.endswith("target=/audio,readonly") for m in argv) for argv in scans)


def test_restore_test_removes_only_throwaway_servers_it_owns_by_id(edge_backup, machine):
    machine.leftover_servers = ["1eft0ver"]
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 0, outcome.err
    assert _removed_servers(machine) == ["1eft0ver", RESTORE_SERVER_ID]
    start = machine.argv_of("Start der Wegwerf-Datenbank")
    assert start[start.index("--label") + 1] == f"{RESTORE_OWNER_LABEL}={RESTORE_OWNER_ROLE}"


def test_restore_test_never_removes_a_foreign_container_holding_the_name(edge_backup, machine):
    machine.foreign_server_name = True
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert "Start der Wegwerf-Datenbank endete mit Exit 125" in outcome.err
    assert _removed_servers(machine) == []


def test_monthly_check_reads_a_tenth_of_the_data(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    checks = [argv for label, argv in machine.calls if label == "restic check"]
    assert [argv[argv.index("check") + 1 :] for argv in checks] == [
        [],
        ["--read-data-subset=10%"],
    ]


def test_the_throwaway_server_has_no_network_and_never_touches_live_data(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    start = machine.argv_of("Start der Wegwerf-Datenbank")
    assert start[start.index("--network") + 1] == "none"
    assert not any("--mount" in argv or "-v" in argv for argv in [start])
    restore_test_calls = machine.calls[machine.labels().index("restic snapshots") :]
    assert not any(SONGMAKER_CONTAINER in argv for _, argv in restore_test_calls)
    assert not any("songmaker_audiofiles" in " ".join(argv) for _, argv in restore_test_calls)


@pytest.mark.parametrize(
    ("failing_label", "exit_code", "phase", "backup_counts"),
    [
        ("pg_dump", 1, "dump", False),
        ("Zählen der Audiodateien", 1, "manifest", False),
        ("restic backup", 3, "backup", False),
        ("restic backup", 1, "backup", False),
        ("restic forget", 1, "forget", True),
        ("restic check", 1, "check", True),
    ],
)
def test_a_failed_backup_step_fails_loud_and_never_counts_as_success(
    edge_backup, machine, home, failing_label, exit_code, phase, backup_counts
):
    machine.failing[failing_label] = exit_code

    outcome = edge_backup("backup")

    assert outcome.exit_code == 1
    assert outcome.err.count("\n") == 1
    assert f"{failing_label} endete mit Exit {exit_code}" in outcome.err
    assert outcome.status["state"] == "failed"
    assert outcome.status["failing_phase"] == phase
    assert outcome.status["exit_category"] == "command-failed"
    assert (outcome.status["last_successful_backup"] is not None) is backup_counts
    if not backup_counts:
        assert "restic forget" not in machine.labels()
    assert list((home / ".cache/edge-backup").iterdir()) == []


@pytest.mark.parametrize(
    "command",
    [
        ("backup",),
        ("restore-test",),
        ("snapshots",),
        ("unlock",),
        ("restore", "songmaker", "--target", "restored"),
    ],
    ids=lambda command: command[0],
)
def test_every_repository_command_holds_the_host_lock(
    edge_backup, machine, home, monkeypatch, command
):
    monkeypatch.chdir(home)
    edge_backup("backup")
    machine.calls.clear()

    outcome = edge_backup(*command)

    assert outcome.exit_code == 0, outcome.err
    assert machine.calls
    assert machine.lock_free_during == set()


def test_staging_and_restore_directories_and_their_files_are_private(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    assert machine.private_modes == {
        "staging": 0o700,
        "db.dump": 0o600,
        "manifest.json": 0o600,
        "restored": 0o700,
    }


@pytest.mark.parametrize(
    ("argv", "exit_code", "message"),
    [
        (["sh", "-c", "exit 3"], 3, "probe endete mit Exit 3"),
        (["sh", "-c", "exit 1"], 1, "probe endete mit Exit 1"),
        (["edge-backup-no-such-binary"], None, "probe ist nicht installiert"),
    ],
)
def test_host_run_fails_on_every_nonzero_exit_and_a_missing_binary(argv, exit_code, message):
    with pytest.raises(host.HostCommandError, match=message) as raised:
        host.run(argv, label="probe")

    assert raised.value.exit_code == exit_code


def test_host_run_kills_a_command_that_outlasts_its_timeout():
    with pytest.raises(
        host.HostCommandError, match=re.escape("probe antwortete nicht binnen 0.05 s")
    ):
        host.run(["sleep", "5"], label="probe", timeout=timedelta(milliseconds=50))


def test_host_run_returns_stdout_or_writes_it_into_a_new_private_file(tmp_path):
    target = tmp_path / "out"

    assert host.run(["printf", "piped"], label="probe") == b"piped"
    host.run(["printf", "filed"], label="probe", stdout=target)

    assert (target.read_bytes(), _mode(target)) == (b"filed", 0o600)
    with pytest.raises(FileExistsError):
        host.run(["printf", "again"], label="probe", stdout=target)
    assert target.read_bytes() == b"filed"


def test_a_failed_check_keeps_the_last_successful_backup(edge_backup, machine):
    machine.failing["restic check"] = 1

    outcome = edge_backup("backup")

    assert outcome.status["state"] == "failed"
    assert outcome.status["last_snapshot_id"] == machine.snapshots[-1]["id"]


def test_backup_waits_for_the_database_container_to_appear(edge_backup, machine):
    machine.absent_polls = 3

    outcome = edge_backup("backup")

    assert outcome.exit_code == 0, outcome.err
    assert outcome.status["last_attempt"] == "2026-10-18T03:00:00+00:00"
    assert machine.labels().count("docker ps") == 4


def _never_start_the_database(machine: Machine) -> None:
    machine.absent_polls = 10_000


def _set_the_wall_clock_back_while_waiting(machine: Machine) -> None:
    _never_start_the_database(machine)
    machine.clock.wall_drift_per_sleep = timedelta(hours=-1)


def _hang(label: str) -> Callable[[Machine], None]:
    return lambda machine: machine.hanging.add(label)


@pytest.mark.parametrize(
    ("command", "stall"),
    [
        ("backup", _never_start_the_database),
        ("backup", _set_the_wall_clock_back_while_waiting),
        ("backup", _hang("docker ps")),
        ("restore-test", _hang("docker info")),
    ],
    ids=["database-absent", "wall-clock-set-back", "docker-ps-hangs", "docker-info-hangs"],
)
def test_readiness_gives_up_after_ten_minutes(edge_backup, machine, command, stall):
    stall(machine)

    outcome = edge_backup(command)

    assert outcome.exit_code == 1
    assert "nach 10 Minuten nicht bereit" in outcome.err
    assert outcome.status["failing_phase"] == "readiness"
    assert outcome.status["exit_category"] == "not-ready"
    assert not {"pg_dump", "restic restore"} & set(machine.labels())


@pytest.mark.parametrize(
    ("ambiguity", "message"),
    [
        ({"db_containers": ["c0ffee", "c0ffee2"]}, "2 Datenbank-Container"),
        ({"volumes": []}, "0 Volumes audiofiles"),
        ({"volumes": ["a_audiofiles", "b_audiofiles"]}, "2 Volumes audiofiles"),
    ],
)
def test_backup_refuses_when_labels_name_more_or_less_than_one_target(
    edge_backup, machine, ambiguity, message
):
    for name, value in ambiguity.items():
        setattr(machine, name, value)

    outcome = edge_backup("backup")

    assert outcome.exit_code == 1
    assert message in outcome.err
    assert outcome.status["exit_category"] == "configuration"
    assert "pg_dump" not in machine.labels()


def _lose_a_file_written_during_the_scan(machine: Machine) -> None:
    machine.written_during_scan = {"u3/new.mp3": b"ID3 new"}


def _reference_a_missing_file(machine: Machine) -> None:
    machine.references = [*machine.references, "u9/gone.mp3"]


def _reference_an_empty_file(machine: Machine) -> None:
    machine.audio = machine.audio | {"u1/half.mp3": b""}
    machine.references = [*machine.references, "u1/half.mp3"]


def _reference_outside_the_audio_volume(machine: Machine) -> None:
    machine.references = [*machine.references, "../db/db.dump"]


def _corrupt_the_dump(machine: Machine) -> None:
    machine.failing["pg_restore"] = 1


@pytest.mark.parametrize(
    ("break_backup", "message"),
    [
        (_lose_a_file_written_during_the_scan, "4 Audiodateien wiederhergestellt"),
        (_reference_a_missing_file, "1 von 3 referenzierten Audiodateien fehlen"),
        (_reference_an_empty_file, "1 von 3 referenzierten Audiodateien fehlen"),
        (_reference_outside_the_audio_volume, "1 von 3 referenzierten Audiodateien fehlen"),
        (_corrupt_the_dump, "pg_restore endete mit Exit 1"),
    ],
)
def test_restore_test_fails_when_the_copy_is_not_what_was_backed_up(
    edge_backup, machine, home, break_backup, message
):
    break_backup(machine)
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert message in outcome.err
    assert outcome.status["state"] == "failed"
    assert outcome.status["failing_phase"] == "verify"
    assert outcome.status["exit_category"] == "verification-failed"
    assert outcome.status["last_successful_restore_test"] is None
    assert _no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


def test_restore_test_without_a_snapshot_fails(edge_backup):
    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert "kein Snapshot von songmaker" in outcome.err
    assert outcome.status["failing_phase"] == "restore"


def test_an_interrupted_run_is_failed_and_cleans_up(edge_backup, machine, home):
    edge_backup("backup")
    machine.failing["pg_restore"] = KeyboardInterrupt

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert outcome.status["state"] == "failed"
    assert outcome.status["exit_category"] == "interrupted"
    assert _no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


def test_an_unexpected_error_marks_the_run_failed_and_surfaces(edge_backup, machine):
    machine.failing["restic backup"] = RuntimeError

    with pytest.raises(RuntimeError):
        edge_backup("backup")

    status = json.loads(machine.status_file.read_text())
    assert (status["state"], status["exit_category"]) == ("failed", "internal")


def _remove_password_file(home: Path) -> None:
    (home / ".secrets/restic/password").unlink()


def _make_password_file_readable_by_others(home: Path) -> None:
    (home / ".secrets/restic/password").chmod(0o644)


def _remove_repository(home: Path) -> None:
    (home / "backups/restic/config").unlink()


def _corrupt_apps_toml(home: Path) -> None:
    (home / "apps.toml").write_text("[app.songmaker]\ncompose_project = 1\n")


@pytest.mark.parametrize(
    ("break_setup", "message"),
    [
        (_remove_password_file, "die Passwortdatei fehlt"),
        (_make_password_file_readable_by_others, "Modus 600"),
        (_remove_repository, "nicht initialisiert"),
        (_corrupt_apps_toml, "apps.toml [app.songmaker]"),
    ],
)
@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_broken_setup_fails_before_touching_docker(
    edge_backup, machine, home, break_setup, message, command
):
    (home / "apps.toml").write_text(cli.DEFAULT_APPS.read_text())
    break_setup(home)

    outcome = edge_backup("--apps", str(home / "apps.toml"), command)

    assert outcome.exit_code == 1
    assert message in outcome.err
    assert outcome.status["state"] == "failed"
    assert outcome.status["exit_category"] == "configuration"
    assert machine.calls == []


def test_an_unreadable_status_file_is_replaced_by_a_failed_one(edge_backup, machine):
    machine.status_file.parent.mkdir(parents=True)
    machine.status_file.write_text("{not json")

    outcome = edge_backup("backup")

    assert outcome.exit_code == 1
    assert "Statusdatei ist unlesbar" in outcome.err
    assert outcome.status["state"] == "failed"
    assert machine.calls == []


def test_status_holds_the_contract_keys_and_no_paths_or_output(edge_backup, machine, home):
    machine.failing["restic check"] = 1
    edge_backup("backup")
    status_text = machine.status_file.read_text()

    assert set(json.loads(status_text)) == STATUS_KEYS
    assert str(home) not in status_text
    assert "u1/" not in status_text
    assert "docker" not in status_text


def test_the_password_never_appears_in_argv_output_status_or_errors(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")
    machine.failing["restic backup"] = 3
    failed = edge_backup("backup")

    every_argv = " ".join(argument for _, argv in machine.calls for argument in argv)
    assert CANARY_PASSWORD not in every_argv
    assert CANARY_PASSWORD not in failed.out + failed.err
    assert CANARY_PASSWORD not in machine.status_file.read_text()
    restic_runs = [argv for label, argv in machine.calls if label.startswith("restic")]
    assert all(
        _mount_source(argv, "/run/secrets/restic-password").endswith(".secrets/restic/password")
        and argv[argv.index("--password-file") + 1] == "/run/secrets/restic-password"
        for argv in restic_runs
    )
    assert all(
        any(m.endswith("target=/run/secrets/restic-password,readonly") for m in argv)
        for argv in restic_runs
    )


def test_restic_runs_without_network_privileges_or_a_writable_root(edge_backup, machine):
    edge_backup("backup")

    backup = machine.argv_of("restic backup")
    assert backup[backup.index("--network") + 1] == "none"
    assert backup[backup.index("--hostname") + 1] == "edge-backup"
    assert {"--read-only", "--rm"} <= set(backup)
    assert backup[backup.index("--cap-drop") + 1] == "ALL"
    assert any(m.endswith("target=/backup/songmaker/audio,readonly") for m in backup)


def test_restore_puts_the_latest_snapshot_into_a_new_directory(edge_backup, machine, home):
    edge_backup("backup")
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--target", str(target))

    assert outcome.exit_code == 0, outcome.err
    assert (target / "db/db.dump").read_bytes() == DUMP
    assert (target / "audio/u1/one.mp3").read_bytes() == AUDIO["u1/one.mp3"]
    assert edge_backup("restore", "songmaker", "--target", str(target)).exit_code == 1


def test_init_creates_a_private_repository_directory(edge_backup, machine, home):
    (home / "backups/restic/config").unlink()
    (home / "backups/restic").rmdir()

    outcome = edge_backup("init")

    assert outcome.exit_code == 0, outcome.err
    assert (home / "backups/restic").stat().st_mode & 0o777 == 0o700
    assert machine.labels() == ["restic init"]


def test_the_projects_apps_toml_declares_songmaker():
    [songmaker] = parse_apps(cli.DEFAULT_APPS.read_text())

    assert (songmaker.name, songmaker.compose_project, songmaker.db_service) == (
        "songmaker",
        "songmaker",
        "postgres",
    )
    assert (songmaker.dump, songmaker.volume) == (DumpKind.PG_DUMP_CUSTOM, "audiofiles")
    assert songmaker.restore_test.count_table == "songs"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("dump", '"sh -c pg_dump"'), "dump muss einer von"),
        (("volume", '"audio files"'), "volume muss"),
        (("hook", '"rm -rf /"'), "unbekannter Schlüssel ['hook']"),
        (("db_name", '"x; DROP TABLE songs"'), "db_name muss"),
    ],
)
def test_apps_toml_accepts_only_validated_records(change, message):
    key, value = change
    lines = [
        line
        for line in cli.DEFAULT_APPS.read_text().splitlines()
        if not line.startswith(f"{key} =")
    ]
    lines.insert(lines.index("[app.songmaker]") + 1, f"{key} = {value}")
    text = "\n".join(lines)

    with pytest.raises(AppsError, match=re.escape(message)):
        parse_apps(text)


def test_apps_toml_rejects_an_unsafe_sql_identifier():
    text = cli.DEFAULT_APPS.read_text().replace('"songs"', '"songs\\"; --"')

    with pytest.raises(AppsError, match="count_table muss"):
        parse_apps(text)


def _unit(name: str) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read_string((SYSTEMD / name).read_text())
    return unit


@pytest.mark.parametrize(
    ("unit", "calendar", "command"),
    [
        ("edge-backup", "Sun *-*-* 03:00:00", "backup"),
        ("edge-backup-restore-test", "Sun *-*-01..07 05:00:00", "restore-test"),
    ],
)
def test_each_timer_starts_its_service_on_schedule_and_catches_up_missed_runs(
    unit, calendar, command
):
    timer, service = _unit(f"{unit}.timer"), _unit(f"{unit}.service")

    assert dict(timer["Timer"]) == {"OnCalendar": calendar, "Persistent": "true"}
    assert timer["Install"]["WantedBy"] == "timers.target"
    assert service["Service"]["Type"] == "oneshot"
    program, *arguments = service["Service"]["ExecStart"].split()
    assert (program, arguments) == ("%h/git/edge/.venv/bin/edge-backup", [command])
    with pytest.raises(SystemExit) as help_shown:
        cli.main([command, "--help"])
    assert help_shown.value.code == 0
