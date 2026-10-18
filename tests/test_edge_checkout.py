"""The live checkout `$HOME/git/edge`: whether a tree is it, judged from paths alone, and whether it
is a clean `main`, asked of real git in `tmp_path` through `checkout`'s isolated runner.

Every tree is built under `tmp_path`. HOME and XDG_CONFIG_HOME are throwaway directories, so a
global git config is one a test wrote; GIT_CONFIG_GLOBAL is never set, because the runner drops
every `GIT_*` variable and a test setting one would prove nothing."""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from edge import checkout

IGNORED = ".env\n/dynamic/switch-*\n.venv/\n__pycache__/\n"
# githooks(5): every hook git may run, so a sentinel catches whichever one would.
HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
    "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout",
    "post-merge", "pre-push", "pre-receive", "update", "proc-receive", "post-receive",
    "post-update", "reference-transaction", "push-to-checkout", "pre-auto-gc", "post-rewrite",
    "sendemail-validate", "fsmonitor-watchman", "post-index-change",
)  # fmt: skip
AS_ROOT = os.geteuid() == 0
ROOT_REASON = "root reads a mode-000 file anyway, so the cell cannot show it is never opened"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME and XDG_CONFIG_HOME: no global config but the one a test writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def plain_git(tree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Git as the operator's shell runs it, global config, hooks and all (the positive control),
    but with no `GIT_*` of the shell that runs the tests."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(tree), *arguments],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, env=environment,
    )  # fmt: skip


def committed_main(tree: Path) -> Path:
    """`tree`'s files committed as the one commit of a fresh `main`, with this repository's
    ignores, an ignored `.env` and a repository identity; the fixture's own commit runs no hook."""
    tree.mkdir(parents=True, exist_ok=True)
    (tree / ".gitignore").write_text(IGNORED)
    (tree / ".env").touch()
    plain_git(tree, "init", "--quiet", "--initial-branch", checkout.MAIN)
    for key, value in (("user.name", "edge test"), ("user.email", "test@edge.invalid")):
        plain_git(tree, "config", key, value)
    plain_git(tree, "add", "--all")
    plain_git(tree, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--message", "init")
    return tree


@pytest.fixture
def main_tree(home: Path) -> Path:
    tree = home / "git" / "edge"
    tree.mkdir(parents=True)
    (tree / "traefik.yml").write_text("entryPoints: {}\n")
    (tree / "fleet.yaml").write_text("services: {}\n")
    return committed_main(tree)


@pytest.fixture
def git(main_tree: Path) -> checkout.Git:
    return checkout.Git(main_tree)


def tree(path: Path, *, linked: bool = False) -> Path:
    """A tree at `path` whose `.git` is a directory (a main worktree) or, when `linked`, a file (a
    linked worktree)."""
    path.mkdir(parents=True)
    if linked:
        (path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/issue-9-y\n")
    else:
        (path / ".git").mkdir()
    return path


def test_the_main_worktree_at_home_git_edge_is_the_live_checkout(home):
    """#79 cell 1: `$HOME/git/edge` with a `.git` directory proceeds."""
    assert checkout.not_live_checkout(tree(home / "git" / "edge"), home) is None


def test_a_linked_worktree_of_edge_elsewhere_is_another_copy(home, tmp_path):
    """#79 cell 2, #95 G2: a linked worktree is refused as the tree it is."""
    tree(home / "git" / "edge")
    worktree = tree(tmp_path / "edge-worktrees" / "issue-9-y", linked=True)

    assert checkout.not_live_checkout(worktree, home) == f"this is {worktree}"


def test_another_copy_is_named_as_the_tree_it_is(home, tmp_path):
    """#79 cell 3, #95 G2: a main worktree elsewhere, e.g. `/tmp/x/edge`, says `this is X`."""
    tree(home / "git" / "edge")
    copy = tree(tmp_path / "x" / "edge")

    assert checkout.not_live_checkout(copy, home) == f"this is {copy}"


def test_the_live_checkout_reached_through_a_symlinked_home_is_the_live_checkout(tmp_path):
    """#79 cell 4: a HOME that is a symlink to the home holding the code tree proceeds."""
    code_tree = tree(tmp_path / "real-home" / "git" / "edge")
    home = tmp_path / "linked-home"
    home.symlink_to(tmp_path / "real-home")

    assert checkout.not_live_checkout(code_tree, home) is None


def test_a_home_git_edge_symlink_to_the_code_tree_is_the_live_checkout(home, tmp_path):
    """#79 cell 4: `$HOME/git/edge` as a symlink that resolves to the code tree proceeds."""
    code_tree = tree(tmp_path / "disk" / "edge")
    (home / "git").mkdir(parents=True)
    (home / "git" / "edge").symlink_to(code_tree)

    assert checkout.not_live_checkout(code_tree, home) is None


def test_home_git_edge_as_a_linked_worktree_says_so(home):
    """#79 cell 5, #95 G3 (#34 finding 43): `$HOME/git/edge` whose `.git` is a file."""
    linked = tree(home / "git" / "edge", linked=True)

    assert checkout.not_live_checkout(linked, home) == "a linked worktree, not the main checkout"


def test_a_tree_without_git_says_it_is_no_git_checkout(home):
    """#79 rule 1, #95 G4: a bare copy of files at `$HOME/git/edge` is refused with its reason."""
    code_tree = home / "git" / "edge"
    code_tree.mkdir(parents=True)

    assert checkout.not_live_checkout(code_tree, home) == "no git checkout"


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda env: None, id="missing"),
        pytest.param(lambda env: env.mkdir(), id="a directory"),
        pytest.param(lambda env: env.symlink_to(env.parent / "gone"), id="a dangling symlink"),
    ],
)
def test_an_environment_file_that_is_no_file_is_missing(tmp_path, make):
    """#95 rule 3, G6: `.env` missing, a directory or a dangling symlink is no `.env`."""
    make(tmp_path / ".env")

    assert not checkout.has_environment_file(tmp_path)


