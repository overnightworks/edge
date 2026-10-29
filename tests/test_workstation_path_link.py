"""The path step of `hal setup`: the link `~/.local/bin/hal` to the `bin/hal` of the live checkout (#655, #670)."""

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
def live(home: Path) -> Path:
    return Layout(home).live_checkout


@pytest.fixture
def link(home: Path, live: Path) -> PathLink:
    make_checkout(live)
    return PathLink(Layout(home))


def entry_of(home: Path) -> Path:
    return home / ".local" / "bin" / "hal"


def test_a_missing_link_is_created_and_the_plan_shows_it_with_the_home_as_a_tilde(
    link: PathLink, home: Path, live: Path
) -> None:
    assert link.check() is CheckState.WOULD_CHANGE
    assert link.fact() == "~/.local/bin/hal → ~/srv/hal/bin/hal"
    assert not entry_of(home).is_symlink()

    assert link.apply() is Applied.CHANGED
    assert entry_of(home).readlink() == live / "bin" / "hal"


def test_a_missing_bin_directory_is_created_with_the_link(tmp_path: Path) -> None:
    home = tmp_path / "bare-home"
    live = Layout(home).live_checkout
    make_checkout(live)

    assert PathLink(Layout(home)).apply() is Applied.CHANGED
    assert entry_of(home).readlink() == live / "bin" / "hal"


def test_a_link_that_already_points_here_is_unchanged_and_stays_the_same_link(
    link: PathLink, home: Path, live: Path
) -> None:
    entry_of(home).symlink_to(live / "bin" / "hal")
    before = entry_of(home).lstat().st_ino

    assert link.check() is CheckState.UNCHANGED
    assert link.apply() is Applied.UNCHANGED
    assert entry_of(home).lstat().st_ino == before


def test_a_relative_link_that_already_points_here_is_unchanged(link: PathLink, home: Path, live: Path) -> None:
    entry_of(home).symlink_to(Path(os.path.relpath(live / "bin" / "hal", entry_of(home).parent)))

    assert link.check() is CheckState.UNCHANGED
    assert link.apply() is Applied.UNCHANGED


def test_a_missing_live_launcher_leaves_the_link_alone_and_the_row_unchanged(home: Path) -> None:
    entry_of(home).write_text("uv's own script\n")
    absent = PathLink(Layout(home))

    assert absent.check() is CheckState.UNCHANGED
    assert absent.fact() == ""
    assert absent.apply() is Applied.UNCHANGED
    assert entry_of(home).read_text() == "uv's own script\n"


def test_setup_run_from_another_tree_links_the_live_checkout(home: Path, live: Path) -> None:
    make_checkout(live)
    make_checkout(home / "git-worktrees" / "issue-1-lane")

    assert PathLink(Layout(home)).apply() is Applied.CHANGED
    assert entry_of(home).readlink() == live / "bin" / "hal"


def link_to_a_moved_clone(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "hal" / "bin" / "hal")


def link_to_another_hal_clone(home: Path) -> None:
    other = home / "elsewhere" / "land"
    make_checkout(other)
    entry_of(home).symlink_to(other / "bin" / "hal")


def relative_link_to_another_hal_clone(home: Path) -> None:
    make_checkout(home / "git" / "hal-land")
    entry_of(home).symlink_to(Path("..") / ".." / "git" / "hal-land" / "bin" / "hal")


def relative_link_to_a_moved_clone(home: Path) -> None:
    entry_of(home).symlink_to(Path("..") / ".." / "git" / "hal" / "bin" / "hal")


def link_to_a_moved_clone_while_git_is_a_symlink(home: Path) -> None:
    (home / "git").mkdir()
    (home / "git").rename(home / "real-git")
    (home / "git").symlink_to(home / "real-git")
    entry_of(home).symlink_to(home / "git" / "hal" / "bin" / "hal")


@pytest.mark.parametrize(
    "arrange",
    [
        link_to_a_moved_clone,
        link_to_another_hal_clone,
        relative_link_to_another_hal_clone,
        relative_link_to_a_moved_clone,
        link_to_a_moved_clone_while_git_is_a_symlink,
    ],
    ids=[
        "missing clone directly under ~/git",
        "existing clone named overnightworks-hal",
        "relative target",
        "relative target to a missing clone directly under ~/git",
        "missing clone under ~/git when ~/git is a symlink",
    ],
)
def test_a_link_into_another_hal_checkout_is_repointed_atomically_after_the_plan(
    link: PathLink, home: Path, live: Path, arrange: Callable[[Path], None]
) -> None:
    arrange(home)
    before = entry_of(home).readlink()

    assert link.check() is CheckState.WOULD_CHANGE
    assert entry_of(home).readlink() == before
    assert link.apply() is Applied.CHANGED
    assert entry_of(home).readlink() == live / "bin" / "hal"
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


