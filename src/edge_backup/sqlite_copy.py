"""Runs inside the pinned `sqlite` image beside an app's database volume; stdlib only.

It prints a count, an owner or the copy's bytes and nothing else: no row value, no path.
Each connection is read-only, so the script never writes or checkpoints the database file.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def owner(database: Path) -> str:
    """`uid:gid` of the file, whose user must run the copy to take part as a WAL reader."""
    status = database.stat()
    return f"{status.st_uid}:{status.st_gid}"


def copy(database: Path) -> bytes:
    """One read snapshot taken in one step; a missing file fails instead of being created."""
    with (
        closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(":memory:")) as snapshot,
    ):
        source.backup(snapshot, pages=-1)
        return snapshot.serialize()


def check(database: Path, table: str) -> int:
    """The row count of a database whose integrity check answers exactly `ok`."""
    with closing(
        sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("integrity_check")
        quoted = '"' + table.replace('"', '""') + '"'
        return connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case ["owner", database]:
                sys.stdout.write(owner(Path(database)) + "\n")
            case ["copy", database]:
                sys.stdout.buffer.write(copy(Path(database)))
            case ["check", database, table]:
                sys.stdout.write(f"{check(Path(database), table)}\n")
            case _:
                return 2
    except (sqlite3.Error, OSError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
