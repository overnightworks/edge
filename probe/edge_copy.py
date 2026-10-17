"""What the live-stack probes share: a copy of the repository with its own venv, and `edge` run in
it, at a pty or without a terminal (#52 proof 10, #60 proof 11, #61 probe c).

A probe works in the copy so that it flips `fleet.yaml` and lets `edge` write `dynamic/` and the
state under its throwaway HOME without ever touching the checkout it runs from. The copy is the
live checkout of that HOME, `<HOME>/git/edge` with a `.git` directory, so that its `edge apply`
runs there and never against the operator's HOME (#79 rule 3). A probe adds this directory and the
repository's `src` to `sys.path` before importing it, which also works when the probe is loaded by
path.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from edge import checkout

APPLY_SECONDS = 600
DISCARD_OUTPUT = "/dev/null"


class ProbeFailed(Exception):
    """A proof did not hold; the message names what was expected and what came back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailed(message)


def repository_copy(repository: Path, home: Path) -> Path:
    """The files of `repository` that git tracks or would track, as the live checkout of `home`: a
    fresh git repository with its own venv from `uv sync`."""
    copy = checkout.live_checkout(home)
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, cwd=repository, check=True, timeout=APPLY_SECONDS,
    )  # fmt: skip
    for name in filter(None, listing.stdout.decode().split("\0")):
        if (repository / name).is_file():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / name, copy / name)
    subprocess.run(
        ["git", "init", "--quiet"], capture_output=True, cwd=copy, check=True,
        timeout=APPLY_SECONDS,
    )  # fmt: skip
    synced = subprocess.run(
        ["uv", "sync", "--quiet"], capture_output=True, cwd=copy, check=False,
        timeout=APPLY_SECONDS,
    )  # fmt: skip
    if synced.returncode != 0:
        tail = synced.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeFailed(f"uv sync exited {synced.returncode}: {tail[-1] if tail else ''}")
    return copy


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
