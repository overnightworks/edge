"""The in-container census of the agent service's writable trees (#339 lane C3, agent-runtime#440).

Standard library only. The host sends this file's text followed by one line, `emit(<json text>)`,
on the stdin of `python3 -`; nothing is copied into the container, and the HMAC key and the
settings never reach an argv or the environment. `emit` prints exactly one JSON line.

Nothing leaves memory before `Names`: every path component and every process name is shown as an
allowlisted structural name or as an opaque id. The output holds counts, type letters, opaque ids
and closed skip reasons, never an exception message, a raw name or a `repr`. The walk anchors at a
mount point and goes down by descriptor: each directory is opened from its parent's descriptor
with O_NOFOLLOW, checked against the listing by (st_dev, st_ino), and never resolved by path.
Special files and links are listed by type letter and never opened or read; a regular file is
opened only to count a token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from enum import StrEnum

Identity = tuple[int, int]  # (st_dev, st_ino)

ALLOWED_NAMES = frozenset({"system-prompt", "relay-mcp.json", ".claude", "/tmp", "home", "work"})
SOCKS_DIRECTORY = re.compile(r"cc-socks-[0-9]+")
RUN_TREE = re.compile(r"agent-run-([A-Za-z0-9_-]+)")
OPAQUE_HEX_DIGITS = 8

EXCLUDED_FILESYSTEMS = frozenset({"proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue"})
START_UP_CHILDREN = frozenset({b"rg", b"git", b"sh", b"uname"})

MAXIMUM_DEPTH = 64
CHUNK_SIZE = 1 << 16
MAXIMUM_SMALL_FILE = 1 << 22
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
DIRECTORY_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


class Skip(StrEnum):
    REPLACED = "replaced"
    NESTED_MOUNT = "nested mount skipped"
    LOGIN = "login excluded"
    REFUSED = "refused"
    TOO_DEEP = "too deep"
    NOT_REGULAR = "not regular"
    VANISHED = "vanished"


class Names:
    """The sanitizer: the one way a name from the container's tree is shown."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def show(self, component: str | bytes) -> str:
        raw = os.fsencode(component)
        text = os.fsdecode(raw)
        run_tree = RUN_TREE.fullmatch(text)
        if text in ALLOWED_NAMES or SOCKS_DIRECTORY.fullmatch(text):
            return text
        if run_tree:
            return f"agent-run-<{len(run_tree.group(1))}>"
        digest = hmac.new(self._key, raw, hashlib.sha256).hexdigest()
        return f"n-{digest[:OPAQUE_HEX_DIGITS]}"


@dataclass(frozen=True)
class WritableMount:
    point: str
    nested: frozenset[tuple[str, ...]]  # every other mount point below it, relative


def _decode_escapes(field_text: bytes) -> str:
    return os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), field_text))


def _relative(point: str, below: str) -> tuple[str, ...] | None:
    prefix = point.rstrip("/") + "/"
    if point == below or not below.startswith(prefix):
        return None
    return tuple(part for part in below[len(prefix) :].split("/") if part)


def writable_mounts(mountinfo_text: bytes) -> tuple[WritableMount, ...]:
    """Mounts whose own options (field 6) hold `rw`; the superblock options after ` - ` do not
    count. Pseudo filesystems are left out."""
    mounts: list[tuple[str, bool]] = []
    for line in mountinfo_text.split(b"\n"):
        head, separator, tail = line.partition(b" - ")
        fields = head.split(b" ")
        if not separator or len(fields) < 6 or not tail:
            continue
        filesystem = tail.split(b" ")[0].decode("ascii", "replace")
        options = fields[5].split(b",")
        writable = b"rw" in options and filesystem not in EXCLUDED_FILESYSTEMS
        mounts.append((_decode_escapes(fields[4]), writable))
    return tuple(
        WritableMount(
            point,
            frozenset(rel for other, _ in mounts if (rel := _relative(point, other)) is not None),
        )
        for point, writable in mounts
        if writable
    )


