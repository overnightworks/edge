"""The live checkout: `~/git/edge` on `main` is the tree the live stack runs from (P6).

`edge apply` binds the compose project `edge` to the tree its code runs from, so it runs only
from the live checkout (#79 rule 1). Whether a tree is that checkout is read from paths alone,
without git: it is `$HOME/git/edge` once both are resolved, and it is a main worktree, whose `.git`
is a directory where a linked worktree has a `.git` file.

Whether that checkout is a clean `main` is asked of git, through the one runner here (#95 rule 7).
It runs no hook, no fsmonitor and no maintenance, ignores every inherited `GIT_*` variable and
never prompts; its local profile reads no global or system configuration either, so no filter,
driver or helper of the operator's runs. The repository's own `.git/config` is same-user and still
read (D14). The local profile also reads no global excludes or attributes file, so "ignored" means
only the repository's own `.gitignore` (#98 rule 6).

The one step that reaches beyond the machine is the fetch of origin's `main` (#98): it runs in the
transfer profile, which keeps the global configuration for the operator's credential helper,
refuses a URL that carries a password and never prompts. From the fetch on, the fetched sha is
pinned: the comparison with HEAD and the waiting commits and files are all read against it. The
fetch writes only objects, FETCH_HEAD and `origin/main`; HEAD, the index and the tree stay as
they are. A credential is never printed: whatever stands before `@` in a URL is redacted.
"""

import contextlib
import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from edge import compose
from hal_output import printable

LIVE_CHECKOUT = "~/git/edge"
MAIN = "main"
_BRANCH_PREFIX = "refs/heads/"
_ISOLATION = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "submodule.recurse=false",
)  # fmt: skip
_NEVER_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
_ERROR_PREFIXES = ("fatal:", "error:")
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s]+@", re.IGNORECASE)
_REDACTED = r"\1<redacted>@"
_STATUS_LETTERS = frozenset("ADMT")
_ORIGIN = "origin"
ORIGIN_MAIN = f"refs/remotes/{_ORIGIN}/{MAIN}"  # origin's `main` as the last fetch left it


class Profile(Enum):
    """LOCAL for every step but a fetch: no global or system configuration. TRANSFER for a fetch
    (S2.4b): the global configuration stays, for the operator's credential helper."""

    LOCAL = "local"
    TRANSFER = "transfer"


