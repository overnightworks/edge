"""The in-container census (#339 lane C3, agent-runtime#440's secret-safety contract).

Pure tests on temporary trees: nothing here needs a container. The contract lines they prove:
1 nothing leaves before `Names`, 2 the mount table and the descriptor-relative walk, 3 one
content read, and the process sampler's closed vocabulary.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

PROGRAM = Path(__file__).resolve().parent.parent / "probe" / "agent-service" / "tree_census.py"
KEY = bytes(range(32))
OPAQUE = re.compile(r"n-[0-9a-f]{8}")
TOKEN = b"sk-ant-oat01-Zq_9-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
GRAMMAR = re.compile(
    r"(?:(?:system-prompt|relay-mcp\.json|\.claude|/tmp|home|work|cc-socks-[0-9]+"
    r"|agent-run-<[0-9]+>|n-[0-9a-f]{8}))(?:/(?:system-prompt|relay-mcp\.json|\.claude|home|work"
    r"|cc-socks-[0-9]+|agent-run-<[0-9]+>|n-[0-9a-f]{8}))*"
)


@pytest.fixture(scope="module")
def census() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("agent_probe_tree_census", PROGRAM)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through it
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


@pytest.fixture
def names(census: ModuleType):
    return census.Names(KEY)


HOSTILE = [
    TOKEN.decode(),
    TOKEN.decode()[:20],
    TOKEN.decode()[-20:],
    "aB3_x-Q9" * 5 + "abc",
    "a--b__c---d___e",
    "line\nbreak",
    "\x1b[31mred",
    "tab\there",
    "system-prompt.bak",
    "home2",
    "cc-socks-1000x",
    "cc-socks-",
    "agent-run-a\nb",
    "agent-run-",
    "work/",
    "\udcff\udcfe",
]


@pytest.mark.parametrize(
    "name", ["system-prompt", "relay-mcp.json", ".claude", "/tmp", "home", "work", "cc-socks-1000"]
)
def test_structural_names_are_shown_as_they_are(names, name: str) -> None:
    assert names.show(name) == name


def test_the_run_tree_is_shown_with_its_suffix_length_only(names) -> None:
    assert names.show("agent-run-Ab3_-xYz") == "agent-run-<8>"
    assert names.show("agent-run-Q") == "agent-run-<1>"


@pytest.mark.parametrize("name", HOSTILE)
def test_every_other_name_becomes_an_opaque_id(names, name: str) -> None:
    shown = names.show(name)
    assert OPAQUE.fullmatch(shown)
    assert name not in shown


def test_opaque_ids_are_stable_per_key_and_differ_across_keys(census: ModuleType, names) -> None:
    other = census.Names(bytes(32))
    assert names.show("alpha") == names.show("alpha") == names.show(b"alpha")
    assert names.show("alpha") != other.show("alpha")
    assert names.show("alpha") != names.show("beta")


def hostile_tree(root: Path) -> None:
    (root / "home").mkdir()
    for name in [*HOSTILE, os.fsdecode(b"\xff\xfe")]:
        if "/" not in name:
            (root / "home" / name).write_bytes(b"x")
    (root / "agent-run-Ab3xYz90").mkdir()
    (root / "agent-run-Ab3xYz90" / "work").mkdir()


def test_a_hostile_tree_is_shown_in_the_closed_grammar_only(
    census: ModuleType, names, tmp_path: Path
) -> None:
    hostile_tree(tmp_path)
    mount = census.WritableMount(str(tmp_path), frozenset())
    result = census.census_mount(mount, names, census.WalkRules())
    shown = [item.path for item in result.listings]
    assert len(shown) > 10
    assert all(GRAMMAR.fullmatch(path) for path in shown)
    assert "agent-run-<8>/work" in shown


def test_the_grammar_check_fails_when_names_is_bypassed(
    census: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile_tree(tmp_path)
    monkeypatch.setattr(census.Names, "show", lambda self, component: os.fsdecode(component))
    result = census.census_mount(
        census.WritableMount(str(tmp_path), frozenset()), census.Names(KEY), census.WalkRules()
    )
    assert not all(GRAMMAR.fullmatch(item.path) for item in result.listings)


MOUNTINFO = b"\n".join(
    [
        b"20 1 0:18 / / rw,relatime - overlay overlay rw,lowerdir=/a",
        b"21 20 0:19 / /proc rw,nosuid - proc proc rw",
        b"22 20 0:20 / /tmp rw,nosuid - tmpfs tmpfs rw",
        b"23 22 0:21 /x /tmp/ro\\040dir ro,relatime - tmpfs tmpfs rw",
        b"24 22 0:22 / /tmp/inner rw - tmpfs tmpfs ro",
        b"25 20 0:23 / /sys/fs/cgroup rw - cgroup2 cgroup rw",
        b"26 20 0:24 / /run/escaped\\040space rw - ext4 /dev/sda rw",
        b"27 20 0:25 / /dev/shm rw - tmpfs shm ro",
        b"garbage line",
        b"1 2 3 4 5 - ",
        b"",
    ]
)


def test_only_mounts_with_rw_in_their_own_options_are_writable(census: ModuleType) -> None:
    points = {mount.point for mount in census.writable_mounts(MOUNTINFO)}
    assert points == {"/", "/tmp", "/tmp/inner", "/run/escaped space", "/dev/shm"}


def test_a_read_only_bind_over_rw_and_pseudo_filesystems_are_left_out(census: ModuleType) -> None:
    points = {mount.point for mount in census.writable_mounts(MOUNTINFO)}
    assert not points & {"/tmp/ro dir", "/proc", "/sys/fs/cgroup"}


def test_nested_mount_points_are_reported_relative_to_the_walked_mount(census: ModuleType) -> None:
    by_point = {mount.point: mount for mount in census.writable_mounts(MOUNTINFO)}
    assert by_point["/tmp"].nested == {("ro dir",), ("inner",)}
    assert ("tmp", "inner") in by_point["/"].nested
    assert by_point["/dev/shm"].nested == frozenset()


def walk(census: ModuleType, names, root: Path, **rules):
    return census.census_mount(
        census.WritableMount(str(root), rules.pop("nested", frozenset())),
        names,
        census.WalkRules(**rules),
    )


def kinds(result) -> dict[str, str]:
    return {item.path: item.kind for item in result.listings}


def test_a_symlink_is_listed_as_a_link_and_never_followed(
    census: ModuleType, names, tmp_path: Path
) -> None:
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "inner").write_bytes(b"x")
    (tmp_path / "to-dir").symlink_to(tmp_path / "secret")
    (tmp_path / "to-root").symlink_to("/")
    result = walk(census, names, tmp_path)
    assert sorted(kinds(result).values()) == ["d", "f", "l", "l"]


def test_a_fifo_that_would_block_an_open_does_not_stop_the_walk(
    census: ModuleType, names, tmp_path: Path
) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "later").write_bytes(b"x")
    result = walk(census, names, tmp_path, token=TOKEN)
    assert sorted(kinds(result).values()) == ["f", "p"]
    assert result.skips == {}


def test_a_socket_is_listed_by_type_letter(census: ModuleType, names, tmp_path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(tmp_path / "s"))
        assert list(kinds(walk(census, names, tmp_path)).values()) == ["s"]


@pytest.mark.parametrize(("mode", "letter"), [(0o20600, "c"), (0o60600, "b")])
def test_device_nodes_get_their_type_letters(census: ModuleType, mode: int, letter: str) -> None:
    assert census.kind_letter(mode) == letter
    assert census.kind_letter(0) == "?"


def test_the_login_directory_is_excluded_by_inode_not_by_name(
    census: ModuleType, names, tmp_path: Path
) -> None:
    login = tmp_path / "login"
    login.mkdir()
    (login / "token").write_bytes(TOKEN)
    login_identity = census._identity(login.stat())
    login.rename(tmp_path / "renamed")
    (tmp_path / "login").mkdir()
    (tmp_path / "login" / "other").write_bytes(b"x")
    result = walk(census, names, tmp_path, excluded=frozenset({login_identity}), token=TOKEN)
    assert result.skips == {"login excluded": 1}
    assert result.token_files == 0
    assert len(result.listings) == 3  # the excluded directory is listed, not entered


def test_a_nested_mount_point_is_skipped_and_counted(
    census: ModuleType, names, tmp_path: Path
) -> None:
    (tmp_path / "inner").mkdir()
    (tmp_path / "inner" / "hidden").write_bytes(b"x")
    result = walk(census, names, tmp_path, nested=frozenset({("inner",)}))
    assert result.skips == {"nested mount skipped": 1}
    assert len(result.listings) == 1


def test_a_directory_swapped_for_a_symlink_before_it_is_opened_fails_closed(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "victim").mkdir()
    elsewhere = tmp_path.parent / (tmp_path.name + "-elsewhere")
    elsewhere.mkdir()
    (elsewhere / "outside").write_bytes(b"x")
    real = census._open_child

    def swap_then_open(name: str, parent_fd: int) -> int:
        os.rmdir(tmp_path / name)
        os.symlink(elsewhere, tmp_path / name)
        return real(name, parent_fd)

    monkeypatch.setattr(census, "_open_child", swap_then_open)
    result = walk(census, names, tmp_path)
    assert result.skips == {"replaced": 1}
    assert [item.kind for item in result.listings] == ["d"]


def test_a_directory_replaced_by_another_directory_fails_closed(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "victim").mkdir()
    (tmp_path / "victim" / "old").write_bytes(b"x")
    real = census._open_child

    def replace_then_open(name: str, parent_fd: int) -> int:
        os.rename(tmp_path / name, tmp_path / "moved")
        os.mkdir(tmp_path / name)
        return real(name, parent_fd)

    monkeypatch.setattr(census, "_open_child", replace_then_open)
    result = walk(census, names, tmp_path)
    assert result.skips == {"replaced": 1}
    assert "old" not in " ".join(item.path for item in result.listings)


def test_an_entry_that_vanishes_after_the_listing_is_counted(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "gone").write_bytes(b"x")
    real = os.stat

    def stat_after_removal(path, *args, **kwargs):
        os.unlink(tmp_path / "gone")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_after_removal)
    assert walk(census, names, tmp_path).skips == {"vanished": 1}


def test_a_file_that_vanishes_before_its_content_read_is_counted_as_skipped(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "f").write_bytes(TOKEN)
    real = census.count_token

    def count_after_removal(directory_fd: int, name: str, token: bytes):
        os.unlink(tmp_path / name)
        return real(directory_fd, name, token)

    monkeypatch.setattr(census, "count_token", count_after_removal)
    result = walk(census, names, tmp_path, token=TOKEN)
    assert (result.token_files, result.skips) == (0, {"vanished": 1})


def test_a_directory_that_cannot_be_listed_is_refused(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sub").mkdir()

    def refuse(descriptor):
        raise PermissionError

    monkeypatch.setattr(os, "scandir", refuse)
    assert walk(census, names, tmp_path).skips == {"refused": 1}


def test_a_mount_point_that_cannot_be_opened_is_refused(
    census: ModuleType, names, tmp_path: Path
) -> None:
    assert walk(census, names, tmp_path / "missing").skips == {"refused": 1}


def test_a_tree_deeper_than_the_limit_stops_and_says_so(
    census: ModuleType, names, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(census, "MAXIMUM_DEPTH", 3)
    path = tmp_path
    for _ in range(5):
        path = path / "d"
        path.mkdir()
    result = walk(census, names, tmp_path)
    assert result.skips == {"too deep": 1}
    assert len(result.listings) == 3


def test_a_harness_planted_file_is_labelled_harness_owned(
    census: ModuleType, names, tmp_path: Path
) -> None:
    planted, other = tmp_path / "canary", tmp_path / "other"
    planted.write_bytes(b"x")
    other.write_bytes(b"x")
    result = walk(
        census, names, tmp_path, harness_owned=frozenset({census._identity(planted.stat())})
    )
    assert sorted(item.harness_owned for item in result.listings) == [False, True]


def write_and_count(census: ModuleType, tmp_path: Path, content: bytes, chunk: int = 7) -> str:
    (tmp_path / "f").write_bytes(content)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return census.count_token(descriptor, "f", TOKEN, chunk).value
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("split", [1, 10, len(TOKEN) - 1])
def test_a_token_split_across_a_chunk_boundary_is_found(
    census: ModuleType, tmp_path: Path, split: int
) -> None:
    padding = b"." * (7 - split % 7)
    assert write_and_count(census, tmp_path, padding + TOKEN + b"tail") == "found"
    assert write_and_count(census, tmp_path, b"." * split + TOKEN) == "found"


def test_a_token_in_a_file_larger_than_the_chunk_is_found_even_twice(
    census: ModuleType, tmp_path: Path
) -> None:
    assert write_and_count(census, tmp_path, b"z" * 100 + TOKEN + b"z" * 100 + TOKEN) == "found"


def test_a_file_without_the_token_is_absent_even_with_a_near_miss(
    census: ModuleType, tmp_path: Path
) -> None:
    assert write_and_count(census, tmp_path, TOKEN[:-1] + b"!" + b"x" * 40) == "absent"


def test_a_one_byte_token_needs_no_overlap(census: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"abc")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert census.count_token(descriptor, "f", b"c", 2).value == "found"
    finally:
        os.close(descriptor)


def test_an_empty_token_is_refused_up_front(census: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        census.count_token(0, "f", b"")


def test_non_regular_missing_and_linked_files_are_never_read(
    census: ModuleType, tmp_path: Path
) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "target").write_bytes(TOKEN)
    (tmp_path / "link").symlink_to(tmp_path / "target")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        outcomes = {
            name: census.count_token(descriptor, name, TOKEN).value
            for name in ("pipe", "link", "missing")
        }
    finally:
        os.close(descriptor)
    assert outcomes == {"pipe": "not regular", "link": "refused", "missing": "vanished"}


def test_a_read_error_after_the_open_is_refused(
    census: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(descriptor, size):
        raise OSError

    monkeypatch.setattr(os, "read", fail)
    assert write_and_count(census, tmp_path, b"abc") == "refused"


def test_the_walk_counts_files_holding_the_token_and_skips_by_reason(
    census: ModuleType, names, tmp_path: Path
) -> None:
    (tmp_path / "a").write_bytes(b"x" + TOKEN)
    (tmp_path / "b").write_bytes(b"nothing")
    (tmp_path / "c").symlink_to(tmp_path / "a")
    result = walk(census, names, tmp_path, token=TOKEN)
    assert (result.token_files, result.skips) == (1, {})


def write_proc(root: Path, entries: dict[str, bytes]) -> None:
    for pid, data in entries.items():
        if pid.isdigit():
            (root / pid).mkdir()
            (root / pid / "stat").write_bytes(data)
        else:
            (root / pid).write_bytes(data)


def stat_line(pid: int, command: bytes, ppid: int, pgid: int, sid: int) -> bytes:
    return b"%d (%s) S %d %d %d 0 0" % (pid, command, ppid, pgid, sid)


def test_the_sampler_reports_ids_and_sanitized_names_only(
    census: ModuleType, names, tmp_path: Path
) -> None:
    write_proc(
        tmp_path,
        {
            "10": stat_line(10, b"claude", 1, 10, 10),
            "11": stat_line(11, b"rg", 10, 10, 10),
            "12": stat_line(12, b"we ird) name", 10, 10, 10),
            "13": b"garbage",
            "14": b"",
            "self": b"x",
            "٣": b"x",
        },
    )
    (tmp_path / "15").mkdir()  # no stat file: the process vanished
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        records = census.sample_processes(descriptor, names)
    finally:
        os.close(descriptor)
    assert sorted(r.pid for r in records) == [10, 11, 12]
    shown = {r.name for r in records}
    assert all(OPAQUE.fullmatch(name) for name in shown)
    assert "claude" not in shown


def test_the_sampler_collects_distinct_processes_over_its_window(
    census: ModuleType, names, tmp_path: Path
) -> None:
    write_proc(tmp_path, {"10": stat_line(10, b"claude", 1, 10, 10)})
    ticks = iter([0.0, 0.0, 1.0, 2.5])
    slept: list[float] = []

    def clock() -> float:
        return next(ticks)

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 1:
            write_proc(tmp_path, {"11": stat_line(11, b"git", 10, 10, 10)})

    records = census.sample_for(str(tmp_path), names, 2.0, 0.05, clock, sleep)
    assert sorted(r.pid for r in records) == [10, 11]
    assert slept == [0.05, 0.05]


def record(census: ModuleType, pid: int, command: bytes, pgid: int = 10):
    return census._process_record(stat_line(pid, command, 1, pgid, pgid), pid, census.Names(KEY))


def test_start_up_children_are_one_boolean_and_a_count(census: ModuleType) -> None:
    expected = [record(census, 10, b"claude"), record(census, 11, b"rg"), record(census, 12, b"sh")]
    assert census.start_up_children_only(expected, 10) == (True, 2)
    surprise = [*expected, record(census, 13, b"curl"), record(census, 14, b"nc", pgid=99)]
    assert census.start_up_children_only(surprise, 10) == (False, 3)
    assert census.start_up_children_only([], 10) == (True, 0)


def settings(tmp_path: Path, **extra) -> str:
    token_file = tmp_path / "login" / "token"
    token_file.parent.mkdir(exist_ok=True)
    token_file.write_bytes(TOKEN + b"\n")
    return json.dumps(
        {
            "mode": "census",
            "key": KEY.hex(),
            "token_file": str(token_file),
            "harness_owned": [],
            **extra,
        }
    )


def test_emit_prints_one_json_line_and_no_free_text_for_a_sampler_run(
    census: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    write_proc(
        proc, {"10": stat_line(10, b"claude", 1, 10, 10), "11": stat_line(11, b"rg", 10, 10, 10)}
    )
    census.emit(
        json.dumps(
            {
                "mode": "sample",
                "key": KEY.hex(),
                "proc": str(proc),
                "duration": 0,
                "interval": 0,
                "group_leader": 10,
            }
        )
    )
    report = json.loads(capsys.readouterr().out)
    assert report["only_expected_start_up_children"] is True
    assert report["start_up_children"] == 1
    assert "claude" not in json.dumps(report)


def test_emit_reports_a_failure_without_its_text(
    census: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    census.emit(TOKEN.decode())
    assert json.loads(capsys.readouterr().out) == {"error": "failed"}


def test_a_census_run_reads_its_token_itself_and_shows_no_raw_name(
    census: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a-hostile-name").write_bytes(b"has " + TOKEN)
    (tree / "planted").write_bytes(b"x")
    real = census._read_small

    def fake_mountinfo(path: str, directory_fd=None):
        if path.endswith("/mountinfo"):
            return f"1 0 0:1 / {tree} rw - tmpfs tmpfs rw".encode()
        return real(path, directory_fd)

    monkeypatch.setattr(census, "_read_small", fake_mountinfo)
    census.emit(settings(tmp_path, harness_owned=[str(tree / "planted"), str(tree / "absent")]))
    output = capsys.readouterr().out
    report = json.loads(output)
    (mount,) = report["mounts"]
    assert OPAQUE.fullmatch(mount["mount"])
    assert mount["token_files"] == 1
    assert sorted(item[2] for item in mount["listings"]) == [False, True]
    assert "a-hostile-name" not in output
    assert TOKEN.decode() not in output


def test_the_walk_uses_none_of_the_path_following_calls() -> None:
    source = PROGRAM.read_text()
    forbidden = [
        r"os\.walk",
        r"rglob",
        r"realpath",
        r"readlink",
        r"(?<![\w.])open\(",
        r"print\(",
        r"repr\(",
        r"\bstr\(",
        r"logging",
    ]
    assert [pattern for pattern in forbidden if re.search(pattern, source)] == []
