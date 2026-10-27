"""The census's watch mode (#469, agent-runtime#440 B9 (b) and (c)): when the CLI's process group
and the run tree were first seen gone. Each test runs `watch_for` on a fake /proc and a temporary
working root that a script changes between two samples, on fake clocks."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

PROGRAM = Path(__file__).resolve().parent.parent / "probe" / "agent-service" / "tree_census.py"
KEY = bytes(range(32))
SERVICE, CLI, TOOL = 7, 20, 21
INTERVAL = 0.01
REPORT_KEYS = {"group_seen", "group_gone_at", "tree_seen", "tree_gone_at", "samples"}


@pytest.fixture(scope="module")
def census() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("agent_probe_tree_census_watch", PROGRAM)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through it
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


def clear(directory: Path) -> None:
    """Empties the directory in place: the watch holds it open by descriptor."""
    for child in directory.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


class Scene:
    """A fake /proc, a working root and the clocks; `frames` is what each sample finds in turn:
    the processes as {pid: (ppid, pgid)} and how many entries the working root holds."""

    def __init__(self, root: Path, frames: list[tuple[dict[int, tuple[int, int]], int]]) -> None:
        self.proc, self.work = root / "proc", root / "work"
        self.stop = root / "stop"
        self.proc.mkdir()
        self.work.mkdir()
        self.frames = frames
        self.index = 0
        self.now = 0.0
        self.show(0)

    def show(self, index: int) -> None:
        processes, entries = self.frames[min(index, len(self.frames) - 1)]
        clear(self.proc)
        for pid, (ppid, pgid) in processes.items():
            (self.proc / str(pid)).mkdir()
            (self.proc / str(pid) / "stat").write_text(
                f"{pid} (name) S {ppid} {pgid} {pgid} 0 0 0", encoding="utf-8"
            )
        clear(self.work)
        for number in range(entries):
            (self.work / f"entry-{number}").touch()

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.index += 1
        self.show(self.index)

    def watch(self, census: ModuleType, deadline: float) -> dict:
        return census.watch_for(
            str(self.proc), str(self.work), census.Names(KEY, 1000), deadline, INTERVAL,
            str(self.stop), clock=lambda: self.now, sleep=self.sleep, wall=lambda: 1000 + self.now,
        )  # fmt: skip


RUNNING = {1: (0, 1), SERVICE: (1, SERVICE), CLI: (SERVICE, CLI), TOOL: (CLI, CLI)}
IDLE = {1: (0, 1), SERVICE: (1, SERVICE)}


def test_the_watch_reports_when_the_group_and_the_tree_were_first_seen_gone(census, tmp_path):
    """B9 (b), (c): wall times of the first sample that found each one gone, then it stops."""
    scene = Scene(
        tmp_path, [(IDLE, 0), (RUNNING, 2), (RUNNING, 2), (IDLE, 2), (IDLE, 0), (IDLE, 0)]
    )
    report = scene.watch(census, deadline=60)
    assert report == {
        "group_seen": True, "group_gone_at": 1000 + 3 * INTERVAL,
        "tree_seen": True, "tree_gone_at": 1000 + 4 * INTERVAL, "samples": 5,
    }  # fmt: skip


def test_the_watch_reports_only_booleans_wall_times_and_a_count(census, tmp_path):
    scene = Scene(tmp_path, [(RUNNING, 1)])
    assert set(scene.watch(census, deadline=0)) == REPORT_KEYS


def test_the_watch_that_sees_nothing_runs_to_its_deadline(census, tmp_path):
    scene = Scene(tmp_path, [(IDLE, 0)])
    report = scene.watch(census, deadline=0.05)
    assert report["group_seen"] is report["tree_seen"] is False
    assert report["group_gone_at"] is report["tree_gone_at"] is None
    assert report["samples"] == 6


def test_a_group_that_never_empties_has_no_time(census, tmp_path):
    scene = Scene(tmp_path, [(RUNNING, 1), (RUNNING, 0)])
    report = scene.watch(census, deadline=0.05)
    assert (report["group_seen"], report["group_gone_at"]) == (True, None)
    assert report["tree_gone_at"] is not None


def test_the_stop_file_ends_the_watch_at_once(census, tmp_path):
    scene = Scene(tmp_path, [(IDLE, 0)])
    scene.stop.touch()
    assert scene.watch(census, deadline=60)["samples"] == 1


def test_the_cli_group_is_a_child_of_the_lowest_process_below_init_that_leads_its_group(
    census, tmp_path
):
    """A detached process below init (a later pid) is no CLI; the service's own group is none."""
    detached = {**IDLE, 30: (1, 30), 31: (30, 31)}
    scene = Scene(tmp_path, [(detached, 0), (detached, 0)])
    assert scene.watch(census, deadline=0.01)["group_seen"] is False


def test_the_watch_mode_runs_through_the_census_entry_point(census, tmp_path):
    """`run` dispatches the mode: a one-sample watch on the fake /proc answers with its report."""
    scene = Scene(tmp_path, [(RUNNING, 1)])
    settings = {
        "key": KEY.hex(), "mode": "watch", "proc": str(scene.proc),
        "working_root": str(scene.work), "deadline": 0, "interval": INTERVAL,
        "stop_path": str(scene.stop),
    }  # fmt: skip
    report = census.run(json.dumps(settings))
    assert set(report) == REPORT_KEYS
    assert report["tree_seen"] is True
