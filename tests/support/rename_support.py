"""What the `hal rename` tests share: a temporary HOME, real git clones and fake boundaries
for `gh`, the process scan and `uv`."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from support.git_support import committed_main, plain_git
from support.terminal_support import type_at_prompt

from hal import cli, rename_commands
from hal.rename_commands import RunningProcess, World

SSH = "git@github.com:overnightworks/{}.git"

HTTPS = "https://github.com/overnightworks/{}.git"


@dataclass
class Fakes:
    """What the fake boundaries answer, and what they were asked."""

    known: set[str] = field(default_factory=lambda: {"fresh"})
    running: list[RunningProcess] = field(default_factory=list)
    tools: str = ""
    uv_calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)
    terminal: bool = True
    now: float = 0.0
    seconds_per_uv_call: float = 0.0
    seconds_per_scan: float = 0.0

    def uv(self, project: Path, arguments: tuple[str, ...]) -> None:
        self.uv_calls.append((project, arguments))
        self.now += self.seconds_per_uv_call

    def scan(self, roots: Sequence[Path]) -> list[RunningProcess]:
        self.now += self.seconds_per_scan
        return [
            session
            for session in self.running
            if any(session.directory.is_relative_to(root) for root in roots)
        ]

    def clock(self) -> float:
        return self.now


def make_clone(path: Path, url: str, worktrees: tuple[str, ...] = ()) -> Path:
    """A clone at `path` whose origin is `url`, with linked worktrees in `<path>-worktrees`."""
    committed_main(path)
    plain_git(path, "remote", "add", "origin", url)
    plain_git(path, "switch", "--detach")
    for name in worktrees:
        plain_git(
            path, "worktree", "add", "-b", name,
            str(path.parent / f"{path.name}-worktrees" / name),
        )  # fmt: skip
    return path


def memory_of(home: Path, clone: Path) -> Path:
    return rename_commands.memory_directory(home, clone)


def squeezed(text: str) -> str:
    """Each line with its column padding collapsed, so a test reads the words, not the widths."""
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def origin_of(path: Path) -> str:
    return plain_git(path, "config", "--get", "remote.origin.url").stdout.strip()


def tree_state(home: Path) -> list[str]:
    return sorted(
        str(path.relative_to(home)) for path in home.rglob("*") if ".git/" not in str(path)
    )


def worktree_registrations(clone: Path) -> dict[str, str]:
    """The files `git worktree repair` rewrites: the gitdir link of each registered worktree and
    the `.git` pointer file in each linked worktree, with their contents."""
    links = [*(clone / ".git" / "worktrees").glob("*/gitdir")]
    pointers = [*(clone.parent / f"{clone.name}-worktrees").glob("*/.git")]
    return {str(path): path.read_text() for path in (*links, *pointers)}


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
