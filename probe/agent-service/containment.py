"""The containment cells (`unit.py --cell containment`, agent-runtime#440 B1 and B2): a run whose
app lists tools, in the stand-in issuer's project with a fake app on `agent-songmaker`
(app_tools.py), asked by the repository's own `agent-smoke --tool`.

B1 is the run with the relay: the CLI's start announcement is accepted (no refusal verdict) and
the run reaches the upstream, which refuses the dummy login. B2 plants the run capability's
signature in a tool description: the relay's owner fails with `CapabilityReflected`, and the
runner's failure is `CapabilityReflected` too, with no announcement verdict (a
`RELAY_NOT_CONNECTED` line is tolerated, any other verdict fails the cell) and no run reaching
`CliLoginRefusedError`; the run fails. A cell reads verdicts, counts and event names, never a
token, and none of them prints a log line. The (a) parts, the loopback capture, are #339 C2's."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from census_client import SERVICE_UID, CensusClient, Process
from egress import CLI_BINARY, LABEL, REPO, NotDriven, ProbeFailed, expect
from issuer import ISSUER_SERVICE, SERVICE, IssuerProbe, wait_for_listening
from refusals import SMOKE_SERVICE
from scratch_secrets import issue_certificate
from smoke import Tokens

APP_SERVICE = "agent-app"
APP_PORT = 8444
APP_ORIGIN = f"https://{APP_SERVICE}:{APP_PORT}"
ORIGIN_LINE = re.compile(r'^mcp_origin = ".*"$', re.MULTILINE)
BINARY_LINE = re.compile(r'^binary = ".*"$', re.MULTILINE)
WRAPPER_DIRECTORY = "/opt/probe-wrapper"
WRAPPER = f"{WRAPPER_DIRECTORY}/claude"
PLANT_FLAG = "plant"
TEMPORARY = "/tmp"  # NOSONAR(S5443) only listed in the census, never written to
FRAME = "event: failed"
WINDOW_SECONDS = 60  # the sampler's window, which covers the whole run
REAP_SECONDS = 6  # the reap's bound is 5 s
DIE_SECONDS = 30
SMOKE_TOOLS = ("step:read_only", "bare:read_only")
REFUSED_ANNOUNCEMENT = "A CLI run's start announcement was refused ("
RELAY_NOT_CONNECTED = f"{REFUSED_ANNOUNCEMENT}RELAY_NOT_CONNECTED)"
LOGIN_REFUSED = "CliLoginRefusedError"
RELAY_OWNER_FAILED = re.compile(
    r"The owner of the relay of run \S+ failed: CapabilityReflected at "
)
RUNNER_FAILED = re.compile(r"The runner of run \S+ failed: CapabilityReflected at ")
APP_LINE = re.compile(
    r"method=(?:initialize|notifications/initialized|tools/list|other) "
    r"offered=(?:-|step,bare,extra) canary=[01] bearer=[01]"
)
LOG_SECONDS = 30


APP_HANDSHAKE = (
    "method=initialize offered=- canary=0 bearer=1",
    "method=notifications/initialized offered=- canary=0 bearer=1",
)
APP_LISTING = "method=tools/list offered=step,bare,extra canary={canary} bearer=1"
PROVIDER_ERROR = "provider_error"


class LogCounts(NamedTuple):
    """How often the service log holds each kind of verdict line."""

    refused: int
    login_refused: int
    relay_owner_failed: int
    runner_failed: int
    relay_not_connected: int

    @classmethod
    def of(cls, log: str) -> LogCounts:
        return cls(
            log.count(REFUSED_ANNOUNCEMENT),
            log.count(LOGIN_REFUSED),
            len(RELAY_OWNER_FAILED.findall(log)),
            len(RUNNER_FAILED.findall(log)),
            log.count(RELAY_NOT_CONNECTED),
        )

    def since(self, earlier: LogCounts) -> LogCounts:
        return LogCounts(*(now - before for now, before in zip(self, earlier, strict=True)))

    def other_refusals(self) -> int:
        """Announcement verdicts other than RELAY_NOT_CONNECTED."""
        return self.refused - self.relay_not_connected


@dataclass(frozen=True)
class Observed:
    """What one run left behind: the smoke's lines, the fake app's new lines and the new service
    log counts."""

    smoke: list[str]
    app: list[str]
    new: LogCounts

    def failed_as_provider_error(self) -> bool:
        return "event: failed" in self.smoke and any(
            line.startswith(PROVIDER_ERROR) for line in self.smoke
        )


Verdicts = dict[str, tuple[bool, str]]


def smoke_evidence(seen: Observed) -> str:
    return (
        f"event: failed {'seen' if 'event: failed' in seen.smoke else 'missing'}, "
        f"{PROVIDER_ERROR} {'seen' if seen.failed_as_provider_error() else 'missing'}"
    )


def b1_verdicts(seen: Observed) -> Verdicts:
    new = seen.new
    return {
        "B1 (b)": (
            new.refused == 0 and new.login_refused == 1,
            f"{new.refused} announcement refusals and {new.login_refused} refused logins",
        ),
        "B1 (c)": (seen.failed_as_provider_error(), smoke_evidence(seen)),
    }


def b2_verdicts(seen: Observed) -> Verdicts:
    new = seen.new
    return {
        "B2 (b)": (
            new.relay_owner_failed == 1,
            f"{new.relay_owner_failed} lines of the relay's owner failing on the capability",
        ),
        "B2 (c)": (
            new.runner_failed == 1 and new.login_refused == 0 and new.other_refusals() == 0,
            f"{new.runner_failed} lines of the runner failing on the capability, "
            f"{new.login_refused} refused logins, {new.other_refusals()} other announcement "
            f"verdicts, {new.relay_not_connected} RELAY_NOT_CONNECTED verdicts (tolerated)",
        ),
        "B2 (d)": (seen.failed_as_provider_error(), smoke_evidence(seen)),
    }


class Snapshot(NamedTuple):
    """One look inside the service container: its processes and how many entries the census
    lists in the working root and in /tmp. `sleep` is the opaque name the census shows `sleep`
    by, which is what the markers are."""

    processes: tuple[Process, ...]
    sleep: str
    working_root: int
    temporary: int

    def sleepers(self) -> tuple[Process, ...]:
        return tuple(seen for seen in self.processes if seen.name == self.sleep)

    def plain_markers(self) -> tuple[Process, ...]:
        """Marker A: a marker in the group of its parent, the CLI, which leads that group."""
        return tuple(m for m in self.sleepers() if m.ppid == m.pgid != m.pid)

    def stray_markers(self) -> tuple[Process, ...]:
        """Any marker that leads no session, so one orphaned under init counts too."""
        return tuple(m for m in self.sleepers() if m.sid != m.pid)

    def session_markers(self) -> tuple[Process, ...]:
        """Marker B: the leader of a session of its own."""
        return tuple(m for m in self.sleepers() if m.sid == m.pid)

    def markers_below_init(self) -> bool:
        return all(marker.ppid == 1 for marker in self.session_markers())

    def service_pids(self) -> tuple[int, ...]:
        """The processes below init, which is not the service's own pid 1."""
        return tuple(seen.pid for seen in self.processes if seen.ppid == 1 and seen.pid != 1)