def link_to_a_pyproject_whose_project_is_no_table(home: Path) -> None:
    foreign = home / "git" / "foreign"
    make_checkout(foreign, project=None)
    (foreign / "pyproject.toml").write_text('project = "other"\n')
    entry_of(home).symlink_to(foreign / "bin" / "hal")


def link_to_a_tree_with_an_unreadable_pyproject(home: Path) -> None:
    broken = home / "git" / "broken"
    make_checkout(broken, project=None)
    (broken / "pyproject.toml").write_text("[project\n")
    entry_of(home).symlink_to(broken / "bin" / "hal")


def link_to_a_pyproject_that_is_not_utf8(home: Path) -> None:
    foreign = home / "git" / "foreign"
    make_checkout(foreign, project=None)
    (foreign / "pyproject.toml").write_bytes(b'[project]\nname = "\xff\xfe"\n')
    entry_of(home).symlink_to(foreign / "bin" / "hal")


def link_through_a_symlinked_directory_and_dot_dot_to_a_foreign_tree(home: Path) -> None:
    child = home / "srv" / "tree" / "child"
    child.mkdir(parents=True)
    make_checkout(home / "srv" / "tree" / "foreign", project="other")
    (home / "git").mkdir(exist_ok=True)
    (home / "git" / "alias").symlink_to(child)
    entry_of(home).symlink_to(home / "git" / "alias" / ".." / "foreign" / "bin" / "hal")


def link_through_a_self_looping_directory(home: Path) -> None:
    (home / "git").mkdir(exist_ok=True)
    (home / "git" / "loop").symlink_to("loop")
    entry_of(home).symlink_to(home / "git" / "loop" / "bin" / "hal")


def link_to_a_missing_tree_outside_git(home: Path) -> None:
    entry_of(home).symlink_to(home / "elsewhere" / "gone" / "bin" / "hal")


def link_to_a_missing_tree_deeper_in_git(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "group" / "gone" / "bin" / "hal")


def link_to_a_missing_file_not_named_hal(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "gone" / "bin" / "other")


def link_to_a_hal_file_outside_a_bin_directory(home: Path) -> None:
    entry_of(home).symlink_to(home / "git" / "gone" / "hal")


@pytest.mark.parametrize(
    "arrange",
    [
        a_regular_file,
        a_directory,
        link_to_a_system_binary,
        link_to_an_older_clone_of_another_project,
        link_to_a_tree_without_pyproject,
        link_to_a_pyproject_whose_project_is_no_table,
        link_to_a_tree_with_an_unreadable_pyproject,
        link_to_a_pyproject_that_is_not_utf8,
        link_through_a_symlinked_directory_and_dot_dot_to_a_foreign_tree,
        link_through_a_self_looping_directory,
        link_to_a_missing_tree_outside_git,
        link_to_a_missing_tree_deeper_in_git,
        link_to_a_missing_file_not_named_hal,
        link_to_a_hal_file_outside_a_bin_directory,
    ],
)
def test_an_entry_that_is_not_provably_a_hal_checkout_fails_the_plan_and_is_never_replaced(
    link: PathLink, home: Path, arrange: Callable[[Path], None]
) -> None:
    arrange(home)
    before = (
        entry_of(home).is_symlink(),
        os.readlink(entry_of(home)) if entry_of(home).is_symlink() else None,
    )

    with pytest.raises(StepFailed) as refusal:
        link.check()

    assert (
        str(refusal.value) == "~/.local/bin/hal is not a hal link · move or delete it, then rerun"
    )
    with pytest.raises(StepFailed):
        link.apply()
    after = (
        entry_of(home).is_symlink(),
        os.readlink(entry_of(home)) if entry_of(home).is_symlink() else None,
    )
    assert after == before


def test_an_unrelated_file_beside_the_link_survives_the_creation_untouched(
    link: PathLink, home: Path
) -> None:
    for name in (".hal.new", "home"):
        entry_of(home).with_name(name).write_text(f"{name} of the person\n")

    link.apply()

    assert sorted(path.name for path in entry_of(home).parent.iterdir()) == [
        ".hal.new",
        "hal",
        "home",
    ]
    assert entry_of(home).with_name(".hal.new").read_text() == ".hal.new of the person\n"


def test_an_entry_named_home_beside_the_link_is_left_as_it_was_and_no_other_name_appears(
    link: PathLink, home: Path
) -> None:
    dead_home = entry_of(home).with_name("home")
    dead_home.symlink_to(home / "git" / "edge" / "bin" / "home")

    link.apply()

    assert sorted(path.name for path in entry_of(home).parent.iterdir()) == ["hal", "home"]
    assert dead_home.readlink() == home / "git" / "edge" / "bin" / "home"


def test_a_failed_replacement_leaves_the_entry_and_no_temporary_link_behind(
    link: PathLink, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError("no replacing today")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(PermissionError):
        link.apply()

    assert list(entry_of(home).parent.iterdir()) == []
