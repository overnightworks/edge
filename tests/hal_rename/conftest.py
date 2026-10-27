"""The fixtures the `hal rename` tests share: a temporary HOME, a repository to rename and fake
boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest
from rename_support import SSH, Fakes, make_clone, memory_of

from hal import rename_commands
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
def world(home: Path, fakes: Fakes) -> World:
    return World(
        home=home,
        github_knows=lambda name: name in fakes.known,
        running_in=lambda roots: [
            session
            for session in fakes.running
            if any(session.directory.is_relative_to(root) for root in roots)
        ],
        uv=lambda project, arguments: fakes.uv_calls.append((project, arguments)),
        uv_tools=lambda: fakes.tools,
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