def test_an_environment_file_reached_through_a_symlink_is_present(tmp_path):
    """#95 rule 3, G7: a symlink to a file is a `.env`."""
    (tmp_path / "real.env").touch()
    (tmp_path / ".env").symlink_to(tmp_path / "real.env")

    assert checkout.has_environment_file(tmp_path)


@pytest.mark.skipif(AS_ROOT, reason=ROOT_REASON)
def test_an_unreadable_environment_file_is_present_and_never_opened(tmp_path):
    """#95 rule 3, G7: a mode-000 `.env` passes; opening it would have failed."""
    environment_file = tmp_path / ".env"
    environment_file.touch(mode=0o000)
    environment_file.chmod(0o000)

    assert checkout.has_environment_file(tmp_path)


def test_a_born_main_is_on_main(git):
    """#95 rule 5: `main` with a commit passes."""
    assert checkout.not_on_main(git) is None


def test_another_branch_is_named(git, main_tree):
    """#95 rule 5, G12: another branch is named."""
    plain_git(main_tree, "switch", "--quiet", "--create", "feature/x")

    assert checkout.not_on_main(git) == "this is branch feature/x"


def test_a_detached_head_is_named_by_its_short_sha(git, main_tree):
    """#95 rule 5, G13: a detached HEAD is named by its short sha."""
    short = plain_git(main_tree, "rev-parse", "--short", "HEAD").stdout.strip()
    plain_git(main_tree, "switch", "--quiet", "--detach")

    assert checkout.not_on_main(git) == f"HEAD is detached at {short}"


def test_an_unborn_main_has_no_commit(home):
    """#95 rule 5, G14: `main` without a commit is refused."""
    unborn = home / "git" / "edge"
    unborn.mkdir(parents=True)
    plain_git(unborn, "init", "--quiet", "--initial-branch", checkout.MAIN)

    assert checkout.not_on_main(checkout.Git(unborn)) == "main has no commit yet"