def expect_planted(window: Snapshot) -> None:
    plain, session = window.plain_markers(), window.session_markers()
    missing = [name for name, found in (("A", plain), ("B", session)) if not found]
    if missing:
        raise NotDriven(
            f"B3: marker {' and '.join(missing)} was not seen among the "
            f"{len(window.processes)} processes of the run's window"
        )


def b3_verdicts(window: Snapshot, frame: Snapshot, later: Snapshot) -> Verdicts:
    plain, session = window.plain_markers(), window.session_markers()
    apart = all(b.pgid != a.pgid and b.sid != a.sid for a in plain for b in session)
    survivors = later.session_markers()
    return {
        "B3 (during)": (
            bool(plain and session) and apart,
            f"{len(plain)} records in the CLI's group, {len(session)} in a session of their own "
            f"(a marker is one record per parent it had)",
        ),
        "B3 (frame)": (
            bool(frame.session_markers()),
            f"{len(frame.session_markers())} markers in their own session alive (asserted); "
            f"{len(frame.stray_markers())} markers in the CLI's group, {frame.working_root} "
            f"working-root entries, parent of B is init: {frame.markers_below_init()} (reported)",
        ),
        "B3 (6 s)": (
            not later.stray_markers()
            and later.working_root == 0
            and len(survivors) == 1
            and later.markers_below_init(),
            f"{len(later.stray_markers())} markers in the CLI's group, {later.working_root} "
            f"working-root entries, {len(survivors)} markers in their own session, "
            f"parent of B is init: {later.markers_below_init()}",
        ),
    }