_PROFILE_ENVIRONMENT = {
    Profile.LOCAL: {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    Profile.TRANSFER: {},
}
_PROFILE_ARGUMENTS = {
    Profile.LOCAL: (
        "-c", f"core.excludesFile={os.devnull}",
        "-c", f"core.attributesFile={os.devnull}",
    ),
    Profile.TRANSFER: ("-c", "transfer.credentialsInUrl=die"),
}  # fmt: skip
TIMEOUT_SECONDS = {Profile.LOCAL: 30, Profile.TRANSFER: 120}


class Standing(Enum):
    """How HEAD stands to the fetched `main` (#98 rule 3)."""

    CURRENT = "current"
    BEHIND = "behind"
    AHEAD = "ahead"
    DIVERGED = "diverged"


class GitError(Exception):
    """A git call did not run, failed or answered nonsense; the message is one line."""


Runner = Callable[[Path, Sequence[str], Profile], subprocess.CompletedProcess[bytes]]


def run_git(
    tree: Path, arguments: Sequence[str], profile: Profile
) -> subprocess.CompletedProcess[bytes]:
    """One git call on `tree`, isolated as rule 7 says; raises OSError or TimeoutExpired. git runs
    in its own process group, and a call that does not finish ends with that whole group killed:
    a fetch's transport helpers would otherwise outlive the cut and go on writing objects after
    edge has said nothing changed (#98 rule 2)."""
    command = ["git", "--no-pager", "-C", str(tree), *_ISOLATION, *_PROFILE_ARGUMENTS[profile],
               *arguments]  # fmt: skip
    with subprocess.Popen(
        command,
        env=git_environment(os.environ, profile),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        process_group=0,
    ) as git:
        try:
            stdout, stderr = git.communicate(timeout=TIMEOUT_SECONDS[profile])
        except BaseException:
            _kill_group(git)
            raise
    return subprocess.CompletedProcess(command, git.returncode, stdout, stderr)


def _kill_group(git: subprocess.Popen[bytes]) -> None:
    """Kill every process of `git`'s group and wait until the last has closed its pipes."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(git.pid, signal.SIGKILL)
    git.communicate()


def git_environment(inherited: Mapping[str, str], profile: Profile) -> dict[str, str]:
    """`inherited` without any `GIT_*` variable, so none can point git elsewhere, and never
    prompting; the profile's own variables on top."""
    kept = {name: value for name, value in inherited.items() if not name.startswith("GIT_")}
    return {**kept, **_NEVER_PROMPT, **_PROFILE_ENVIRONMENT[profile]}


@dataclass(frozen=True)
class Origin:
    """origin's `main` as fetched, pinned by its full sha, against HEAD (#98 rules 1, 3)."""

    url: str  # the effective fetch URL, credentials redacted
    sha: str
    short: str
    head: str  # HEAD's short sha
    ahead: int  # commits HEAD has that the fetched main lacks
    behind: int  # commits the fetched main has that HEAD lacks

    @property
    def standing(self) -> Standing:
        return standing_of(self.ahead, self.behind)


def standing_of(ahead: int, behind: int) -> Standing:
    """How HEAD stands to a `main` it has `ahead` commits on and `behind` commits short of."""
    if ahead and behind:
        return Standing.DIVERGED
    if ahead:
        return Standing.AHEAD
    return Standing.BEHIND if behind else Standing.CURRENT


@dataclass(frozen=True)
class Commit:
    short: str
    subject: str  # escaped onto one line


@dataclass(frozen=True)
class ChangedFile:
    status: str  # A, D, M or T, as `diff --name-status` says
    path: str  # relative to the checkout, a byte that is no UTF-8 as `\\xNN`

    @property
    def shown(self) -> str:
        return printable(self.path)


@dataclass(frozen=True)
class Git:
    """The questions `edge apply` asks of a checkout; only the fetch writes, and only to `.git`."""

    tree: Path
    run: Runner = run_git

    def branch(self) -> str | None:
        """The branch HEAD names, or None when HEAD is detached."""
        result = self.call(["symbolic-ref", "--quiet", "HEAD"], accepted=(0, 1))
        if result.returncode == 1:
            return None
        reference = _text(result.stdout).strip()
        if not reference.startswith(_BRANCH_PREFIX):
            raise GitError(f"`git symbolic-ref` answered '{printable(reference)}'")
        return reference.removeprefix(_BRANCH_PREFIX)

    def short(self, revision: str = "HEAD") -> str:
        return _sha(
            _text(self.call(["rev-parse", "--short", revision]).stdout).strip(), "rev-parse"
        )

    def has_commit(self) -> bool:
        """Whether HEAD names a commit; a branch without one is unborn."""
        verified = ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
        return self.call(verified, accepted=(0, 1)).returncode == 0

    def has_fetched_main(self) -> bool:
        """Whether origin's `main` was ever fetched, so `ORIGIN_MAIN` names a commit."""
        verified = ["rev-parse", "--verify", "--quiet", f"{ORIGIN_MAIN}^{{commit}}"]
        return self.call(verified, accepted=(0, 1)).returncode == 0

    def changed_paths(self) -> list[str]:
        """Every path that differs from HEAD: modified, staged or untracked, not ignored. The
        index is never refreshed, so the call writes nothing (`--no-optional-locks`)."""
        status = ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        return _porcelain_paths(self.call(status).stdout)

    def fetch_url(self) -> str:
        """origin's effective fetch URL, rewritten as the global configuration says, credentials
        redacted (#98 rule 1)."""
        answer = self.call(["remote", "get-url", _ORIGIN], profile=Profile.TRANSFER)
        return without_credentials(printable(_text(answer.stdout).strip()))

    def fetch_main(self) -> str:
        """Fetch origin's `main` through the operator's credential helper and pin it: the full sha
        FETCH_HEAD now names (#98 rule 1). Writes only objects, FETCH_HEAD and `origin/main`."""
        fetch = ["fetch", "--no-tags", "--no-recurse-submodules", "--write-fetch-head"]
        self.call([*fetch, _ORIGIN, MAIN], profile=Profile.TRANSFER)
        pinned = self.call(["rev-parse", "--verify", "FETCH_HEAD^{commit}"])
        return _sha(_text(pinned.stdout).strip(), "rev-parse")

    def divergence(self, sha: str) -> tuple[int, int]:
        """The commits HEAD has that `sha` lacks, and those `sha` has that HEAD lacks."""
        answer = _text(self.call(["rev-list", "--left-right", "--count", f"HEAD...{sha}"]).stdout)
        counts = answer.split()
        if len(counts) != 2 or not all(count.isdigit() for count in counts):
            raise GitError(f"`git rev-list` answered '{printable(answer.strip())}'")
        return int(counts[0]), int(counts[1])

    def waiting_commits(self, sha: str) -> list[Commit]:
        """Every commit `sha` has that HEAD lacks, newest first, subjects escaped (#98 rule 4)."""
        log = ["log", "--no-color", "--no-show-signature", "-z", "--format=%h%x00%s"]
        fields = _nul_fields(self.call([*log, f"HEAD..{sha}"]).stdout, "log")
        return [
            Commit(_sha(_text(short), "log"), printable(_text(subject)))
            for short, subject in zip(fields[::2], fields[1::2], strict=True)
        ]

    def waiting_files(self, sha: str) -> list[ChangedFile]:
        """Every file that differs between HEAD and `sha`, with its status (#98 rule 4)."""
        diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "-z", "--name-status"]
        fields = _nul_fields(self.call([*diff, "HEAD", sha]).stdout, "diff")
        changed = []
        for status, path in zip(fields[::2], fields[1::2], strict=True):
            if _text(status) not in _STATUS_LETTERS:
                raise GitError(f"`git diff` answered '{printable(_text(status))}'")
            changed.append(ChangedFile(_text(status), _text(path)))
        return changed

    def call(
        self,
        arguments: Sequence[str],
        accepted: tuple[int, ...] = (0,),
        profile: Profile = Profile.LOCAL,
    ) -> subprocess.CompletedProcess[bytes]:
        """One git call on the tree; any exit but `accepted` and every failure to run it is a
        GitError with git's last error line, credentials redacted."""
        verb = next(argument for argument in arguments if not argument.startswith("-"))
        try:
            result = self.run(self.tree, arguments, profile)
        except subprocess.TimeoutExpired as error:
            raise GitError(f"`git {verb}` took longer than {error.timeout}s") from error
        except OSError as error:
            raise GitError(f"cannot run git ({error.strerror})") from error
        if result.returncode not in accepted:
            last = _error_line(_text(result.stderr) + _text(result.stdout))
            raise GitError(f"`git {verb}` exited {result.returncode}: {last}")
        return result


