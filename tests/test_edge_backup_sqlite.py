"""The SQLite copy script against real database files, without Docker."""

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from edge_backup import sqlite_copy

WRITER = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA wal_autocheckpoint=8")
for _ in range(int(sys.argv[2])):
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany("INSERT INTO users (name) VALUES (hex(randomblob(1000)))", [()] * 20)
    connection.execute("COMMIT")
"""


def _database(path: Path, rows: int = 3) -> Path:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    connection.executemany("INSERT INTO users (name) VALUES (?)", [("secret-name",)] * rows)
    connection.close()
    return path


def _copied(source: Path, target: Path) -> Path:
    target.write_bytes(sqlite_copy.copy(source))
    return target


def test_a_copy_holds_the_rows_of_the_source_and_passes_the_check(tmp_path):
    copied = _copied(_database(tmp_path / "source.sqlite3", rows=5), tmp_path / "copy.sqlite3")

    assert sqlite_copy.check(copied, "users") == 5


def test_a_copy_taken_while_a_writer_commits_and_checkpoints_is_a_committed_prefix(tmp_path):
    source = _database(tmp_path / "source.sqlite3", rows=0)
    writer = subprocess.Popen([sys.executable, "-I", "-c", WRITER, str(source), "300"])
    counts = []
    while writer.poll() is None or not counts:
        counts.append(sqlite_copy.check(_copied(source, tmp_path / "copy.sqlite3"), "users"))
    assert writer.wait() == 0

    assert all(count % 20 == 0 for count in counts)


def test_a_missing_database_fails_and_creates_nothing(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        sqlite_copy.copy(tmp_path / "missing.sqlite3")

    assert list(tmp_path.iterdir()) == []


def test_a_copy_of_a_cleanly_closed_database_leaves_at_most_empty_sidecars_that_a_writer_removes(
    tmp_path,
):
    source = _database(tmp_path / "source.sqlite3")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.sqlite3"]

    assert sqlite_copy.check(_copied(source, tmp_path.parent / f"{tmp_path.name}.copy"), "users") == 3

    base = source.stat()
    left = {path.name: path.stat() for path in tmp_path.iterdir() if path != source}
    assert left.keys() <= {"source.sqlite3-wal", "source.sqlite3-shm"}
    assert all(
        sidecar.st_uid == base.st_uid
        and stat.S_IMODE(sidecar.st_mode) == stat.S_IMODE(base.st_mode)
        for sidecar in left.values()
    )
    assert left.get("source.sqlite3-wal") is None or left["source.sqlite3-wal"].st_size == 0
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("INSERT INTO users (name) VALUES ('later')")
    writer.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.sqlite3"]


def test_the_owner_is_the_uid_and_gid_of_the_file(tmp_path):
    source = _database(tmp_path / "source.sqlite3")

    assert sqlite_copy.owner(source) == f"{os.getuid()}:{os.getgid()}"


def test_a_corrupt_copy_fails_the_check(tmp_path):
    copied = _copied(_database(tmp_path / "source.sqlite3", rows=500), tmp_path / "copy.sqlite3")
    data = bytearray(copied.read_bytes())
    data[len(data) // 2 : len(data) // 2 + 4096] = os.urandom(4096)
    copied.write_bytes(data)

    with pytest.raises(sqlite3.DatabaseError):
        sqlite_copy.check(copied, "users")


def test_the_check_of_a_database_with_no_rows_answers_zero(tmp_path):
    copied = _copied(_database(tmp_path / "source.sqlite3", rows=0), tmp_path / "copy.sqlite3")

    assert sqlite_copy.check(copied, "users") == 0


def test_the_check_never_modifies_the_copy(tmp_path):
    copied = _copied(_database(tmp_path / "source.sqlite3"), tmp_path / "copy.sqlite3")
    before = copied.read_bytes()

    sqlite_copy.check(copied, "users")

    assert copied.read_bytes() == before
    assert not [path for path in tmp_path.iterdir() if path.name.startswith("copy.sqlite3-")]


def _run_script(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", sqlite_copy.__file__, *arguments], capture_output=True, check=False
    )


def test_the_script_prints_a_count_and_nothing_else(tmp_path):
    copied = _copied(_database(tmp_path / "source.sqlite3", rows=4), tmp_path / "copy.sqlite3")

    result = _run_script("check", str(copied), "users")

    assert (result.returncode, result.stdout) == (0, b"4\n")
    assert b"secret-name" not in result.stdout + result.stderr


def test_the_script_copies_the_bytes_to_stdout(tmp_path):
    source = _database(tmp_path / "source.sqlite3", rows=2)

    result = _run_script("copy", str(source))

    assert result.returncode == 0
    copied = tmp_path / "copy.sqlite3"
    copied.write_bytes(result.stdout)
    assert sqlite_copy.check(copied, "users") == 2


def test_the_script_fails_quietly_on_a_missing_file(tmp_path):
    result = _run_script("copy", str(tmp_path / "missing.sqlite3"))

    assert (result.returncode, result.stdout) == (1, b"")
    assert list(tmp_path.iterdir()) == []