def b4_verdicts(
    dies: int, restarts_before: int, restarts_after: int, host_found: int, restarted: Snapshot
) -> Verdicts:
    return {
        "B4 (die)": (
            dies >= 1 and restarts_after == restarts_before + 1,
            f"{dies} die events, RestartCount {restarts_before} to {restarts_after}",
        ),
        "B4 (gone)": (
            host_found == 0 and not restarted.sleepers(),
            f"{host_found} marker processes on the host, {len(restarted.sleepers())} in the "
            f"restarted container",
        ),
        "B4 (empty)": (
            restarted.working_root == 0 and restarted.temporary == 0,
            f"{restarted.working_root} working-root entries and {restarted.temporary} /tmp "
            f"entries after the restart",
        ),
    }


class Mode(StrEnum):
    RELAY = "relay"
    CANARY = "canary"


def with_containment(text: str) -> str:
    """The service's configuration with the fake app's origin in place of songmaker's, and the
    wrapper in place of the CLI; the signing key and the CA come from the repository's own
    `[signing]` and `[mcp]`."""
    expect(len(ORIGIN_LINE.findall(text)) == 1, "service.toml: no single mcp_origin")
    expect(len(BINARY_LINE.findall(text)) == 1, "service.toml: no single binary")
    moved = ORIGIN_LINE.sub(f'mcp_origin = "{APP_ORIGIN}"', text)
    return BINARY_LINE.sub(f'binary = "{WRAPPER}"', moved)


def wrapper_text(cli: str) -> str:
    """Transparent unless asked: on `-p`, and only while the host's flag file sits beside it, it
    starts marker A as a plain background child and marker B in a session of its own, both with
    the stdio detached so that neither holds the run's pipes, then runs the CLI in its place."""
    flag = f'"${{0%/*}}/{PLANT_FLAG}"'
    return f"""#!/bin/sh
case " $* " in
*" -p "*)
    if [ -r {flag} ]; then
        read -r plain detached < {flag}
        sleep "$plain" </dev/null >/dev/null 2>&1 &
        setsid sleep "$detached" </dev/null >/dev/null 2>&1 &
    fi
    ;;
esac
exec {cli} "$@"
"""


