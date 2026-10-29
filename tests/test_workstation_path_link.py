"""The path step of `hal setup`: the link `~/.local/bin/hal` to the `bin/hal` of the checkout hal runs from (#655)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from hal.workstation.layout import Layout
from hal.workstation.path_link import PathLink
from hal.workstation.system_step import Applied, CheckState, StepFailed

HAL_PROJECT = "overnightworks-hal"


def make_checkout(root: Path, project: str | None = HAL_PROJECT) -> Path:
    """A checkout's `bin/hal`; `project` is the name its pyproject.toml gives, or no pyproject at all."""
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "hal"
    launcher.write_text("#!/bin/sh\n")
    if project is not None:
        (root / "pyproject.toml").write_text(f'[project]\nname = "{project}"\n')
    return launcher


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home" / ".local" / "bin").mkdir(parents=True)
    return tmp_path / "home"


@pytest.fixture
def running(home: Path) -> Path:
    return home / "git" / "hal"


@pytest.fixture
def link(home: Path, running: Path) -> PathLink:
    make_checkout(running)
    return PathLink(Layout(home), running)


def entry_of(home: Path) -> Path:
    return home / ".local" / "bin" / "hal"


def test_a_missing_link_is_created_and_the_plan_shows_it_with_the_home_as_a_tilde(
    link: PathLink, home: Path, running: Path
) -> None:
    assert link.check() is CheckState.WOULD_CHANGE
    assert link.fact() == "~/.local/bin/hal → ~/git/hal/bin/hal"
    assert not entry_of(home).is_symlink()

    assert link.apply() is Applied.CHANGED
    assert entry_of(home).readlink() == running / "bin" / "hal"


def test_a_missing_bin_directory_is_created_with_the_link(tmp_path: Path) -> None:
    home = tmp_path / "bare-home"
    running = home / "git" / "hal"
    make_checkout(running)

    assert PathLink(Layout(home), running).apply() is Applied.CHANGED
    assert entry_of(home).readlink() == running / "bin" / "hal"


def test_a_link_that_already_points_here_is_unchanged_and_stays_the_same_link(
    link: PathLink, home: Path, running: Path
) -> None:
    entry_of(home).symlink_to(running / "bin" / "hal")
    before = entry_of(home).lstat().st_ino

    assert link.check() is CheckState.UNCHANGED
    assert link.apply() is Applied.UNCHANGED
    assert entry_of(home).lstat().st_ino == before


def test_a_wheel_without_a_checkout_touches_nothing(home: Path) -> None:
    entry_of(home).write_text("uv's own script\n")
    wheel = PathLink(Layout(home), None)

    assert wheel.check() is CheckState.UNCHANGED
    assert wheel.apply() is Applied.UNCHANGED
    assert entry_of(home).read_text() == "uv's own script\n"


def link_to_a_moved_clone(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "edge" / "bin" / "hal")


def link_to_another_hal_clone(home: Path) -> None:
    other = home / "elsewhere" / "land"
    make_checkout(other)
    entry_of(home).symlink_to(other / "bin" / "hal")


def relative_link_to_another_hal_clone(home: Path) -> None:
    make_checkout(home / "git" / "hal-land")
    entry_of(home).symlink_to(Path("..") / ".." / "git" / "hal-land" / "bin" / "hal")


@pytest.mark.parametrize(
    "arrange",
    [link_to_a_moved_clone, link_to_another_hal_clone, relative_link_to_another_hal_clone],
    ids=[
        "missing clone directly under ~/git",
        "existing clone named overnightworks-hal",
        "relative target",
    ],
)
def test_a_link_into_another_hal_checkout_is_repointed_atomically_after_the_plan(
    link: PathLink, home: Path, running: Path, arrange: Callable[[Path], None]
) -> None:
    arrange(home)
    before = entry_of(home).readlink()

    assert link.check() is CheckState.WOULD_CHANGE
    assert entry_of(home).readlink() == before
    assert link.apply() is Applied.CHANGED
    assert entry_of(home).readlink() == running / "bin" / "hal"
    assert sorted(path.name for path in entry_of(home).parent.iterdir()) == ["hal"]


def a_regular_file(home: Path) -> None:
    entry_of(home).write_text("the person's own hal\n")


def a_directory(home: Path) -> None:
    entry_of(home).mkdir()


def link_to_a_system_binary(home: Path) -> None:
    entry_of(home).symlink_to("/usr/bin/true")


def link_to_an_older_clone_of_another_project(home: Path) -> None:
    older = home / "git" / "older"
    make_checkout(older, project="home")
    entry_of(home).symlink_to(older / "bin" / "hal")


def link_to_a_tree_without_pyproject(home: Path) -> None:
    bare = home / "git" / "bare"
    make_checkout(bare, project=None)
    entry_of(home).symlink_to(bare / "bin" / "hal")


def link_to_a_missing_tree_outside_git(home: Path) -> None:
    entry_of(home).symlink_to(home / "elsewhere" / "gone" / "bin" / "hal")


def link_to_a_missing_tree_deeper_in_git(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "group" / "gone" / "bin" / "hal")


def link_to_a_missing_file_not_named_hal(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "gone" / "bin" / "other")


def link_to_a_hal_file_outside_a_bin_directory(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "gone" / "hal")


@pytest.mark.parametrize(
    ("arrange", "kind"),
    [
        (a_regular_file, "a file of its own"),
        (a_directory, "a directory"),
        (link_to_a_system_binary, "a link to /usr/bin/true"),
        (link_to_an_older_clone_of_another_project, "a link to"),
        (link_to_a_tree_without_pyproject, "a link to"),
        (link_to_a_missing_tree_outside_git, "a link to"),
        (link_to_a_missing_tree_deeper_in_git, "a link to"),
        (link_to_a_missing_file_not_named_hal, "a link to"),
        (link_to_a_hal_file_outside_a_bin_directory, "a link to"),
    ],
)
def test_an_entry_that_is_not_provably_a_hal_checkout_fails_the_plan_and_is_never_replaced(
    link: PathLink, home: Path, arrange: Callable[[Path], None], kind: str
) -> None:
    arrange(home)
    before = (
        entry_of(home).is_symlink(),
        os.readlink(entry_of(home)) if entry_of(home).is_symlink() else None,
    )

    with pytest.raises(StepFailed) as refusal:
        link.check()

    assert str(refusal.value).startswith(f"~/.local/bin/hal is {kind}")
    assert "you decide: move it away or delete it, then rerun" in str(refusal.value)
    with pytest.raises(StepFailed):
        link.apply()
    assert (
        entry_of(home).is_symlink(),
        os.readlink(entry_of(home)) if entry_of(home).is_symlink() else None,
    ) == before


def test_the_refusal_escapes_what_a_terminal_would_act_on(link: PathLink, home: Path) -> None:
    entry_of(home).symlink_to("/tmp/\x1b[31mred\nline/bin/hal")

    with pytest.raises(StepFailed) as refusal:
        link.check()

    assert "\x1b" not in str(refusal.value)
    assert "\n" not in str(refusal.value)


def test_no_entry_named_home_or_edge_is_created_or_read(link: PathLink, home: Path) -> None:
    dead_home = entry_of(home).with_name("home")
    dead_home.symlink_to(home / "git" / "edge" / "bin" / "home")

    link.apply()

    assert sorted(path.name for path in entry_of(home).parent.iterdir()) == ["hal", "home"]
    assert dead_home.readlink() == home / "git" / "edge" / "bin" / "home"
