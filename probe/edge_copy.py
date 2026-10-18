"""What the live-stack probes share: a copy of the repository with its own venv, and `edge` run in
it, at a pty or without a terminal (#52 proof 10, #60 proof 11, #61 probe c).

A probe works in the copy so that it flips `fleet.yaml` and lets `edge` write `dynamic/` and the
state under its throwaway HOME without ever touching the checkout it runs from. The copy is the
live checkout of that HOME, `<HOME>/git/edge` with a `.git` directory, so that its `edge apply`
runs there and never against the operator's HOME (#79 rule 3). It is a clean `main` with a bare
remote `<scratch>/origin.git` and an empty, ignored `.env`, as `edge apply --closed` requires; a
probe publishes every edit it makes there (commit and push) before it applies (#95 rule 10). To
put the copy behind origin, as a pull request merged elsewhere does, a probe advances origin from
a work clone `<scratch>/work` (#98 P1). A probe adds this directory and the repository's `src` to
`sys.path` before importing it, which also works when the probe is loaded by path.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from edge import checkout, compose

APPLY_SECONDS = 600
DISCARD_OUTPUT = "/dev/null"
REMOTE = "origin.git"
WORK_CLONE = "work"
# The probe's own identity, in the copy's config: its git reads no global one (checkout's local
# profile).
PROBE_IDENTITY = {"user.name": "edge probe", "user.email": "probe@edge.invalid"}


class ProbeFailed(Exception):
    """A proof did not hold; the message names what was expected and what came back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailed(message)


def repository_copy(repository: Path, home: Path, scratch: Path) -> Path:
    """The files of `repository` that git tracks or would track, as the live checkout of `home`: a
    `main` pushed to the bare remote `<scratch>/origin.git`, with an empty ignored `.env` and its
    own venv from `uv sync`. Copying again over an existing copy publishes what differs."""
    copy = checkout.live_checkout(home)
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, cwd=repository, check=True, timeout=APPLY_SECONDS,
    )  # fmt: skip
    for name in filter(None, listing.stdout.decode().split("\0")):
        if (repository / name).is_file():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / name, copy / name)
    if not (copy / ".git").is_dir():
        remote = scratch / REMOTE
        git(scratch, "init", "--quiet", "--bare", "--initial-branch", checkout.MAIN, str(remote))
        git(copy, "init", "--quiet", "--initial-branch", checkout.MAIN)
        git(copy, "remote", "add", "origin", str(remote))
        for key, value in PROBE_IDENTITY.items():
            git(copy, "config", key, value)
    (copy / checkout.ENVIRONMENT_FILE).touch()
    synced = subprocess.run(
        ["uv", "sync", "--quiet"], capture_output=True, cwd=copy, check=False,
        timeout=APPLY_SECONDS,
    )  # fmt: skip
    if synced.returncode != 0:
        tail = synced.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeFailed(f"uv sync exited {synced.returncode}: {tail[-1] if tail else ''}")
    publish(copy, "the lane's files")
    return copy


def publish(copy: Path, message: str) -> None:
    """Commit every change of the copy and push it to its remote, as a merged pull request and its
    pull would bring it; the copy is a clean `main` again."""
    git(copy, "add", "--all")
    git(copy, "commit", "--quiet", "--allow-empty", "--message", message)
    git(copy, "push", "--quiet", "origin", checkout.MAIN)


def advance_origin(scratch: Path, message: str, appended: Mapping[str, str]) -> str:
    """One commit pushed to the bare remote from the work clone `<scratch>/work`, appending each
    text to its file there, so the copy is behind origin's `main` until it merges; the commit's
    short sha."""
    work = scratch / WORK_CLONE
    if (work / ".git").is_dir():
        git(work, "pull", "--quiet", "--ff-only", "origin", checkout.MAIN)
    else:
        git(scratch, "clone", "--quiet", str(scratch / REMOTE), str(work))
        for key, value in PROBE_IDENTITY.items():
            git(work, "config", key, value)
    for name, text in appended.items():
        with (work / name).open("a") as file:
            file.write(text)
    publish(work, message)
    return checkout.Git(work).short()


def git(tree: Path, *arguments: str) -> None:
    """Git through edge's own isolated runner: no hook, no global config, no prompt."""
    result = checkout.run_git(tree, arguments, checkout.Profile.LOCAL)
    tail = compose.last_line(result.stderr.decode("utf-8", "backslashreplace"))
    expect(result.returncode == 0, f"git {arguments[0]} exited {result.returncode}: {tail}")


def run_edge(
    copy: Path,
    environment: Mapping[str, str],
    *arguments: str,
    terminal: bool,
    exit_code: int = 0,
    typed: bytes = b"yes\n",
) -> str:
    """`edge <arguments>` in the copy: at a pty with the operator typing `typed`, or with stdin and
    stdout that are no terminal. The whitespace-normalised output of a run that exits
    `exit_code`."""
    command = [str(copy / ".venv" / "bin" / "edge"), *arguments]
    if terminal:
        result = subprocess.run(
            ["script", "-qec", shlex.join(command), DISCARD_OUTPUT],
            input=typed, capture_output=True, env=environment, cwd=copy, check=False,
            timeout=APPLY_SECONDS,
        )  # fmt: skip
    else:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, env=environment,
            cwd=copy, check=False, timeout=APPLY_SECONDS,
        )  # fmt: skip
    output = " ".join((result.stdout + result.stderr).decode(errors="replace").split())
    expect(
        result.returncode == exit_code,
        f"edge {' '.join(arguments)} exited {result.returncode}, wanted {exit_code}: "
        f"{output[-200:]}",
    )
    return output