class ContainmentProbe(IssuerProbe):
    """The service with the scratch signing key and CA and a fake app that lists tools on
    `agent-songmaker`, its certificate issued by that CA."""

    def __init__(self, scratch: Path) -> None:
        super().__init__(scratch)
        self.shown: list[str] = []
        directory = scratch / "containment"
        directory.mkdir()
        app = directory / "app"
        app.mkdir()
        self.mode_directory = directory / "mode"
        self.mode_directory.mkdir()
        self.write_mode(Mode.RELAY)
        certificate, key = issue_certificate(self.scratch_secrets, APP_SERVICE, app)
        self.wrapper_directory = directory / "wrapper"
        self.wrapper_directory.mkdir()
        wrapper = self.wrapper_directory / "claude"
        wrapper.write_text(wrapper_text(CLI_BINARY), encoding="utf-8")
        wrapper.chmod(0o755)
        for readable in (app, self.mode_directory, self.wrapper_directory):
            readable.chmod(0o755)
        for name in (certificate, key):
            name.chmod(0o644)
        self.configuration.write_text(
            with_containment(self.configuration.read_text(encoding="utf-8")), encoding="utf-8"
        )
        program = Path(__file__).resolve().with_name("app_tools.py")
        overlay = scratch / "compose.containment.yaml"
        overlay.write_text(
            json.dumps({"services": {
                APP_SERVICE: {
                    "image": self.image,
                    "profiles": ["agent-service"],
                    "user": "65534:65534", "read_only": True, "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"], "networks": ["agent-songmaker"],
                    "entrypoint": ["python", "-I", "-B", "/app_tools.py"],
                    "volumes": [
                        {"type": "bind", "source": str(program), "target": "/app_tools.py",
                         "read_only": True},
                        {"type": "bind", "source": str(app), "target": "/app",
                         "read_only": True},
                        {"type": "bind", "source": str(self.mode_directory), "target": "/mode",
                         "read_only": True},
                    ],
                },
                SERVICE: {
                    "volumes": [
                        {"type": "bind", "source": str(self.wrapper_directory),
                         "target": WRAPPER_DIRECTORY, "read_only": True},
                    ],
                },
            }}),
            encoding="utf-8",
        )  # fmt: skip
        self.overlays.append(overlay)

    def services_to_start(self) -> tuple[str, ...]:
        return ("agent-egress", ISSUER_SERVICE, APP_SERVICE, SERVICE)

    def write_mode(self, mode: Mode) -> None:
        (self.mode_directory / "mode").write_text(mode.value, encoding="utf-8")
        (self.mode_directory / "mode").chmod(0o644)

    def drive(self) -> None:
        self.wait_until_listening()
        tokens = self.tokens()
        dummy = (self.token_path / "token").read_text(encoding="utf-8").strip()
        relay = self.run_with(Mode.RELAY, tokens)
        canary = self.run_with(Mode.CANARY, tokens)
        self.expect_driven("B1", relay, [*APP_HANDSHAKE, APP_LISTING.format(canary=0)])
        self.expect_driven("B2", canary, [APP_LISTING.format(canary=1)])
        failed = [*self.judge(b1_verdicts(relay)), *self.judge(b2_verdicts(canary))]
        for cell in ("B1 (a)", "B2 (a)"):
            print(f"{cell} NOT DRIVEN: the loopback capture is #339 C2's", flush=True)
        census = CensusClient(self, self.container(SERVICE), self.configuration)
        service_pids = self.baseline(census)
        try:
            detached, snapshot = self.cell_b3(census, tokens)
            failed += self.judge(snapshot)
            failed += self.judge(self.cell_b4(census, service_pids, detached))
        finally:
            self.expect_no_token(tokens, dummy)
            self.cell_s2()
        if failed:
            raise ProbeFailed(f"cells failed: {', '.join(failed)}")

    # --- the cells ----------------------------------------------------------------------------

    def judge(self, verdicts: Verdicts) -> list[str]:
        """Each cell held or failed, reported at once; the failed names come back."""
        for cell, (held, evidence) in verdicts.items():
            if held:
                self.held.append(cell)
            print(f"{cell} {'held' if held else 'FAILED'}: {evidence}", flush=True)
        return [cell for cell, (held, _) in verdicts.items() if not held]

    def expect_driven(self, cell: str, seen: Observed, lines: list[str]) -> None:
        missing = [line for line in lines if line not in seen.app]
        if missing:
            raise NotDriven(
                f"{cell}: the fake app did not log {len(missing)} of {len(lines)} expected "
                f"lines ({', '.join(line.split()[0] for line in missing)}); it logged "
                f"{len(seen.app)} lines; {smoke_evidence(seen)}"
            )

    def baseline(self, census: CensusClient) -> tuple[int, ...]:
        """The pids below init, from a look at the transparent wrapper's start: no marker exists."""
        looked = self.snapshot(census)
        expect(not looked.sleepers(), "the wrapper planted a marker before the run")
        pids = looked.service_pids()
        print(
            f"baseline: no marker, {looked.working_root} working-root entries and "
            f"{looked.temporary} /tmp entries",
            flush=True,
        )
        return pids

    def snapshot(self, census: CensusClient) -> Snapshot:
        working_root, temporary = census.listed(census.working_root, TEMPORARY)
        return Snapshot(census.processes(), census.sleep, working_root, temporary)

    def cell_b3(self, census: CensusClient, tokens: Tokens) -> tuple[str, Verdicts]:
        """The run with the markers planted: the process sampler's window covers all of it, then
        a look at the terminal frame and one `REAP_SECONDS` after it. Returns marker B's number."""
        plain, detached = (f"3000.{secrets.randbelow(10**9):09d}" for _ in range(2))
        expect(plain != detached, "B3: the two markers got one number")
        flag = self.wrapper_directory / PLANT_FLAG
        flag.write_text(f"{plain} {detached}\n", encoding="utf-8")
        flag.chmod(0o644)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                sampled = pool.submit(census.processes, WINDOW_SECONDS)
                frame, later = self.run_until_frame_and_after(census, tokens)
                window = Snapshot(sampled.result(), census.sleep, 0, 0)
        finally:
            flag.unlink(missing_ok=True)
        expect_planted(window)
        return detached, b3_verdicts(window, frame, later)

    def run_until_frame_and_after(
        self, census: CensusClient, tokens: Tokens
    ) -> tuple[Snapshot, Snapshot]:
        """One run, its output read as it comes: a look when the terminal frame arrives, another
        `REAP_SECONDS` later by the monotonic clock, then the rest of the run's output."""
        self.write_mode(Mode.RELAY)
        smoke = subprocess.Popen(
            self.smoke_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=self.environment, cwd=REPO,
            close_fds=True,
        )  # fmt: skip
        self.windows.append(smoke)
        smoke.stdin.write(f"{tokens.person}\n{tokens.workload}\n")
        smoke.stdin.close()
        lines: list[str] = []
        frame: Snapshot | None = None
        for line in smoke.stdout:
            lines.append(line)
            if line.rstrip("\n") == FRAME:
                arrived = time.monotonic()
                frame = self.snapshot(census)
                break
        if frame is None:
            smoke.wait()
            self.shown.append("".join(lines))
            raise NotDriven(f"B3: the run printed no {FRAME}")
        time.sleep(max(0.0, arrived + REAP_SECONDS - time.monotonic()))
        later = self.snapshot(census)
        lines.append(smoke.communicate()[0])
        self.shown.append("".join(lines))
        return frame, later

    def cell_b4(
        self, census: CensusClient, service_pids: tuple[int, ...], detached: str
    ) -> Verdicts:
        """The service killed by its pid, as its own user, while marker B lives; the container
        is the one the guarded compose resolves."""
        if len(service_pids) != 1:
            raise NotDriven(f"B4: {len(service_pids)} processes below init, not one service")
        (service_pid,) = service_pids
        container = self.container(SERVICE)
        before_kill = self.snapshot(census)
        found = self.host_markers(detached)
        if not before_kill.session_markers() or found == 0:
            raise NotDriven(
                f"B4: marker B seen by the container {len(before_kill.session_markers())} and "
                f"by the host {found} times; both must see it"
            )
        service_user = f"{SERVICE_UID}:{SERVICE_UID}"
        restarts_before = self.restart_count(container)
        killed_at = int(time.time())
        self.run(
            ["docker", "exec", "--user", service_user, container, "python", "-I", "-c",
             f"import os, signal; os.kill({service_pid}, signal.SIGKILL)"],
            check=False,
        )  # fmt: skip
        dies = self.wait_for_die(container, killed_at)
        wait_for_listening(
            lambda: self.log_since(container, killed_at), "the restarted service did not listen"
        )
        return b4_verdicts(
            dies,
            restarts_before,
            self.restart_count(container),
            self.host_markers(detached),
            self.snapshot(census),
        )

    def wait_for_die(self, container: str, since: int) -> int:
        deadline = time.monotonic() + DIE_SECONDS
        dies = 0
        while not dies and time.monotonic() < deadline:
            events = self.run(
                ["docker", "events", "--since", str(since), "--until", str(int(time.time()) + 1),
                 "--filter", f"container={container}", "--filter", "event=die"],
                check=False, timeout=60,
            )  # fmt: skip
            dies = len(events.stdout.splitlines())
        return dies

    def restart_count(self, container: str) -> int:
        return int(
            self.run(["docker", "inspect", "--format", "{{.RestartCount}}", container]).stdout
        )

    def log_since(self, container: str, since: int) -> str:
        logs = self.run(["docker", "logs", "--since", str(since), container], check=False)
        if logs.returncode != 0:
            raise ProbeFailed(f"docker logs {container} exited {logs.returncode}")
        return logs.stdout + logs.stderr

    def host_markers(self, marker: str) -> int:
        """How many host processes run `sleep <marker>`; the pattern holds no raw name."""
        found = self.run(["pgrep", "-f", f"sleep {re.escape(marker)}"], check=False)
        return len(found.stdout.split())

    def cell_s2(self) -> None:
        stray = [line for line in self.app_lines() if not APP_LINE.fullmatch(line)]
        expect(not stray, f"S2: the fake app logged {len(stray)} lines outside its vocabulary")
        self.held.append("S2")

    def expect_no_token(self, tokens: Tokens, dummy: str) -> None:
        shown = "\n".join([*self.shown, self.service_log(), *self.app_lines()])
        for secret in (tokens.person, tokens.workload, dummy):
            expect(secret not in shown, "S1: a token was printed")
        self.held.append("S1")

    # --- one run and what it leaves -----------------------------------------------------------

    def run_with(self, mode: Mode, tokens: Tokens) -> Observed:
        self.write_mode(mode)
        app_before = len(self.app_lines())
        before = self.verdict_counts()
        smoke = self.run(
            self.smoke_command(),
            stdin=f"{tokens.person}\n{tokens.workload}\n",
            check=False,
            timeout=240,
        )
        self.shown.append(smoke.stdout + smoke.stderr)
        new = self.wait_for_a_verdict(before).since(before)
        return Observed(smoke.stdout.splitlines(), self.app_lines()[app_before:], new)

    def smoke_command(self) -> list[str]:
        tools = [argument for tool in SMOKE_TOOLS for argument in ("--tool", tool)]
        return self.compose_command(
            "run", "--rm", "-T", "--no-deps", "--label", LABEL, SMOKE_SERVICE,
            "--timeout", "180", *tools,
        )  # fmt: skip

    def wait_for_a_verdict(self, before: LogCounts) -> LogCounts:
        """The service logs a run's verdict a moment after the smoke saw its end; the runner's
        line comes after the relay owner's."""
        deadline = time.monotonic() + LOG_SECONDS
        counts = self.verdict_counts()
        while (
            counts.login_refused + counts.runner_failed
            == before.login_refused + before.runner_failed
            and time.monotonic() < deadline
        ):
            time.sleep(0.5)
            counts = self.verdict_counts()
        return counts

    def verdict_counts(self) -> LogCounts:
        return LogCounts.of(self.service_log())

    def service_log(self) -> str:
        return self.container_log(SERVICE)

    def app_lines(self) -> list[str]:
        return self.container_log(APP_SERVICE).splitlines()