def test_a_clean_main_has_no_changed_path(git):
    """#95 rule 6: a freshly committed main has no changed path, so the clean guard proceeds."""
    assert git.changed_paths() == []


def test_a_modified_tracked_file_is_named_and_left_unchanged(git, main_tree):
    """#95 rule 6, G15: a modified tracked file is named; its content stays."""
    (main_tree / "fleet.yaml").write_text("services: {edited: {}}\n")

    assert git.changed_paths() == ["fleet.yaml"]
    assert (main_tree / "fleet.yaml").read_text() == "services: {edited: {}}\n"


def test_a_staged_only_change_is_named(git, main_tree):
    """#95 rule 6, G16: a change staged and then reverted in the tree still differs from HEAD."""
    (main_tree / "fleet.yaml").write_text("services: {staged: {}}\n")
    plain_git(main_tree, "add", "fleet.yaml")
    (main_tree / "fleet.yaml").write_text("services: {}\n")

    assert git.changed_paths() == ["fleet.yaml"]


def test_an_untracked_file_is_named_down_to_the_file(git, main_tree):
    """#95 rule 6, G17: an untracked, non-ignored file is named by its own path."""
    (main_tree / "dynamic").mkdir()
    (main_tree / "dynamic" / "extra.yml").touch()

    assert git.changed_paths() == ["dynamic/extra.yml"]


def test_ignored_files_do_not_count(git, main_tree):
    """#95 rule 6, G18: `.env`, a barrier, `.venv/` and `__pycache__/` leave the tree clean."""
    (main_tree / ".env").write_text("EDGE_DOMAIN=probe.test\n")
    (main_tree / "dynamic").mkdir()
    (main_tree / "dynamic" / "switch-probe.yml").touch()
    for directory in (".venv", "__pycache__"):
        (main_tree / directory).mkdir()
        (main_tree / directory / "x").touch()

    assert git.changed_paths() == []


def test_traefik_yml_replaced_by_an_empty_directory_is_named(git, main_tree):
    """#95 rule 6, G19 (#34 finding 49): a tracked file replaced by a directory is dirty."""
    (main_tree / "traefik.yml").unlink()
    (main_tree / "traefik.yml").mkdir()

    assert git.changed_paths() == ["traefik.yml"]


def test_a_rename_names_both_paths(git, main_tree):
    """#95 rule 6: a staged rename names where it goes and where it came from."""
    plain_git(main_tree, "mv", "fleet.yaml", "renamed.yaml")

    assert git.changed_paths() == ["renamed.yaml", "fleet.yaml"]


def test_paths_with_control_characters_or_no_utf8_are_escaped_onto_one_line(git, main_tree):
    """#95 rule 6, G20: a newline, an ESC and a non-UTF-8 byte are printed escaped."""
    names = [b"new\nline", b"esc\x1b[31m", b"latin\xe9"]
    for name in names:
        (main_tree / os.fsdecode(name)).touch()

    assert sorted(git.changed_paths()) == ["esc\\x1b[31m", "latin\\xe9", "new\\nline"]


def test_five_hundred_changed_paths_are_all_named(git, main_tree):
    """#95 rule 6, G21: every path is named, however many."""
    for number in range(500):
        (main_tree / f"untracked-{number:03}").touch()

    assert git.changed_paths() == [f"untracked-{number:03}" for number in range(500)]


def test_the_guards_leave_the_index_untouched(git, main_tree):
    """#95 rule 6, G26: after a guarded run with a stat-dirty file, `.git/index`'s bytes and mtime
    are what they were; nothing refreshed it."""
    os.utime(main_tree / "fleet.yaml", ns=(1, 1))
    index = main_tree / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    checkout.not_on_main(git)
    git.changed_paths()
    after = (index.read_bytes(), index.stat().st_mtime_ns)

    assert after == before


