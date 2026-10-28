"""The fixtures the `hal rename` tests share: a temporary HOME, a repository to rename and fake
boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest
from rename_support import HTTPS, SSH, Fakes, make_clone, memory_of, squeezed
from terminal_support import type_at_prompt

from hal import cli, rename_commands
from hal.rename_commands import World


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


@pytest.fixture
def cli_world(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    """The fake world behind `hal.cli.main`, which reaches the world only through `real_world`."""
    monkeypatch.setattr(rename_commands, "real_world", lambda path: replace(world, home=path))
    return world


@pytest.fixture
def through_cli(cli_world, capsys, monkeypatch):
    """`hal rename` through `hal.cli.main` with a person who types `yes` unless told otherwise,
    returning the exit code and the two streams, their column padding collapsed."""

    def run(*arguments: str, answer: str | type[BaseException] | None = "yes", while_waiting=None):
        type_at_prompt(monkeypatch, answer, while_waiting)
        try:
            code = cli.main(["rename", *arguments])
        except SystemExit as stop:  # a usage error ends the process
            code = stop.code
        captured = capsys.readouterr()
        return code, squeezed(captured.out), squeezed(captured.err)

    return run


@pytest.fixture
def world(home: Path, fakes: Fakes) -> World:
    return World(
        home=home,
        github_knows=lambda name: name in fakes.known,
        running_in=fakes.scan,
        uv=fakes.uv,
        uv_tools=lambda: fakes.tools,
        is_interactive=lambda: fakes.terminal,
        clock=fakes.clock,
    )


@pytest.fixture
def repository(home: Path) -> Path:
    """`~/git/old` with two worktrees and a Claude memory directory."""
    clone = make_clone(home / "git" / "old", SSH.format("old"), ("lane", "other"))
    memory = memory_of(home, clone) / "memory"
    memory.mkdir(parents=True)
    (memory / "note.md").write_text("remembered")
    return clone


@pytest.fixture
def locked(repository):
    (repository / "uv.lock").write_text("")
    (repository / ".gitignore").write_text(".venv/\n")
    (repository / ".venv").mkdir()
    return repository


@pytest.fixture
def everything(home, locked) -> None:
    """The clone with two worktrees, memory and a lock file, and a deploy checkout."""
    make_clone(home / "old-live", HTTPS.format("old"))
