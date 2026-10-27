"""The pure side of the login command (#457, `cells_12b.py`): reading the login, the exact-secret
gate over its output, the failure type and runner that print nothing but verdicts, the preflight
of the resolved compose configuration and the S00 verdicts (agent-runtime#440, contract line 1).

Nothing here prints a path, a subprocess's output or an exception's text: every line is a verdict,
a count, a role name, a docker verb with an exit code, or a fixed reason from `Refusal`."""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TextIO

from census_client import Mount
from egress import ProbeFailed
from image import ProbeFailed as ImageProbeFailed
from live_smoke import redacted
from tree_census import Skip

TOKEN_LIMIT = 16 * 1024
TOKEN_FILE = "token"
SKIP_REASONS = frozenset(skip.value for skip in Skip)


class Refusal(StrEnum):
    """The reasons the command refuses to start; each names the flag or the kind of file, never a
    path."""

    NOT_A_DIRECTORY = "--login-directory is not an existing directory"
    NO_TOKEN_FILE = "--login-directory holds no regular file token"
    TOKEN_UNREADABLE = "--login-directory: the token file cannot be read as text"
    TOKEN_TOO_LARGE = "--login-directory: the token file is too large"
    TOKEN_EMPTY = "--login-directory: the token file is empty"
    LOGIN_BIND = "the resolved configuration has no single read-only bind of the login directory"
    CONFIGURATION_BIND = (
        "the resolved configuration has no single read-only bind of the scratch service.toml"
    )
    WRITABLE_ROOT = "the service's root file system is not read-only"
    LOG_DRIVER = "a started service does not log through the local driver"


class LoginFailed(ProbeFailed):
    """A failure whose message is safe to print: a fixed `Refusal` or `<verb> exited <code>`."""


@dataclass(frozen=True)
class Login:
    directory: Path  # resolved once; the only form that is checked, read and bound
    token: str = field(repr=False)


def read_login(text: str) -> Login:
    """The login directory at `text`, resolved once, with its token read into memory for the
    gate. The directory is never listed and the token is never printed."""
    try:
        directory = Path(text).resolve(strict=True)
    except (OSError, RuntimeError) as problem:
        raise LoginFailed(Refusal.NOT_A_DIRECTORY) from problem
    if not directory.is_dir():
        raise LoginFailed(Refusal.NOT_A_DIRECTORY)
    path = directory / TOKEN_FILE
    try:
        regular = stat.S_ISREG(os.lstat(path).st_mode)
    except OSError as problem:
        raise LoginFailed(Refusal.NO_TOKEN_FILE) from problem
    if not regular:
        raise LoginFailed(Refusal.NO_TOKEN_FILE)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as handle:
            raw = handle.read(TOKEN_LIMIT + 1)
        token = raw.decode("utf-8").strip()  # as tree_census.py and live_smoke.py strip it
    except (OSError, UnicodeDecodeError) as problem:
        raise LoginFailed(Refusal.TOKEN_UNREADABLE) from problem
    if len(raw) > TOKEN_LIMIT:
        raise LoginFailed(Refusal.TOKEN_TOO_LARGE)
    if not token:
        raise LoginFailed(Refusal.TOKEN_EMPTY)
    return Login(directory, token)


class Gate:
    """The exact-secret list and how many lines it had to change."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self.secrets = [secret for secret in secrets if secret]
        self.hits = 0

    def add(self, secret: str) -> None:
        if secret:
            self.secrets.append(secret)

    def clean(self, text: str) -> str:
        cleaned = redacted(text, self.secrets)
        self.hits += cleaned != text
        return cleaned


class GatedStream:
    """Passes completed lines through the gate; the unterminated rest waits for `flush`."""

    def __init__(self, gate: Gate, stream: TextIO) -> None:
        self.gate = gate
        self.stream = stream
        self.pending = ""

    def write(self, text: str) -> int:
        *lines, self.pending = (self.pending + text).split("\n")
        for line in lines:
            self.stream.write(self.gate.clean(line) + "\n")
        return len(text)

    def flush(self) -> None:
        if self.pending:
            self.stream.write(self.gate.clean(self.pending))
            self.pending = ""
        self.stream.flush()

    def __getattr__(self, name: str):
        return getattr(self.stream, name)


@contextmanager
def gating(secrets: Iterable[str]) -> Iterator[Gate]:
    """`sys.stdout` and `sys.stderr` behind one gate for the command's lifetime."""
    gate = Gate(secrets)
    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = (GatedStream(gate, stream) for stream in original)
    try:
        yield gate
    finally:
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        sys.stdout, sys.stderr = original


COMPOSE_OPTIONS_WITH_ARGUMENT = frozenset({"-p", "--project-directory", "--file", "--profile"})


def verb(command: Sequence[str]) -> str:
    """The docker subcommand of `command` (compose's own for `docker compose`): the only part of
    an argv that is printed."""
    if command[:1] != ["docker"]:
        return Path(command[0]).name
    rest = list(command[1:])
    if rest[:1] == ["compose"]:
        rest = rest[1:]
        skip = False
        for word in rest:
            if skip:
                skip = False
            elif word in COMPOSE_OPTIONS_WITH_ARGUMENT:
                skip = True
            elif not word.startswith("-"):
                return f"compose {word}"
        return "compose"
    return next((word for word in rest if not word.startswith("-")), "docker")


def failed_command(command: Sequence[str], code: int) -> LoginFailed:
    return LoginFailed(f"{verb(command)} exited {code}")