class TokenRead(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    NOT_REGULAR = "not regular"
    VANISHED = "vanished"
    REFUSED = "refused"


def count_token(
    directory_fd: int, name: str, token: bytes, chunk_size: int = CHUNK_SIZE
) -> TokenRead:
    """One content read: does this file hold the token's bytes? The overlap between chunks is
    the token's length minus one, compared in memory."""
    if not token:
        raise ValueError
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return TokenRead.VANISHED
    except OSError:
        return TokenRead.REFUSED
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return TokenRead.NOT_REGULAR
        overlap = len(token) - 1
        carried = b""
        while chunk := os.read(descriptor, chunk_size):
            window = carried + chunk
            if token in window:
                return TokenRead.FOUND
            carried = window[max(0, len(window) - overlap) :]
        return TokenRead.ABSENT
    except OSError:
        return TokenRead.REFUSED
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class Listing:
    path: str
    kind: str
    harness_owned: bool = False


@dataclass
class Census:
    listings: list[Listing] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)
    token_files: int = 0

    def skip(self, reason: Skip) -> None:
        self.skips[reason.value] = self.skips.get(reason.value, 0) + 1


@dataclass(frozen=True)
class WalkRules:
    excluded: frozenset[Identity] = frozenset()
    nested: frozenset[tuple[str, ...]] = frozenset()
    harness_owned: frozenset[Identity] = frozenset()
    token: bytes | None = None


def kind_letter(mode: int) -> str:
    for test, letter in (
        (stat.S_ISDIR, "d"),
        (stat.S_ISREG, "f"),
        (stat.S_ISLNK, "l"),
        (stat.S_ISFIFO, "p"),
        (stat.S_ISSOCK, "s"),
        (stat.S_ISBLK, "b"),
        (stat.S_ISCHR, "c"),
    ):
        if test(mode):
            return letter
    return "?"


def _open_child(name: str, parent_fd: int) -> int:
    return os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)


def _identity(info: os.stat_result) -> Identity:
    return (info.st_dev, info.st_ino)


def _listing_names(directory_fd: int) -> Iterator[str] | None:
    try:
        with os.scandir(directory_fd) as entries:
            return iter(sorted(entry.name for entry in entries))
    except OSError:
        return None


def _walk(
    directory_fd: int,
    trail: tuple[str, ...],
    names: Names,
    rules: WalkRules,
    census: Census,
) -> None:
    listed = _listing_names(directory_fd)
    if listed is None:
        census.skip(Skip.REFUSED)
        return
    for name in listed:
        shown = "/".join(names.show(part) for part in (*trail, name))
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            census.skip(Skip.VANISHED)
            continue
        kind = kind_letter(info.st_mode)
        census.listings.append(Listing(shown, kind, _identity(info) in rules.harness_owned))
        if kind == "f" and rules.token is not None:
            _count_file(directory_fd, name, rules.token, census)
        if kind != "d":
            continue
        skip = _directory_skip(trail, name, info, rules)
        if skip is not None:
            census.skip(skip)
            continue
        _descend(directory_fd, trail, name, info, names, rules, census)


def _directory_skip(
    trail: tuple[str, ...], name: str, info: os.stat_result, rules: WalkRules
) -> Skip | None:
    if _identity(info) in rules.excluded:
        return Skip.LOGIN
    if (*trail, name) in rules.nested:
        return Skip.NESTED_MOUNT
    if len(trail) + 1 >= MAXIMUM_DEPTH:
        return Skip.TOO_DEEP
    return None


def _count_file(directory_fd: int, name: str, token: bytes, census: Census) -> None:
    outcome = count_token(directory_fd, name, token)
    if outcome is TokenRead.FOUND:
        census.token_files += 1
    elif outcome is not TokenRead.ABSENT:
        census.skip(Skip(outcome.value))


def _descend(
    parent_fd: int,
    trail: tuple[str, ...],
    name: str,
    listed: os.stat_result,
    names: Names,
    rules: WalkRules,
    census: Census,
) -> None:
    try:
        child_fd = _open_child(name, parent_fd)
    except OSError:
        census.skip(Skip.REPLACED)
        return
    try:
        if _identity(os.fstat(child_fd)) != _identity(listed):
            census.skip(Skip.REPLACED)
            return
        _walk(child_fd, (*trail, name), names, rules, census)
    finally:
        os.close(child_fd)


def census_mount(mount: WritableMount, names: Names, rules: WalkRules) -> Census:
    """Walk one mount from its mount point. The mount point itself is the only path string
    opened; every level below is opened from its parent's descriptor."""
    census = Census()
    try:
        root_fd = os.open(mount.point, DIRECTORY_FLAGS)
    except OSError:
        census.skip(Skip.REFUSED)
        return census
    try:
        _walk(root_fd, (), names, replace(rules, nested=mount.nested), census)
    finally:
        os.close(root_fd)
    return census