def sentinel_script(path: Path, sentinel: Path, then: str = "exit 0") -> Path:
    """An executable that appends its own name to `sentinel` when anything runs it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$0" >> "{sentinel}"\n{then}\n')
    path.chmod(0o755)
    return path


def guarded(git: checkout.Git, main_tree: Path) -> None:
    """Everything the guards ask, plus a commit through the same runner, which would run hooks."""
    checkout.not_on_main(git)
    git.changed_paths()
    commit = ["commit", "--allow-empty", "--message", "x"]
    assert checkout.run_git(main_tree, commit, checkout.Profile.LOCAL).returncode == 0


@pytest.fixture
def sentinel(tmp_path: Path) -> Path:
    return tmp_path / "sentinel"


@pytest.mark.parametrize("scope", ["--global", "--local", ".git/hooks"])
def test_no_hook_runs_through_the_runner(git, main_tree, home, sentinel, scope):
    """#95 R2, G22: every hook as a sentinel, in a global or a repository `core.hooksPath` or in
    the checkout's own `.git/hooks`; plain git runs one (the positive control), the runner none."""
    hooks = main_tree / ".git" / "hooks" if scope == ".git/hooks" else home / "hooks"
    for name in HOOK_NAMES:
        sentinel_script(hooks / name, sentinel)
    if scope != ".git/hooks":
        plain_git(main_tree, "config", scope, "core.hooksPath", str(hooks))
    plain_git(main_tree, "commit", "--quiet", "--allow-empty", "--message", "control")
    assert sentinel.exists(), "positive control: plain git runs the hooks"
    sentinel.unlink()

    guarded(git, main_tree)

    assert not sentinel.exists()


@pytest.mark.parametrize("scope", ["--global", "--local"])
def test_no_fsmonitor_runs_through_the_runner(git, main_tree, tmp_path, sentinel, scope):
    """#95 R2, G23: `core.fsmonitor` as a sentinel in the global or the repository's config; plain
    `git status` runs it (the positive control), the runner never."""
    monitor = sentinel_script(tmp_path / "fsmonitor", sentinel, then="exit 1")
    plain_git(main_tree, "config", scope, "core.fsmonitor", str(monitor))
    plain_git(main_tree, "status", "--porcelain")
    assert sentinel.exists(), "positive control: plain git status runs the fsmonitor"
    sentinel.unlink()

    guarded(git, main_tree)

    assert not sentinel.exists()


def test_no_global_filter_runs_through_the_runner(git, main_tree, tmp_path, sentinel):
    """#95 R2, G27: a global `filter.x.clean` sentinel that `.gitattributes` in HEAD names, and a
    touched file; plain `git status` runs it (the positive control), the runner's status nothing."""
    (main_tree / ".gitattributes").write_text("fleet.yaml filter=x\n")
    plain_git(main_tree, "add", ".gitattributes")
    plain_git(main_tree, "commit", "--quiet", "--message", "attributes")
    clean = sentinel_script(tmp_path / "clean", sentinel, then="exec cat")
    plain_git(main_tree, "config", "--global", "filter.x.clean", str(clean))
    os.utime(main_tree / "fleet.yaml", ns=(1, 1))
    plain_git(main_tree, "--no-optional-locks", "status", "--porcelain")
    assert sentinel.exists(), "positive control: plain git status runs the clean filter"
    sentinel.unlink()
    os.utime(main_tree / "fleet.yaml", ns=(2, 2))

    guarded(git, main_tree)

    assert not sentinel.exists()


