"""What the `hal rename` tests share: a temporary HOME, real git clones and fake boundaries
for `gh`, the process scan and `uv`."""

from dataclasses import dataclass, field
from pathlib import Path

from git_support import committed_main, plain_git

from hal import rename_commands
from hal.rename_commands import RunningProcess

SSH = "git@github.com:overnightworks/{}.git"

HTTPS = "https://github.com/overnightworks/{}.git"


@dataclass
class Fakes:
    """What the fake boundaries answer, and what they were asked."""

    known: set[str] = field(default_factory=lambda: {"fresh"})
    running: list[RunningProcess] = field(default_factory=list)
    tools: str = ""
    uv_calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)


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
