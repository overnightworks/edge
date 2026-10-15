"""edge-backup driven through its CLI, with a scripted Docker host in place of docker and restic."""

import configparser
import itertools
import json
import re
import tempfile
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from edge_backup_support import (
    AUDIO,
    CANARY_PASSWORD,
    DUMP,
    RESTORE_SERVER_ID,
    SONGMAKER_CONTAINER,
    STATUS_KEYS,
    SYSTEMD,
    Machine,
    mode_of,
    mount_of,
    mount_source,
    no_restore_server_left,
    removed_servers,
)

from edge_backup import cli, host
from edge_backup.apps import AppsError, DumpKind, parse_apps
from edge_backup.docker import RESTORE_OWNER_LABEL, RESTORE_OWNER_ROLE
from edge_backup.runner import RUN_MARK_SUFFIX, STAGING_PREFIX
from edge_backup.settings import (
    POSTGRES_IMAGE,
    READINESS_POLL_SECONDS,
    READINESS_TIMEOUT,
    RESTIC_IMAGE,
)


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
        "--filter", "label=com.docker.compose.oneoff=False",
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
    assert no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


def test_the_restored_audio_is_counted_by_the_manifests_own_rule(edge_backup, machine):
    edge_backup("backup")
    edge_backup("restore-test")

    scans = [argv for label, argv in machine.calls if label == "Zählen der Audiodateien"]
    assert [mount_of(argv, "/audio")["type"] for argv in scans] == ["volume", "bind"]
    assert len({tuple(argv[argv.index("--entrypoint") :]) for argv in scans}) == 1
    assert all(any(m.endswith("target=/audio,readonly") for m in argv) for argv in scans)


def test_restore_test_removes_only_throwaway_servers_it_owns_by_id(edge_backup, machine):
    machine.leftover_servers = ["1eft0ver"]
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 0, outcome.err
    assert removed_servers(machine) == ["1eft0ver", RESTORE_SERVER_ID]
    start = machine.argv_of("Start der Wegwerf-Datenbank")
    assert start[start.index("--label") + 1] == f"{RESTORE_OWNER_LABEL}={RESTORE_OWNER_ROLE}"


def test_restore_test_never_removes_a_foreign_container_holding_the_name(edge_backup, machine):
    machine.foreign_server_name = True
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert "Start der Wegwerf-Datenbank endete mit Exit 125" in outcome.err
    assert removed_servers(machine) == []


@pytest.mark.parametrize(
    ("output", "complaint"),
    [
        (b"", "docker run meldete keine Container-ID"),
        (f"WARNING: platform mismatch\n{RESTORE_SERVER_ID}\n".encode(), "keine Container-ID"),
        (b"not-a-container-id\n", "docker run meldete keine Container-ID"),
        (b"\xff\xfe\n", "docker meldete Ausgabe, die kein UTF-8 ist"),
    ],
)
def test_a_started_throwaway_server_is_removed_even_when_its_id_is_unreadable(
    edge_backup, machine, output, complaint
):
    machine.server_start_output = output
    edge_backup("backup")

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert complaint in outcome.err
    assert removed_servers(machine) == [RESTORE_SERVER_ID]


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
        ("init",),
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

    assert (target.read_bytes(), mode_of(target)) == (b"filed", 0o600)
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


def _lose_the_dump(machine: Machine) -> None:
    machine.snapshot_without = {"db.dump"}


def _lose_the_manifest(machine: Machine) -> None:
    machine.snapshot_without = {"manifest.json"}