def test_inherited_git_variables_do_not_redirect_the_guards(git, main_tree, tmp_path, monkeypatch):
    """#95 rule 7, G24: GIT_* exported towards a dirty repository on another branch; the guards
    still judge the live checkout, a clean `main`."""
    elsewhere = committed_main(tmp_path / "elsewhere")
    plain_git(elsewhere, "switch", "--quiet", "--create", "other")
    (elsewhere / "dirty").touch()
    exported = {
        "GIT_DIR": str(elsewhere / ".git"),
        "GIT_WORK_TREE": str(elsewhere),
        "GIT_INDEX_FILE": str(elsewhere / ".git" / "index"),
        "GIT_CONFIG_PARAMETERS": "'core.hookspath'='/elsewhere'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "status.showUntrackedFiles",
        "GIT_CONFIG_VALUE_0": "no",
        "GIT_EXEC_PATH": str(tmp_path / "no-exec-path"),
        "GIT_PAGER": "false",
    }
    for name, value in exported.items():
        monkeypatch.setenv(name, value)
    (main_tree / "untracked").touch()

    assert checkout.not_on_main(git) is None
    assert git.changed_paths() == ["untracked"]


def test_the_environment_drops_git_variables_and_never_prompts():
    """#95 rule 7: no `GIT_*` survives; prompts are off; only the local profile drops the global
    and system config, the transfer profile keeps them for the credential helper."""
    inherited = {"PATH": "/bin", "GIT_DIR": "/x", "GIT_SSH_COMMAND": "evil", "HOME": "/h"}
    never_prompt = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}

    local = checkout.git_environment(inherited, checkout.Profile.LOCAL)
    transfer = checkout.git_environment(inherited, checkout.Profile.TRANSFER)

    assert transfer == {"PATH": "/bin", "HOME": "/h", **never_prompt}
    assert local == {**transfer, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def answering(stdout: bytes, returncode: int = 0) -> checkout.Runner:
    def run(tree, arguments, _profile):
        return subprocess.CompletedProcess(arguments, returncode, stdout, b"")

    return run


def raising(error: Exception) -> checkout.Runner:
    def run(_tree, _arguments, _profile):
        raise error

    return run


@pytest.mark.parametrize(
    ("runner", "ask", "line"),
    [
        pytest.param(answering(b"HEAD\n"), checkout.not_on_main,
                     "`git symbolic-ref` answered 'HEAD'", id="symbolic-ref garbage"),
        pytest.param(answering(b"?? x"), checkout.Git.changed_paths,
                     "`git status` answered no porcelain", id="status without NUL"),
        pytest.param(answering(b"garbage\0"), checkout.Git.changed_paths,
                     "`git status` answered 'garbage'", id="status garbage"),
        pytest.param(answering(b"\x1bHEAD\n"), checkout.not_on_main,
                     "`git symbolic-ref` answered '\\x1bHEAD'", id="symbolic-ref escaped once"),
        pytest.param(answering(b"\x1bab\n"), checkout.Git.short_head,
                     "`git rev-parse` answered '\\x1bab'", id="rev-parse escaped once"),
        pytest.param(answering(b"\x1bgarbage\0"), checkout.Git.changed_paths,
                     "`git status` answered '\\x1bgarbage'", id="status escaped once"),
        pytest.param(answering(b"fatal: not a repo\n", 128), checkout.Git.changed_paths,
                     "`git status` exited 128: fatal: not a repo", id="git fails"),
        pytest.param(raising(subprocess.TimeoutExpired("git", 30)), checkout.not_on_main,
                     "`git symbolic-ref` took longer than 30s", id="timeout"),
    ],
)  # fmt: skip
def test_a_git_that_answers_nonsense_or_hangs_is_one_line(
    main_tree, runner: checkout.Runner, ask: Callable, line: str
):
    """#95 G25: garbage, a failure or a timeout is a GitError of one line."""
    git = checkout.Git(main_tree, runner)

    with pytest.raises(checkout.GitError) as raised:
        ask(git)

    assert str(raised.value) == line


def test_a_missing_git_is_one_line(main_tree, tmp_path, monkeypatch):
    """#95 G25: no git on PATH is a GitError of one line."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    git = checkout.Git(main_tree)

    with pytest.raises(checkout.GitError, match=r"^cannot run git \(No such file or directory\)$"):
        checkout.not_on_main(git)