def _read_small(path: str, directory_fd: int | None = None) -> bytes:
    descriptor = os.open(path, READ_FLAGS, dir_fd=directory_fd)
    try:
        return os.read(descriptor, MAXIMUM_SMALL_FILE)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    pgid: int
    sid: int
    name: str
    start_up_child: bool


def _process_record(data: bytes, pid: int, names: Names) -> ProcessRecord | None:
    opening, closing = data.find(b"("), data.rfind(b")")
    fields = data[closing + 2 :].split()
    if opening < 0 or closing < opening or len(fields) < 4:
        return None
    command = data[opening + 1 : closing]
    ppid, pgid, sid = (int(value) for value in fields[1:4])
    return ProcessRecord(pid, ppid, pgid, sid, names.show(command), command in START_UP_CHILDREN)


def sample_processes(proc_fd: int, names: Names) -> list[ProcessRecord]:
    records = []
    for entry in os.listdir(proc_fd):
        if not (entry.isascii() and entry.isdigit()):
            continue
        try:
            data = _read_small(f"{entry}/stat", proc_fd)
        except OSError:
            continue
        record = _process_record(data, int(entry), names)
        if record is not None:
            records.append(record)
    return records


def sample_for(
    proc_path: str,
    names: Names,
    duration: float,
    interval: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ProcessRecord]:
    """Every distinct process seen while the window lasts, sampled every `interval` seconds."""
    proc_fd = os.open(proc_path, DIRECTORY_FLAGS)
    seen: dict[ProcessRecord, None] = {}
    try:
        deadline = clock() + duration
        while True:
            seen.update(dict.fromkeys(sample_processes(proc_fd, names)))
            if clock() >= deadline:
                return list(seen)
            sleep(interval)
    finally:
        os.close(proc_fd)


def start_up_children_only(records: list[ProcessRecord], group_leader: int) -> tuple[bool, int]:
    """The B7 verdict: whether every other process of the leader's group is an expected
    start-up child, and how many there were. No executable name is part of it."""
    children = [r for r in records if r.pgid == group_leader and r.pid != group_leader]
    return all(child.start_up_child for child in children), len(children)


def _identities(paths: list[str]) -> frozenset[Identity]:
    identities = set()
    for path in paths:
        try:
            identities.add(_identity(os.stat(path, follow_symlinks=False)))
        except OSError:
            continue
    return frozenset(identities)


def _census_report(settings: dict, names: Names) -> dict:
    token_file = settings["token_file"]
    token = _read_small(token_file).strip()
    rules = WalkRules(
        excluded=_identities([os.path.dirname(token_file)]),
        harness_owned=_identities(settings["harness_owned"]),
        token=token,
    )
    mounts = []
    for mount in writable_mounts(_read_small(f"/proc/{os.getpid()}/mountinfo")):
        census = census_mount(mount, names, rules)
        mounts.append(
            {
                "mount": names.show(mount.point),
                "listings": [
                    [item.path, item.kind, item.harness_owned] for item in census.listings
                ],
                "skips": census.skips,
                "token_files": census.token_files,
            }
        )
    return {"mounts": mounts}


def _sample_report(settings: dict, names: Names) -> dict:
    records = sample_for(settings["proc"], names, settings["duration"], settings["interval"])
    only_expected, children = start_up_children_only(records, settings["group_leader"])
    return {
        "processes": [
            [r.pid, r.ppid, r.pgid, r.sid, r.name] for r in sorted(records, key=lambda r: r.pid)
        ],
        "only_expected_start_up_children": only_expected,
        "start_up_children": children,
    }


def run(settings_text: str) -> dict:
    settings = json.loads(settings_text)
    names = Names(bytes.fromhex(settings["key"]))
    if settings["mode"] == "census":
        return _census_report(settings, names)
    return _sample_report(settings, names)


def emit(settings_text: str) -> None:
    try:
        report = run(settings_text)
    except Exception:  # the text of an exception may carry a name or the token
        report = {"error": "failed"}
    sys.stdout.write(json.dumps(report) + "\n")
