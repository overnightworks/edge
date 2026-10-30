"""The pure side of the login command (#457, `cells_12b.py`): reading the login, the exact-secret
gate over its output, the failure type and runner that print nothing but verdicts, the preflight
of the resolved compose configuration and the S00 verdicts (agent-runtime#440, contract line 1).

The B9 verdicts (agent-runtime#440, R2) and the waits of its drive are here too, on injected
clocks.

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
from typing import NamedTuple, Protocol, TextIO

from census_client import Mount, Watched
from egress import ProbeFailed
from fake_app import HELD_LINE, RELEASED_PREFIX
from image import ProbeFailed as ImageProbeFailed
from live_smoke import redacted
from run_client import (
    CLIENT_LINE,
    NO_LOSS,
    OTHER,
    REFUSED,
    RUN_STARTED,
    STATUS_SECONDS,
)
from service_log import LogClasses
from tree_census import Skip

TOKEN_LIMIT = 16 * 1024
TOKEN_FILE = "token"  # noqa: S105 - a name or message text and no credential
ROOTS_CELL = "S00 (roots)"
TOKEN_CELL = "S00 (token)"  # noqa: S105 - a name or message text and no credential
SKIP_REASONS = frozenset(skip.value for skip in Skip)
START_SECONDS = 30  # from starting the client to its `event run_started`: only the admission
R2_CALL_SECONDS = 90  # from `event run_started` to a held call; the paid turn is bounded by it
CALL_POLL_SECONDS = 0.5
RELEASE_SECONDS = 5  # above the service's 2.0 s settle of the forwards, with room for the log read
WATCH_SECONDS = 120  # R2_CALL_SECONDS + the hold + the reap (102.1 s), below the census's 240 s
CLIENT_READ_SECONDS = STATUS_SECONDS + 10  # after it the host kills the client
B9_CELLS = ("B9 (a)", "B9 (b)", "B9 (c)", "B9 (d)", "B9 (e)")


class Refusal(StrEnum):
    """The reasons the command refuses to start; each names the flag or the kind of file, never a
    path."""

    NOT_A_DIRECTORY = "--login-directory is not an existing directory"
    NO_TOKEN_FILE = "--login-directory holds no regular file token"  # noqa: S105 - a name or message text and no credential
    TOKEN_UNREADABLE = "--login-directory: the token file cannot be read as text"  # noqa: S105 - a name or message text and no credential
    TOKEN_TOO_LARGE = "--login-directory: the token file is too large"  # noqa: S105 - a name or message text and no credential
    TOKEN_EMPTY = "--login-directory: the token file is empty"  # noqa: S105 - a name or message text and no credential
    LOGIN_BIND = "the resolved configuration has no single read-only bind of the login directory"
    CONFIGURATION_BIND = (
        "the resolved configuration has no single read-only bind of the scratch service.toml"
    )
    WRITABLE_ROOT = "the service's root file system is not read-only"
    LOG_DRIVER = "a started service does not log through the local driver"
    SCRATCH_IN_LOGIN = "the scratch directory would lie inside the login directory"
    LOCK_IN_LOGIN = "the probe lock would be created inside the login directory"


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
    # an exception's text may carry a secret, so only `describe` speaks of it
    except Exception as problem:
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
        try:
            stage("drive", run.drive)
        finally:  # a signal between the drive and here must not skip the teardown
            begin_teardown()
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


def begin_teardown() -> None:
    """From here an alarm or a SIGTERM cannot abort the teardown and leave the stack, and with it
    the operator's login bind, standing."""
    signal.alarm(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def scratch_parent(login: Login, lock: Path, environment: Mapping[str, str]) -> Path:
    """Where the scratch directory is made: TMPDIR, else /tmp. Refused when that, or the directory
    the probe lock resolves to, lies in the login directory, because making either there would
    write into it."""
    parent = Path(environment.get("TMPDIR") or "/tmp").resolve()  # NOSONAR(S5443) default scratch  # noqa: S108 - the default scratch parent when TMPDIR is unset
    if login.directory in (parent, *parent.parents):
        raise LoginFailed(Refusal.SCRATCH_IN_LOGIN)
    if login.directory in lock.resolve().parents:
        raise LoginFailed(Refusal.LOCK_IN_LOGIN)
    return parent


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


# A file mount is no gap: Docker's rw file binds are root-owned 0644, unwritable for the login uid.
UNREAD_REASONS = frozenset(
    skip.value for skip in (Skip.REPLACED, Skip.REFUSED, Skip.TOO_DEEP, Skip.VANISHED)
)


def unread_entries(mounts: Mapping[str, Mount]) -> int:
    """How many entries the token count could not read; zero hits over those prove nothing."""
    return sum(
        count
        for mount in mounts.values()
        for reason, count in mount.skips
        if reason in UNREAD_REASONS
    )


class Outcome(StrEnum):
    HELD = "held"
    FAILED = "FAILED"
    NOT_DRIVEN = "NOT DRIVEN"


class Verdict(NamedTuple):
    cell: str
    outcome: Outcome
    evidence: str  # counts only


def s00_verdicts(mounts: Mapping[str, Mount]) -> tuple[Verdict, Verdict]:
    """edge's two frame checks over the S00 census: every directory root was listed, and no file
    outside the login directory holds the token. A token count over entries it could not read
    is not driven rather than held, and so is a census that showed no mount at all."""
    if not mounts:
        reason = "0 writable mounts were shown"
        return (
            Verdict(ROOTS_CELL, Outcome.NOT_DRIVEN, reason),
            Verdict(TOKEN_CELL, Outcome.NOT_DRIVEN, reason),
        )
    unlisted = unlisted_roots(mounts)
    if unlisted:
        roots = Verdict(
            ROOTS_CELL, Outcome.NOT_DRIVEN, f"{unlisted} directory roots were not listed"
        )
    else:
        roots = Verdict(
            ROOTS_CELL, Outcome.HELD, f"every directory root of {len(mounts)} mounts was listed"
        )
    leaked = token_files(mounts)
    unread = unread_entries(mounts)
    if leaked:
        token = Verdict(TOKEN_CELL, Outcome.FAILED, f"{leaked} files hold the login token")
    elif unread:
        token = Verdict(
            TOKEN_CELL, Outcome.NOT_DRIVEN, f"{unread} entries could not be read for the token"
        )
    else:
        token = Verdict(
            TOKEN_CELL, Outcome.HELD, "0 files outside the login directory hold the login token"
        )
    return roots, token


# --- B9 -----------------------------------------------------------------------------------------


class ClientReport(NamedTuple):
    """What the run client printed: only lines that fullmatched `CLIENT_LINE` count as its
    fields; any other line is counted and nothing of it is kept."""

    answer: int | None = None
    answer_code: str | None = None
    events: tuple[str, ...] = ()
    closed_at: float | None = None
    state: str | None = None
    running: bool = False
    loss: str | None = None
    broke_off: bool = False
    unmatched: int = 0

    @property
    def started(self) -> bool:
        return RUN_STARTED in self.events


def read_client_output(lines: Iterable[str]) -> ClientReport:
    report = ClientReport()
    for line in lines:
        found = CLIENT_LINE.fullmatch(line)
        if not found:
            report = report._replace(unmatched=report.unmatched + 1)
        elif found["answer"]:
            report = report._replace(answer=int(found["answer"]), answer_code=found["answer_code"])
        elif found["event"]:
            report = report._replace(events=(*report.events, found["event"]))
        elif found["closed_at"]:
            report = report._replace(closed_at=float(found["closed_at"]))
        elif found["state"]:
            report = report._replace(
                state=found["state"], loss=found["loss"], running=bool(found["running"])
            )
        else:
            report = report._replace(broke_off=True)
    return report


class AppCall(NamedTuple):
    """The call of `step` the fake app logged: whether it was held, and when it was released."""

    held: bool
    released_at: float | None


def app_call(lines: Iterable[str]) -> AppCall:
    held, released = False, None
    for line in lines:
        held = held or line == HELD_LINE
        if line.startswith(RELEASED_PREFIX):
            released = float(line.removeprefix(RELEASED_PREFIX))
    return AppCall(held, released)


def wait_until(
    condition: Callable[[int], bool],
    seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """True as soon as `condition` holds within `seconds`, False when they have passed first,
    also when the condition itself held only after them. The condition gets the whole seconds
    left (at least one) to bound its own reads; a read that times out counts as the deadline
    passed."""
    end = clock() + seconds
    while True:
        try:
            if condition(max(int(end - clock()), 1)):
                return clock() <= end
        except subprocess.TimeoutExpired:
            return False
        if clock() >= end:
            return False
        sleep(CALL_POLL_SECONDS)


class CallWait(StrEnum):
    HELD = "held"
    CLIENT_EXITED = "the client exited"
    TIMED_OUT = "no call within the time"


def wait_for_call(
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    app_lines: Callable[[int], list[str]],
    client_alive: Callable[[], bool],
) -> CallWait:
    """Polls the app's lines (those since R2 began) until a `held` line appears, the client
    exits, or `R2_CALL_SECONDS` have passed."""
    held_polls: list[bool] = []

    def call_or_exit(left: int) -> bool:
        held_polls.append(app_call(app_lines(left)).held)
        return held_polls[-1] or not client_alive()

    if not wait_until(call_or_exit, R2_CALL_SECONDS, clock, sleep):
        return CallWait.TIMED_OUT
    return CallWait.HELD if held_polls[-1] else CallWait.CLIENT_EXITED


class B9Evidence(NamedTuple):
    client: ClientReport
    watch: Watched | None
    call: AppCall
    log: LogClasses  # the service log's records since R2 began
    working_root_before: int | None  # its entries at S00, just before R2; None: not shown


class B9Result(NamedTuple):
    verdicts: tuple[Verdict, ...]
    timing: str | None  # printed only when (b), (c) and (d) held


def b9_not_driven(reason: str) -> tuple[Verdict, ...]:
    return tuple(Verdict(cell, Outcome.NOT_DRIVEN, reason) for cell in B9_CELLS)


def _loss_verdict(client: ClientReport) -> Verdict:
    cell = B9_CELLS[0]
    if client.state is None or client.state == REFUSED:
        return Verdict(cell, Outcome.NOT_DRIVEN, f"status read: {client.state or 'none'}")
    evidence = f"client_lost {client.loss}, state {client.state}"
    if OTHER in (client.state, client.loss):
        return Verdict(cell, Outcome.NOT_DRIVEN, "status read: a value outside the wire's sets")
    if client.running or client.loss == NO_LOSS:
        return Verdict(cell, Outcome.FAILED, evidence)
    return Verdict(cell, Outcome.HELD, evidence)


def _gone_verdict(
    cell: str, what: str, gone_at: float | None, seen: bool, t0: float | None
) -> Verdict:
    if t0 is None:
        return Verdict(cell, Outcome.NOT_DRIVEN, "the client logged no t0")
    if not seen:
        return Verdict(cell, Outcome.NOT_DRIVEN, f"the watch never saw {what}")
    if gone_at is None:
        return Verdict(cell, Outcome.FAILED, f"{what} was never seen gone before the deadline")
    if gone_at < t0:
        return Verdict(cell, Outcome.FAILED, f"{what} was seen gone before t0")
    return Verdict(cell, Outcome.HELD, f"{what} was seen gone {gone_at - t0:.2f} s after t0")


def _tree_verdict(evidence: B9Evidence) -> Verdict:
    cell = B9_CELLS[2]
    before = evidence.working_root_before
    if before != 0:
        reason = (
            "S00 showed no working root"
            if before is None
            else f"the working root held {before} entries before R2"
        )
        return Verdict(cell, Outcome.NOT_DRIVEN, reason)
    watch = evidence.watch
    return _gone_verdict(
        cell, "the run tree", watch and watch.tree_gone_at, bool(watch and watch.tree_seen),
        evidence.client.closed_at,
    )  # fmt: skip


def _release_verdict(evidence: B9Evidence) -> Verdict:
    cell, released, t0 = B9_CELLS[3], evidence.call.released_at, evidence.client.closed_at
    if released is None:
        return Verdict(cell, Outcome.FAILED, f"released did not come within {RELEASE_SECONDS} s")
    if t0 is None:
        return Verdict(cell, Outcome.NOT_DRIVEN, "the client logged no t0")
    if released < t0:
        return Verdict(cell, Outcome.FAILED, "the app's connection closed before the client's")
    return Verdict(cell, Outcome.HELD, f"released {released - t0:.2f} s after t0")


def _log_verdict(log: LogClasses) -> Verdict:
    errors = log.other_errors + log.login_refused
    evidence = (
        f"{log.other_errors} other ERROR records, {log.login_refused} login-refused, "
        f"{log.escaped} bare lines"
    )
    return Verdict(B9_CELLS[4], Outcome.HELD if not errors else Outcome.FAILED, evidence)


def b9_verdicts(evidence: B9Evidence) -> B9Result:
    """agent-runtime#440 B9 (a)-(e) for a run whose call the app held and whose stream the client
    closed at t0. The timing is an observation: no bound is claimed."""
    watch, t0 = evidence.watch, evidence.client.closed_at
    group = _gone_verdict(
        B9_CELLS[1], "the CLI group", watch and watch.group_gone_at,
        bool(watch and watch.group_seen), t0,
    )  # fmt: skip
    verdicts = (
        _loss_verdict(evidence.client), group, _tree_verdict(evidence),
        _release_verdict(evidence), _log_verdict(evidence.log),
    )  # fmt: skip
    seen = all(verdict.outcome is Outcome.HELD for verdict in verdicts[1:4])
    return B9Result(verdicts, _timing(evidence) if seen else None)


def _timing(evidence: B9Evidence) -> str:
    watch, t0 = evidence.watch, evidence.client.closed_at
    reaped, tree, released = watch.group_gone_at, watch.tree_gone_at, evidence.call.released_at
    total = max(reaped, tree, released) - t0
    return (
        f"B9 timing (observed, no bound): max(t_reap, t_tree, t_up) - t0 = {total:.2f} s; "
        f"t_up - t_reap = {released - reaped:.2f} s"
    )