def describe(stage: str, problem: BaseException) -> str:
    """What is printed of a failure: the message only of a `LoginFailed`."""
    if isinstance(problem, LoginFailed):
        return f"{stage} FAILED: {problem}"
    if isinstance(problem, (ProbeFailed, ImageProbeFailed, subprocess.TimeoutExpired)):
        return f"{stage} FAILED (reason withheld)"
    return f"{stage} FAILED ({type(problem).__name__})"


def guarded(stage: str, step: Callable[[], object], failed: list[str]) -> bool:
    """Runs `step`; a failure is printed as `describe` says and recorded in `failed`."""
    try:
        step()
    except (
        Exception
    ) as problem:  # an exception's text may carry a secret, so only `describe` speaks of it
        print(describe(stage, problem), flush=True)
        failed.append(stage)
        return False
    return True


class LoginRun(Protocol):
    held: list[str]
    failed: list[str]
    not_driven: list[str]

    def __enter__(self) -> object: ...
    def __exit__(self, *_: object) -> None: ...
    def drive(self) -> None: ...


def run_login(open_probe: Callable[[], LoginRun]) -> int:
    """Open, drive and tear down the probe; exit 0 only when every cell held. A stage that fails
    prints `<stage> FAILED ...` and the teardown still runs once the stack came up."""
    stopped: list[str] = []
    probe: list[LoginRun] = []

    def stage(name: str, step: Callable[[], object]) -> bool:
        try:
            return guarded(name, step, stopped)
        except KeyboardInterrupt:
            print("interrupted", flush=True)
            stopped.append(name)
            return False

    if not stage("setup", lambda: probe.append(open_probe())):
        print("PROBE FAILED: setup", flush=True)
        return 1
    run = probe[0]
    entered = stage("setup", run.__enter__)  # a failed enter tears itself down
    if entered:
        stage("drive", run.drive)
        signal.alarm(0)  # an alarm or a SIGTERM inside the teardown would leave the stack standing
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stage("teardown", run.__exit__)
    if stopped or run.failed or run.not_driven:
        problems = [
            *(f"failed {name}" for name in (*stopped, *run.failed)),
            *(f"not driven {name}" for name in run.not_driven),
        ]
        print(f"PROBE FAILED: held {', '.join(run.held)}; {'; '.join(problems)}", flush=True)
        return 1
    print(f"PROBE PASSED: held {', '.join(run.held)}", flush=True)
    return 0


def stop_on_signal(*_: object) -> None:
    raise KeyboardInterrupt


def start_time_bound(seconds: int) -> None:
    """The alarm and SIGTERM unwind the command as a Ctrl-C does, so its teardown runs."""
    signal.signal(signal.SIGALRM, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.alarm(seconds)


@contextmanager
def probe_lock(path: Path, announce: Callable[[], None]) -> Iterator[None]:
    """Holds the probe lock exclusively; `announce` runs once when another holder makes it wait.
    The lock file is inherited by no child."""
    with open(path, "a", encoding="utf-8") as handle:  # NOSONAR(S5443) the shared probe lock
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            announce()
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def one_read_only_bind(volumes: Sequence[Mapping], target: str, source: Path) -> bool:
    found = [volume for volume in volumes if volume.get("target") == target]
    return (
        len(found) == 1
        and found[0].get("type") == "bind"
        and Path(found[0].get("source", "")) == source
        and found[0].get("read_only") is True
    )


def check_resolved(
    resolved: Mapping,
    *,
    service: str,
    started: Sequence[str],
    login: tuple[str, Path],
    configuration: tuple[str, Path],
) -> None:
    """The resolved `compose config --format json` holds exactly what the command binds: one
    read-only bind each of the login directory and the scratch service.toml (as (target, source)
    pairs), a read-only root, and the local log driver on every started service. Otherwise the
    command refuses and starts nothing."""
    services = resolved["services"]
    volumes = services[service].get("volumes", [])
    if not one_read_only_bind(volumes, *login):
        raise LoginFailed(Refusal.LOGIN_BIND)
    if not one_read_only_bind(volumes, *configuration):
        raise LoginFailed(Refusal.CONFIGURATION_BIND)
    if services[service].get("read_only") is not True:
        raise LoginFailed(Refusal.WRITABLE_ROOT)
    if any((services[name].get("logging") or {}).get("driver") != "local" for name in started):
        raise LoginFailed(Refusal.LOG_DRIVER)


def mount_line(role: str, mount: Mount) -> str:
    """One census mount as a role and counts; a skip reason prints only from the census's closed
    vocabulary."""
    reasons = [
        (reason if reason in SKIP_REASONS else "other", count) for reason, count in mount.skips
    ]
    skips = ", ".join(f"{reason} {count}" for reason, count in reasons) or "none"
    if mount.listed:
        listing = f"listed: {len(mount.listings)} entries"
    else:
        listing = f"not listed ({', '.join(reason for reason, _ in reasons)})"
    return f"S00 {role}: {listing}; skips: {skips}; {mount.token_files} files hold the token"


def unlisted_roots(mounts: Mapping[str, Mount]) -> int:
    """How many directory mounts the census could not list at the root; a file mount lists
    nothing by nature."""
    return sum(
        not mount.listed and Skip.FILE_MOUNT.value not in {reason for reason, _ in mount.skips}
        for mount in mounts.values()
    )


def token_files(mounts: Mapping[str, Mount]) -> int:
    return sum(mount.token_files for mount in mounts.values())