@pytest.mark.parametrize(
    ("break_backup", "message"),
    [
        (_lose_a_file_written_during_the_scan, "4 Audiodateien wiederhergestellt"),
        (_reference_a_missing_file, "1 von 3 referenzierten Audiodateien fehlen"),
        (_reference_an_empty_file, "1 von 3 referenzierten Audiodateien fehlen"),
        (_reference_outside_the_audio_volume, "1 von 3 referenzierten Audiodateien fehlen"),
        (_corrupt_the_dump, "pg_restore endete mit Exit 1"),
        (_lose_the_dump, "der Snapshot enthält keinen Datenbank-Dump"),
        (_lose_the_manifest, "der Snapshot enthält kein Manifest"),
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
    assert no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


@pytest.mark.parametrize(
    "failing_label",
    ["Zählen der Audiodateien", "Start der Wegwerf-Datenbank", "Entfernen der Wegwerf-Datenbank"],
)
def test_restore_test_counts_a_container_that_will_not_run_as_a_failed_command(
    edge_backup, machine, failing_label
):
    edge_backup("backup")
    machine.failing[failing_label] = 125

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert f"{failing_label} endete mit Exit 125" in outcome.err
    assert outcome.status["failing_phase"] == "verify"
    assert outcome.status["exit_category"] == "command-failed"


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
    assert no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []


def _back_up_once(edge_backup, machine) -> None:
    edge_backup("backup")


def _arrange_nothing(edge_backup, machine) -> None:
    pass


def _make_the_database_ambiguous(edge_backup, machine) -> None:
    machine.db_containers = ["c0ffee", "c0ffee2"]


def _corrupt_the_status_file(edge_backup, machine) -> None:
    machine.status_file.parent.mkdir(parents=True)
    machine.status_file.write_text("{not json")


def _forbid_reading_the_status_file(edge_backup, machine) -> None:
    machine.status_file.parent.mkdir(parents=True)
    machine.status_file.write_text("{}")
    machine.status_file.chmod(0o000)


def _write_undecodable_status(edge_backup, machine) -> None:
    machine.status_file.parent.mkdir(parents=True)
    machine.status_file.write_bytes(b"\xff\xfe{}")


@pytest.mark.parametrize(
    ("arguments", "arrange", "exit_code"),
    [
        (("backup",), _back_up_once, 0),
        (("restore-test",), _back_up_once, 0),
        (("backup",), _make_the_database_ambiguous, 1),
        (("restore-test",), _arrange_nothing, 1),
        (("--apps", "/nonexistent/apps.toml", "backup"), _arrange_nothing, 1),
        (("--apps", "/nonexistent/apps.toml", "restore-test"), _arrange_nothing, 1),
        (("backup",), _corrupt_the_status_file, 1),
        (("backup",), _forbid_reading_the_status_file, 1),
        (("backup",), _write_undecodable_status, 1),
    ],
    ids=[
        "backup-succeeds",
        "restore-test-succeeds",
        "backup-with-an-ambiguous-database",
        "restore-test-without-a-snapshot",
        "backup-without-apps",
        "restore-test-without-apps",
        "backup-with-a-corrupt-status",
        "backup-with-a-status-it-may-not-read",
        "backup-with-a-status-that-is-not-utf-8",
    ],
)
def test_a_run_sweeps_only_the_plaintext_a_killed_run_left_behind(
    edge_backup, machine, home, arguments, arrange, exit_code
):
    arrange(edge_backup, machine)
    work_dir = home / ".cache/edge-backup"
    killed_run = _leave_killed_run_staging(work_dir)
    unmarked = (work_dir / f"{STAGING_PREFIX}foreign", work_dir / "manual")
    for copy in (killed_run, *unmarked):
        (copy / "db").mkdir(parents=True, exist_ok=True)
        (copy / "db/db.dump").write_bytes(DUMP)

    outcome = edge_backup(*arguments)

    assert outcome.exit_code == exit_code, outcome.err
    assert set(work_dir.iterdir()) == set(unmarked)


def _leave_killed_run_staging(work_dir: Path) -> Path:
    """What a run killed outright leaves: its staging and the mark that proves it made it."""
    staging = work_dir / f"{STAGING_PREFIX}killed"
    staging.mkdir(parents=True)
    staging.with_name(staging.name + RUN_MARK_SUFFIX).touch()
    return staging


@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_run_whose_staging_name_is_taken_leaves_that_directory_alone(
    edge_backup, machine, home, monkeypatch, command
):
    _back_up_once(edge_backup, machine)
    work_dir = home / ".cache/edge-backup"
    taken = work_dir / f"{STAGING_PREFIX}taken"
    (taken / "db").mkdir(parents=True)
    (taken / "db/db.dump").write_bytes(DUMP)
    # The first random name every temporary file or directory draws is the one already taken.
    random_names = tempfile._get_candidate_names
    monkeypatch.setattr(
        tempfile, "_get_candidate_names", lambda: itertools.chain(["taken"], random_names())
    )

    outcome = edge_backup(command)

    assert outcome.exit_code == 0, outcome.err
    assert (taken / "db/db.dump").read_bytes() == DUMP
    assert set(work_dir.iterdir()) == {taken}


def test_an_unexpected_error_marks_the_run_failed_and_surfaces(edge_backup, machine):
    machine.failing["restic backup"] = RuntimeError

    with pytest.raises(RuntimeError):
        edge_backup("backup")

    status = json.loads(machine.status_file.read_text())
    assert (status["state"], status["exit_category"]) == ("failed", "internal")


@pytest.fixture
def undeletable_leftover(home) -> Iterator[Path]:
    """A killed run's staging that `rmtree` cannot remove: a read-only directory holding a file."""
    locked = _leave_killed_run_staging(home / ".cache/edge-backup") / "audio/locked"
    locked.mkdir(parents=True)
    (locked / "one.mp3").write_bytes(AUDIO["u1/one.mp3"])
    locked.chmod(0o500)
    yield locked
    locked.chmod(0o700)


@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_sweep_that_cannot_remove_a_leftover_fails_the_run_with_one_line(
    edge_backup, machine, undeletable_leftover, command
):
    outcome = edge_backup(command)

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert line.startswith("edge-backup: fehlgeschlagen: der Klartext eines abgebrochenen Laufs")
    assert (outcome.status["state"], outcome.status["exit_category"]) == ("failed", "internal")
    assert machine.calls == []


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


def _write_unknown_schema_status(edge_backup, machine) -> None:
    machine.status_file.parent.mkdir(parents=True)
    machine.status_file.write_text(json.dumps({"schema": 2, "state": "ok"}))


def _identity_of(path: Path) -> tuple[int, int, int, int]:
    """Inode, mtime, size and mode: a rewrite or replacement changes at least one."""
    seen = path.stat()
    return seen.st_ino, seen.st_mtime_ns, seen.st_size, seen.st_mode


@pytest.mark.parametrize(
    "break_status",
    [
        _corrupt_the_status_file,
        _forbid_reading_the_status_file,
        _write_undecodable_status,
        _write_unknown_schema_status,
    ],
)
@pytest.mark.parametrize(
    "command",
    [("backup",), ("restore-test",), ("restore", "songmaker", "--target", "restored")],
    ids=lambda command: command[0],
)
def test_an_unreadable_status_file_is_never_overwritten(
    edge_backup, machine, home, monkeypatch, command, break_status
):
    monkeypatch.chdir(home)
    break_status(edge_backup, machine)
    before = _identity_of(machine.status_file)

    outcome = edge_backup(*command)

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert "Statusdatei ist unlesbar" in line
    assert _identity_of(machine.status_file) == before
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
        mount_source(argv, "/run/secrets/restic-password").endswith(".secrets/restic/password")
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


def test_restore_puts_the_last_successful_snapshot_into_a_new_directory(edge_backup, machine, home):
    edge_backup("backup")
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--target", str(target))

    assert outcome.exit_code == 0, outcome.err
    assert (target / "db/db.dump").read_bytes() == DUMP
    assert (target / "audio/u1/one.mp3").read_bytes() == AUDIO["u1/one.mp3"]
    assert edge_backup("restore", "songmaker", "--target", str(target)).exit_code == 1


@pytest.mark.parametrize("target", [f"{STAGING_PREFIX}manual", "manual"])
def test_restore_refuses_a_target_in_the_directory_runs_sweep(edge_backup, machine, home, target):
    edge_backup("backup")
    work_dir = home / ".cache/edge-backup"

    outcome = edge_backup("restore", "songmaker", "--target", str(work_dir / target))

    assert outcome.exit_code == 1
    assert "Arbeitsverzeichnis" in outcome.err
    assert list(work_dir.iterdir()) == []


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


def test_backup_waits_at_most_the_readiness_budget_for_docker_and_the_database_together(
    edge_backup, machine
):
    machine.daemon_down_polls = 36
    _never_start_the_database(machine)

    outcome = edge_backup("backup")

    assert outcome.status["exit_category"] == "not-ready"
    assert machine.clock.elapsed <= READINESS_TIMEOUT.total_seconds() + READINESS_POLL_SECONDS
