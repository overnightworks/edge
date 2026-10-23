"""What the live-stack probes share: a copy of the repository with its own venv, and `edge` run in
it, at a pty or without a terminal (#52 proof 10, #60 proof 11, #61 probe c).

A probe works in the copy so that it flips `fleet.yaml` and lets `edge` write `dynamic/` and the
state under its throwaway HOME without ever touching the checkout it runs from. The copy is the
live checkout of that HOME, `<HOME>/git/edge` with a `.git` directory, so that its `edge apply`
runs there and never against the operator's HOME (#79 rule 3). It is a clean `main` with a bare
remote `<scratch>/origin.git`, as `edge apply --closed` requires (its ignored `.env` is written by
that apply, #136); a probe publishes every edit it makes there (commit and push) before it
applies (#95 rule 10). To put the copy behind origin, as a pull request merged elsewhere does, a
probe advances origin from a work clone `<scratch>/work` (#98 P1). A probe adds this directory and
the repository's `src` to `sys.path` before importing it, which also works when the probe is
loaded by path.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from edge import checkout, compose

APPLY_SECONDS = 600
HAL_OK, HAL_FAILURE = 0, 3  # hal's exits: 3 is every outcome of the stack that is not "done"
DISCARD_OUTPUT = "/dev/null"
REMOTE = "origin.git"
WORK_CLONE = "work"
# The probe's own identity, in the copy's config: its git reads no global one (checkout's local
# profile).
PROBE_IDENTITY = {"user.name": "edge probe", "user.email": "probe@edge.invalid"}
# The fixed network names of compose.yaml (`name: hal_<suffix>`) that apps join as external
# networks; they survive the project's rename, so a probe must never create or remove one.
LIVE_NETWORK_PREFIX = "hal_"
LIVE_NETWORK_NAME = re.compile(
    rf"^(\s+name:\s+){re.escape(LIVE_NETWORK_PREFIX)}(\w+)\s*$", re.MULTILINE
)
GUARDED_COMPOSE_VERBS = {"up", "run", "down"}  # what creates or removes networks


class ProbeFailed(Exception):
    """A proof did not hold; the message names what was expected and what came back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailed(message)


def namespaced_compose(text: str, project: str) -> str:
    """`text` of compose.yaml with every fixed network name `hal_<suffix>` moved to
    `<project>_<suffix>`, so a probe's stack shares no network with the live one. Refuses a text
    with no such name, and one in which a `hal_` name remains."""
    namespaced, renamed = LIVE_NETWORK_NAME.subn(rf"\g<1>{project}_\g<2>", text)
    expect(renamed > 0, "compose.yaml names no fixed hal_ network to move")
    remaining = re.findall(rf"name:\s+{LIVE_NETWORK_PREFIX}\S*", namespaced)
    expect(not remaining, f"a hal_ network name remains after the rewrite: {remaining}")
    return namespaced


def expect_project_networks(resolved: dict, project: str) -> None:
    """Raises `ProbeFailed` when a network of a resolved compose configuration (`config --format
    json`) is not named `<project>_...`: a call that resolves one would touch a live network."""
    foreign = {
        key: network["name"]
        for key, network in (resolved.get("networks") or {}).items()
        if not network["name"].startswith(f"{project}_")
    }
    expect(not foreign, f"networks outside the project {project}: {foreign}")


def copy_compose_command(copy: Path) -> list[str]:
    """Compose as edge calls it in `copy`: its project directory and file, every profile."""
    return [
        "docker", "compose", "--project-directory", str(copy), "--file",
        str(copy / "compose.yaml"), "--profile", "*",
    ]  # fmt: skip


def daemon_subnets() -> list[str]:
    """The subnets of every network the daemon has."""
    listing = subprocess.run(
        ["docker", "network", "ls", "-q"], capture_output=True, text=True, check=True
    ).stdout.split()
    if not listing:
        return []
    inspected = subprocess.run(
        ["docker", "network", "inspect", *listing], capture_output=True, text=True, check=True
    ).stdout
    return [
        config["Subnet"]
        for network in json.loads(inspected)
        for config in (network.get("IPAM") or {}).get("Config") or []
        if config.get("Subnet")
    ]


def free_subnet(candidates: Sequence[str], occupied: Sequence[str]) -> ipaddress.IPv4Network:
    """The first candidate /24 that overlaps none of the `occupied` subnets (`daemon_subnets()`)."""
    used = [ipaddress.ip_network(subnet) for subnet in occupied]
    for candidate in map(ipaddress.IPv4Network, candidates):
        if not any(candidate.overlaps(other) for other in used if other.version == 4):
            return candidate
    raise ProbeFailed(f"no free /24 among {candidates[0]} ... {candidates[-1]}")


class NetworkGuard:
    """Checks, before a compose call that starts or removes something, that what the call's files
    and profiles resolve names only networks of the probe's project (P26). `run` returns the
    stdout of a command. A command is resolved once unless `once` is false, for a file that
    changes between calls."""

    def __init__(self, project: str, run: Callable[[list[str]], str | bytes]) -> None:
        self.project, self.run = project, run
        self.checked: set[tuple[str, ...]] = set()
        self.live_before = live_networks()

    def expect(self, prefix: list[str], *, once: bool = True) -> None:
        key = tuple(prefix)
        if not once or key not in self.checked:
            expect_project_networks(
                json.loads(self.run([*prefix, "config", "--format", "json"])), self.project
            )
            self.checked.add(key)

    def expect_clean_teardown(self) -> None:
        """No network of the project is left, and the live `hal_*` networks are the same as when
        the guard was made."""
        left = subprocess.run(
            ["docker", "network", "ls", "-q", "--filter", f"name={self.project}_"],
            capture_output=True, check=True, text=True, timeout=APPLY_SECONDS,
        ).stdout.split()  # fmt: skip
        expect(not left, f"the teardown left networks {left}")
        after = live_networks()
        expect(after == self.live_before, f"the hal_ networks changed: {self.live_before ^ after}")


def live_networks() -> set[str]:
    """The names of the fixed `hal_*` networks the daemon has now."""
    listing = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        capture_output=True, check=True, text=True, timeout=APPLY_SECONDS,
    )  # fmt: skip
    return {name for name in listing.stdout.split() if name.startswith(LIVE_NETWORK_PREFIX)}


def repository_copy(
    repository: Path, home: Path, scratch: Path, project: str | None = None
) -> Path:
    """The files of `repository` that git tracks or would track, as the live checkout of `home`: a
    `main` pushed to the bare remote `<scratch>/origin.git`, with its ignored `.env` written by the
    first `edge apply --closed` and its own venv from `uv sync`. With a `project`, the copy's
    compose.yaml names its fixed networks `<project>_...` (namespaced_compose), so the copy's
    `edge apply` touches no live network.
    Copying again over an existing copy publishes what differs."""
    copy = checkout.live_checkout(home)
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, cwd=repository, check=True, timeout=APPLY_SECONDS,
    )  # fmt: skip
    for name in filter(None, listing.stdout.decode().split("\0")):
        if (repository / name).is_file():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / name, copy / name)
    if project:
        compose_file = copy / "compose.yaml"
        compose_file.write_text(namespaced_compose(compose_file.read_text(), project))
    if not (copy / ".git").is_dir():
        remote = scratch / REMOTE
        git(scratch, "init", "--quiet", "--bare", "--initial-branch", checkout.MAIN, str(remote))
        git(copy, "init", "--quiet", "--initial-branch", checkout.MAIN)
        git(copy, "remote", "add", "origin", str(remote))
        for key, value in PROBE_IDENTITY.items():
            git(copy, "config", key, value)
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
