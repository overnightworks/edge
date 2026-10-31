"""The retention census (#814, S1c of #299): the in-container retention mode of `tree_census.py`
and the host side in `retention_census.py`. Pure tests on temporary trees and a fake command
runner: nothing here needs a container."""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from support.probe_support import probe_imports

KEY = bytes(range(32))
REQUEST, ANSWER = "req-7f3a9c41d2e85b06", "ans-0b6d1e94a7c35f28"


@pytest.fixture(scope="module")
def tree() -> Iterator[ModuleType]:
    with probe_imports():
        import tree_census

        yield tree_census


@pytest.fixture
def names(tree: ModuleType):
    return tree.Names(KEY, 1000)


def table_with(mount: Path, *extra: str) -> bytes:
    """A mount table whose one tmpfs sits at `mount`, with the device the directory really has."""
    device = mount.stat().st_dev
    ours = f"30 20 {os.major(device)}:{os.minor(device)} / {mount} rw - tmpfs tmpfs rw"
    return "\n".join(["20 1 0:18 / / ro - overlay overlay ro", ours, *extra]).encode()


def settings(*known: str) -> dict:
    return {"known": list(known), "tokens": {"request": REQUEST, "answer": ANSWER}}


def test_the_retention_report_counts_each_token_by_label_in_the_regular_files_of_a_mount(
    tree: ModuleType, names, tmp_path: Path
) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "log").write_text(f"before {REQUEST} after")
    (tmp_path / "other").write_text("nothing here")

    report = tree.retention_report(settings(str(tmp_path)), names, table_with(tmp_path))

    (mount,) = report["mounts"]
    assert (mount["mount"], mount["found"], mount["skips"]) == (
        str(tmp_path),
        {"request": 1, "answer": 0},
        {},
    )


def test_the_retention_report_applies_no_login_exclusion(
    tree: ModuleType, names, tmp_path: Path
) -> None:
    (tmp_path / "login").mkdir()
    (tmp_path / "login" / "token").write_text(ANSWER)

    report = tree.retention_report(settings(str(tmp_path)), names, table_with(tmp_path))

    assert (report["mounts"][0]["found"]["answer"], report["mounts"][0]["skips"]) == (1, {})


def test_the_retention_report_lists_the_whole_mount_table_before_any_filter(
    tree: ModuleType, names, tmp_path: Path
) -> None:
    extra = (
        "21 20 0:19 / /proc rw - proc proc rw",
        "22 20 0:50 / /srv/customer-name rw - ext4 /dev/sda rw",
    )

    report = tree.retention_report(settings(str(tmp_path)), names, table_with(tmp_path, *extra))

    table = {point: (filesystem, writable) for point, filesystem, writable in report["table"]}
    assert table["/proc"] == ("proc", True)
    assert table[str(tmp_path)] == ("tmpfs", True)
    opaque = [point for point in table if point.startswith("n-")]
    assert sorted(table[point] for point in opaque) == [("ext4", True), ("overlay", False)]
    assert "/srv/customer-name" not in json.dumps(report)


def test_a_mount_entry_keeps_pseudo_and_read_only_mounts_that_the_writable_list_drops(
    tree: ModuleType,
) -> None:
    text = b"21 20 0:19 / /proc rw - proc proc rw\n22 20 0:20 / /ro ro - tmpfs tmpfs rw"

    entries = [(e.point, e.filesystem, e.writable) for e in tree.mount_entries(text)]

    assert entries == [("/proc", "proc", True), ("/ro", "tmpfs", False)]
    assert tree.writable_mounts(text) == ()


def test_the_retention_mode_answers_through_run_with_the_table_and_the_mounts(
    tree: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "file").write_text(REQUEST)
    mountinfo = table_with(tmp_path)
    monkeypatch.setattr(tree, "_read_small", lambda path, directory_fd=None: mountinfo)
    asked = {"key": KEY.hex(), "mode": "retention", **settings(str(tmp_path))}

    report = tree.run(json.dumps(asked))

    assert report["mounts"][0]["found"] == {"request": 1, "answer": 0}


def test_the_program_sent_to_a_container_calls_emit_with_exactly_the_settings(
    tree: ModuleType,
) -> None:
    asked = {"key": KEY.hex(), "mode": "retention", "tokens": {"request": 'it\'s "q"'}}

    text = tree.program(asked)

    call = ast.parse(text.splitlines()[-1]).body[0].value
    assert (call.func.id, json.loads(ast.literal_eval(call.args[0]))) == ("emit", asked)
    assert text.startswith('"""The in-container census')
