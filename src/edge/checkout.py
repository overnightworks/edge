"""The live checkout: `~/git/edge` on `main` is the tree the live stack runs from (P6).

`edge apply` binds the compose project `edge` to the tree its code runs from, so it runs only
from the live checkout (#79 rule 1). Whether a tree is that checkout is read from paths alone,
without git: it is `$HOME/git/edge` once both are resolved, and it is a main worktree, whose `.git`
is a directory where a linked worktree has a `.git` file.

Whether that checkout is a clean `main` is asked of git, through the one runner here (#95 rule 7).
It runs no hook, no fsmonitor and no maintenance, ignores every inherited `GIT_*` variable and
never prompts; its local profile reads no global or system configuration either, so no filter,
driver or helper of the operator's runs. The repository's own `.git/config` is same-user and still
read (D14).
"""

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from edge import compose

LIVE_CHECKOUT = "~/git/edge"
MAIN = "main"
ENVIRONMENT_FILE = ".env"
ENVIRONMENT_FILE_HINT = "cp .env.example .env"
_BRANCH_PREFIX = "refs/heads/"
_ISOLATION = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "submodule.recurse=false",
)  # fmt: skip
_NEVER_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


class Profile(Enum):
    """LOCAL for every step but a fetch: no global or system configuration. TRANSFER for a fetch
    (S2.4b): the global configuration stays, for the operator's credential helper."""

    LOCAL = "local"
    TRANSFER = "transfer"


_PROFILE_ENVIRONMENT = {
    Profile.LOCAL: {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    Profile.TRANSFER: {},
}
_PROFILE_SECONDS = {Profile.LOCAL: 30, Profile.TRANSFER: 120}


class GitError(Exception):
    """A git call did not run, failed or answered nonsense; the message is one line."""


Runner = Callable[[Path, Sequence[str], Profile], subprocess.CompletedProcess[bytes]]


def run_git(
    tree: Path, arguments: Sequence[str], profile: Profile
) -> subprocess.CompletedProcess[bytes]:
    """One git call on `tree`, isolated as rule 7 says; raises OSError or TimeoutExpired."""
    return subprocess.run(
        ["git", "--no-pager", "-C", str(tree), *_ISOLATION, *arguments],
        env=git_environment(os.environ, profile),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        close_fds=True,
        timeout=_PROFILE_SECONDS[profile],
    )


def git_environment(inherited: Mapping[str, str], profile: Profile) -> dict[str, str]:
    """`inherited` without any `GIT_*` variable, so none can point git elsewhere, and never
    prompting; the profile's own variables on top."""
    kept = {name: value for name, value in inherited.items() if not name.startswith("GIT_")}
    return {**kept, **_NEVER_PROMPT, **_PROFILE_ENVIRONMENT[profile]}


@dataclass(frozen=True)
class Git:
    """The questions `edge apply` asks of a checkout; nothing here writes to it."""

    tree: Path
    run: Runner = run_git

    def branch(self) -> str | None:
        """The branch HEAD names, or None when HEAD is detached."""
        result = self._call(["symbolic-ref", "--quiet", "HEAD"], accepted=(0, 1))
        if result.returncode == 1:
            return None
        reference = _text(result.stdout).strip()
        if not reference.startswith(_BRANCH_PREFIX):
            raise GitError(f"`git symbolic-ref` answered {compose.printable(reference)!r}")
        return reference.removeprefix(_BRANCH_PREFIX)

    def short_head(self) -> str:
        head = _text(self._call(["rev-parse", "--short", "HEAD"]).stdout).strip()
        if not head or not all(digit in "0123456789abcdef" for digit in head):
            raise GitError(f"`git rev-parse` answered {compose.printable(head)!r}")
        return head

    def has_commit(self) -> bool:
        """Whether HEAD names a commit; a branch without one is unborn."""
        verified = ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
        return self._call(verified, accepted=(0, 1)).returncode == 0

    def changed_paths(self) -> list[str]:
        """Every path that differs from HEAD: modified, staged or untracked, not ignored. The
        index is never refreshed, so the call writes nothing (`--no-optional-locks`)."""
        status = ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        return _porcelain_paths(self._call(status).stdout)

    def _call(
        self, arguments: Sequence[str], accepted: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[bytes]:
        verb = next(argument for argument in arguments if not argument.startswith("-"))
        try:
            result = self.run(self.tree, arguments, Profile.LOCAL)
        except subprocess.TimeoutExpired as error:
            raise GitError(f"`git {verb}` took longer than {error.timeout}s") from error
        except OSError as error:
            raise GitError(f"cannot run git ({error.strerror})") from error
        if result.returncode not in accepted:
            last = compose.last_line(_text(result.stderr) + _text(result.stdout))
            raise GitError(f"`git {verb}` exited {result.returncode}: {last}")
        return result


def _text(output: bytes) -> str:
    return output.decode("utf-8", "backslashreplace")


def _porcelain_paths(output: bytes) -> list[str]:
    """The paths of `status --porcelain=v1 -z`, decoded and escaped for one line each; a rename or
    copy names its source too."""
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise GitError("`git status` answered no porcelain")
    entries = iter(output[:-1].split(b"\0"))
    paths = []
    for entry in entries:
        if len(entry) < 4 or entry[2:3] != b" ":
            raise GitError(f"`git status` answered {compose.printable(_text(entry))!r}")
        paths.append(entry[3:])
        if {entry[0], entry[1]} & set(b"RC"):
            if (source := next(entries, None)) is None:
                raise GitError("`git status` named a rename without its source")
            paths.append(source)
    return [compose.printable(_text(path)) for path in paths]


def live_checkout(home: Path) -> Path:
    return home / "git" / "edge"


def not_live_checkout(tree: Path, home: Path) -> str | None:
    """None when `tree` is the live checkout, else why not (#95 rule 2)."""
    if tree.resolve() != live_checkout(home).resolve():
        return f"this is {compose.printable(str(tree))}"
    if (tree / ".git").is_file():
        return "a linked worktree, not the main checkout"
    if not (tree / ".git").is_dir():
        return "no git checkout"
    return None


def has_environment_file(tree: Path) -> bool:
    """Whether `.env` is a file, followed through a symlink; it is never opened (#95 rule 3)."""
    return (tree / ENVIRONMENT_FILE).is_file()


def not_on_main(git: Git) -> str | None:
    """None when HEAD is a `main` with a commit, else what it is (#95 rule 5); raises GitError."""
    branch = git.branch()
    if branch is None:
        return f"HEAD is detached at {git.short_head()}"
    if branch != MAIN:
        return f"this is branch {compose.printable(branch)}"
    if not git.has_commit():
        return f"{MAIN} has no commit yet"
    return None