def fetch_origin(git: Git) -> Origin:
    """origin's `main`, fetched and pinned, and how HEAD stands to it; raises GitError."""
    url = git.fetch_url()
    sha = git.fetch_main()
    ahead, behind = git.divergence(sha)
    return Origin(url, sha, git.short(sha), git.short(), ahead, behind)


def without_credentials(text: str) -> str:
    """`text` with the whole userinfo of a `scheme://` URL redacted, every character up to the last
    `@` before the path, punctuation included: a user name may be a token as much as a password
    is (R3)."""
    return _URL_CREDENTIALS.sub(_REDACTED, text)


def _error_line(output: str) -> str:
    """git's last error line, else its last line; one line, credentials redacted (#98 rule 2)."""
    errors = [line for line in output.splitlines() if line.startswith(_ERROR_PREFIXES)]
    return without_credentials(compose.last_line("\n".join(errors) or output))


def _sha(answer: str, verb: str) -> str:
    if not answer or not all(digit in "0123456789abcdef" for digit in answer):
        raise GitError(f"`git {verb}` answered '{printable(answer)}'")
    return answer


def _nul_fields(output: bytes, verb: str) -> list[bytes]:
    """The fields of a `-z` answer that pairs each entry with one more field."""
    if not output:
        return []
    fields = output.removesuffix(b"\0").split(b"\0")
    if not output.endswith(b"\0") or len(fields) % 2:
        raise GitError(f"`git {verb}` answered no NUL-separated pairs")
    return fields


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
            raise GitError(f"`git status` answered '{printable(_text(entry))}'")
        paths.append(entry[3:])
        if {entry[0], entry[1]} & set(b"RC"):
            if (source := next(entries, None)) is None:
                raise GitError("`git status` named a rename without its source")
            paths.append(source)
    return [printable(_text(path)) for path in paths]


def live_checkout(home: Path) -> Path:
    return home / "git" / "edge"


def not_live_checkout(tree: Path, home: Path) -> str | None:
    """None when `tree` is the live checkout, else why not (#95 rule 2)."""
    if tree.resolve() != live_checkout(home).resolve():
        return f"this is {printable(str(tree))}"
    if (tree / ".git").is_file():
        return "a linked worktree, not the main checkout"
    if not (tree / ".git").is_dir():
        return "no git checkout"
    return None


def not_live_checkout_line(tree: Path, home: Path) -> str | None:
    """The refusal line for a `tree` that is not the live checkout; None when it is."""
    if reason := not_live_checkout(tree, home):
        return f"runs only from the live checkout {LIVE_CHECKOUT} ({reason})"
    return None


def not_on_main(git: Git) -> str | None:
    """None when HEAD is a `main` with a commit, else what it is (#95 rule 5); raises GitError."""
    branch = git.branch()
    if branch is None:
        return f"HEAD is detached at {git.short()}"
    if branch != MAIN:
        return f"this is branch {printable(branch)}"
    if not git.has_commit():
        return f"{MAIN} has no commit yet"
    return None


def not_clean_main(git: Git) -> list[str]:
    """Why `git` is not a born `main` with nothing changed, as lines; none when it is (#95 rules
    5, 6). A git that fails is a line too, so the caller refuses rather than guesses."""
    try:
        if reason := not_on_main(git):
            return [f"runs only on {MAIN} ({reason})"]
        changed = git.changed_paths()
    except GitError as error:
        return [f"cannot read the live checkout, {error}"]
    if not changed:
        return []
    return [
        "the live checkout has local changes, nothing changed; a change reaches it through "
        "a pull request and `git pull --ff-only`:",
        *(f"  {path}" for path in changed),
    ]
