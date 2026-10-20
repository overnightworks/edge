"""`hal version`: which code runs, read from the checkout the code lives in, with real git under a
throwaway HOME, through the console script's program (`hal_program`)."""

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest
from git_support import (
    advance,
    checkout_state,
    hal_program,
    linked_worktree,
    local_commit,
    main_checkout,
    plain_git,
    run_hal,
    subjects,
)

from hal.version import NO_CHECKOUT

READ_VERBS = {"rev-parse", "symbolic-ref", "status", "rev-list"}
MARKER = "planted-environment-marker"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME and XDG_CONFIG_HOME: no global config but the one a test writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LC_ALL", "C")
    return home


@dataclass(frozen=True)
class GitRecord:
    """A `git` first on PATH that logs its arguments, then runs the real one."""

    directory: Path
    log: Path

    def calls(self) -> list[list[str]]:
        """The arguments after run_git's isolation (`-c key=value` pairs), one list per call."""
        if not self.log.exists():
            return []
        records = [record.split("\0")[:-1] for record in self.log.read_text().split("\n")[:-1]]
        return [
            record[max(i for i, word in enumerate(record) if word == "-c") + 2 :]
            for record in records
        ]


@pytest.fixture
def git_record(tmp_path: Path) -> GitRecord:
    directory, log = tmp_path / "record", tmp_path / "record.log"
    directory.mkdir()
    real_git = shutil.which("git")
    script = directory / "git"
    script.write_text(
        f"#!/bin/sh\n{{ printf '%s\\0' \"$@\"; printf '\\n'; }} >> {log}\nexec {real_git} \"$@\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return GitRecord(directory, log)


def version(tmp_path: Path, tree: Path, *options: str, **environment: Path | str):
    return run_hal(tmp_path, hal_program(tree), "version", *options, **environment)


def fetch_ahead(tree: Path, bare: Path, count: int) -> str:
    tip = advance(bare, *subjects(*(f"waiting {number}" for number in range(count))))
    plain_git(tree, "fetch", "--quiet", "origin")
    return tip


def test_a_clean_main_that_is_current_says_so(tmp_path):
    tree = main_checkout(tmp_path)

    result = version(tmp_path, tree)

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        f"commit: {plain_git(tree, 'rev-parse', '--short', 'HEAD').stdout.strip()}",
        "branch: main",
        "tree: clean",
        "origin/main: current",
    ]


@pytest.mark.parametrize("worktree", ["the main checkout", "a linked worktree"])
def test_the_code_names_the_checkout_it_lives_in_whatever_the_cwd(tmp_path, worktree):
    """G-12: hal imported from a checkout that carries it, run from a directory outside any."""
    main = main_checkout(tmp_path, carrying_hal=True)
    tree = main if worktree == "the main checkout" else linked_worktree(main, tmp_path)

    result = run_hal(tmp_path, hal_program(), "version", source=tree / "src")

    short = plain_git(tree, "rev-parse", "--short", "HEAD").stdout.strip()
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        f"commit: {short}",
        "branch: main",
        "tree: clean",
        "origin/main: current",
    ]


def test_a_changed_path_and_the_fetched_standing_are_read_without_writing(tmp_path, git_record):
    """G-13: it never fetches, so a commit origin gained after the last fetch is not counted."""
    tree = main_checkout(tmp_path)
    bare = tmp_path / "origin.git"
    fetch_ahead(tree, bare, 2)
    advance(bare, *subjects("not fetched"))
    with (tree / ".gitignore").open("a") as ignores:
        ignores.write("changed\n")
    before = checkout_state(tree, may_change=())

    result = version(tmp_path, tree, path_first=git_record.directory)

    assert result.stdout.splitlines()[2:] == ["tree: 1 changed path", "origin/main: 2 behind"]
    assert checkout_state(tree, may_change=()) == before
    calls = git_record.calls()
    assert calls
    assert {next(word for word in call if not word.startswith("-")) for call in calls} <= READ_VERBS
    assert all("--no-optional-locks" in call for call in calls if "status" in call)


def test_several_changed_paths_are_counted(tmp_path):
    tree = main_checkout(tmp_path)
    (tree / "one").touch()
    (tree / "two").touch()

    assert "tree: 2 changed paths" in version(tmp_path, tree).stdout


def test_a_detached_head_says_where(tmp_path):
    """G-14: the rollback state."""
    tree = main_checkout(tmp_path)
    plain_git(tree, "switch", "--detach")

    short = plain_git(tree, "rev-parse", "--short", "HEAD").stdout.strip()
    assert f"branch: detached at {short}" in version(tmp_path, tree).stdout.splitlines()


def test_a_checkout_that_never_fetched_says_so(tmp_path):
    """G-15"""
    tree = main_checkout(tmp_path)
    plain_git(tree, "remote", "remove", "origin")

    assert "origin/main: never fetched" in version(tmp_path, tree).stdout.splitlines()


def test_a_tree_without_git_beside_the_code_is_no_checkout_though_one_encloses_it(
    tmp_path, git_record
):
    """G-16: git is not asked, so it cannot walk up into the enclosing checkout (L11)."""
    installed = main_checkout(tmp_path) / "build"
    installed.mkdir()

    result = version(tmp_path, installed, path_first=git_record.directory)

    assert (result.returncode, result.stdout) == (0, f"{NO_CHECKOUT}\n")
    assert git_record.calls() == []


def test_verbose_prints_each_git_call_on_stderr_and_never_the_environment(tmp_path, git_record):
    """G-17"""
    tree = main_checkout(tmp_path)
    quiet = version(tmp_path, tree, path_first=git_record.directory)
    git_record.log.unlink()

    verbose = version(tmp_path, tree, "-v", path_first=git_record.directory, HAL_MARKER=MARKER)

    assert verbose.stdout == quiet.stdout
    assert verbose.stderr.splitlines() == [f"git {' '.join(call)}" for call in git_record.calls()]
    assert MARKER not in verbose.stdout + verbose.stderr


@pytest.mark.parametrize(
    ("local", "waiting", "expected"),
    [
        pytest.param(1, 0, "1 ahead", id="ahead"),
        pytest.param(1, 2, "diverged (1 ahead, 2 behind)", id="diverged"),
    ],
)
def test_a_head_ahead_of_the_fetched_main_names_both_counts(tmp_path, local, waiting, expected):
    """G-21"""
    tree = main_checkout(tmp_path)
    local_commit(tree)
    if waiting:
        fetch_ahead(tree, tmp_path / "origin.git", waiting)

    assert f"origin/main: {expected}" in version(tmp_path, tree).stdout.splitlines()


def test_a_branch_name_is_escaped_onto_one_line(tmp_path):
    """G-22: U+202E would flip the line in a terminal."""
    tree = main_checkout(tmp_path)
    plain_git(tree, "switch", "--quiet", "--create", "fix\u202ex")

    result = version(tmp_path, tree)

    assert "branch: fix\\u202ex" in result.stdout.splitlines()
    assert "\u202e" not in result.stdout
    assert len(result.stdout.splitlines()) == 4
