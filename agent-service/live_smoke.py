#!/usr/bin/env python3
"""The live smoke of the agent service in one command (README "The agent service", "The smoke").

    cd ~/git/edge && python3 agent-service/live_smoke.py

You sign in as yourself (the `songmaker` client's code flow with PKCE): open the printed
address, sign in, and paste the address of the page that then fails; it is read without echo.
The person's and the workload's token live in this process only and reach `agent-smoke`
on its stdin; no token or secret is on a command line, in a file or in the report.
The run recreates the service, takes the idle
memory, runs the paid turn (L2, L3), the memory peak (L7), the cancelled turn (L4, counts only), the
searches for a leaked secret (L6) and the egress cells of the running container (L5: E6-E10, E12).
Exit 1 when a cell prints FAILED; L4 is an observation and never fails the run.
L7's figures are the idle `memory.current` of the recreated service and its `memory.peak`
right after the one turn (an upper bound of the answer phase, it includes the start).
A refusal `unauthorized` means expired tokens, `forbidden` a missing `/ki-abo` or tokens that do not share an `azp`.
`agent-smoke` is the service image running `smoke.py` on the `agent` network alone (profile
`apply`).

The compose project and the egress gateway's address are read from `docker compose config` of the
repository this file sits in. The in-container smoke is `smoke.py`, used as it is, and the egress
observations are `egress_observe.py`, shared with the probe. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.client
import inspect
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import egress_observe
from egress_observe import PUBLIC_RESOLVER, lan_address
from smoke import HOLDING_MESSAGE, Tokens

OIDC = "https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect"
CLIENT = "songmaker"
REDIRECT = "https://songmaker.hallucinai.de/auth/callback"
SECRETS_DIRECTORY = Path.home() / ".secrets"
CLIENT_SECRET_FILE = SECRETS_DIRECTORY / "keycloak/clients" / CLIENT
LOGIN_TOKEN_FILE = SECRETS_DIRECTORY / "agent-service/claude/token"
REPOSITORY = Path(__file__).resolve().parents[1]
LIVE_COMPOSE = (
    "docker", "compose", "--project-directory", str(REPOSITORY),
    "--file", str(REPOSITORY / "compose.yaml"), "--profile", "agent-service",
)  # fmt: skip
GATEWAY_ADDRESS_KEY = "x-egress-address"
SERVICE = "agent-service"
SMOKE_SERVICE = "agent-smoke"
EGRESS_SERVICE = "agent-egress"
LISTENING = "Uvicorn running"
LISTEN_ATTEMPTS = 120
SETTLE_SECONDS = 5
TOKEN_TIMEOUT_SECONDS = 30
PAID_TURN_SECONDS = 420  # above the 300 s of each socket of smoke.py
CANCELLED_TURN_SECONDS = 420
CHILD_END_SECONDS = 10
RECREATE_REASON_CHARACTERS = 200
SAMPLE_FAILED = "SAMPLE FAILED"
TIMED_OUT = 124
UNREACHABLE = {"ENETUNREACH", "EHOSTUNREACH", "EADDRNOTAVAIL"}
EMBEDDED_RESOLVER = "127.0.0.11"
PROVIDER_HOST = "api.anthropic.com"
CLI_PROCESS_COUNT = (
    'file=$(mktemp) && cat /proc/[0-9]*/cmdline > "$file" && tr "\\0" "\\n" < "$file" '
    '| grep -c "[c]laude"; status=$?; rm -f "$file"; test "$status" -le 1'
)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LiveSmokeFailed(Exception):
    """The run cannot go on; the message is the one line to print, free of any secret."""


class Verdict(StrEnum):
    HELD = "held"
    FAILED = "FAILED"
    OBSERVED = "observed"


@dataclass(frozen=True)
class Cell:
    name: str
    verdict: Verdict
    figure: str

    def line(self) -> str:
        return f"{self.name} {self.verdict}: {self.figure}"


def redacted(text: str, values: Sequence[str]) -> str:
    """`text` with every value removed, the longest first so that none leaves a part behind."""
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[secret]")
    return text


@dataclass(frozen=True)
class Secrets:
    """Everything that must appear nowhere: the two tokens, the login token, the client secret."""

    tokens: Tokens
    login_token: str
    client_secret: str

    def values(self) -> list[str]:
        everything = (
            self.tokens.person, self.tokens.workload, self.login_token, self.client_secret
        )  # fmt: skip
        return [value for value in everything if value]

    def redact(self, text: str) -> str:
        return redacted(text, self.values())


@dataclass(frozen=True)
class Stack:
    """One compose project and how docker is asked for it.

    `compose_prefix` is everything before the compose subcommand: the repository's own files for
    the live stack, the probe's project and files for the probe. The project's name and the egress
    gateway's address are read from the configuration compose resolves for that prefix."""

    compose_prefix: tuple[str, ...] = LIVE_COMPOSE
    environment: Mapping[str, str] | None = None
    directory: Path | None = None

    def run(
        self, command: Sequence[str], stdin: str | None = None, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, input=stdin, capture_output=True, text=True, check=False, timeout=timeout,
            env=self.environment, cwd=self.directory,
        )  # fmt: skip

    def compose(
        self, *arguments: str, stdin: str | None = None, timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        return self.run([*self.compose_prefix, *arguments], stdin, timeout)

    def start_compose(self, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*self.compose_prefix, *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=self.environment, cwd=self.directory,
        )  # fmt: skip

    @cached_property
    def configuration(self) -> dict:
        answer = self.compose("config", "--format", "json")
        try:
            configuration = json.loads(answer.stdout) if answer.returncode == 0 else None
        except ValueError:
            configuration = None
        if not isinstance(configuration, dict):
            raise LiveSmokeFailed(f"docker compose config failed: {last_line(answer.stderr)}")
        return configuration

    @property
    def project(self) -> str:
        return self.configuration["name"]

    @property
    def gateway(self) -> str:
        return self.configuration[GATEWAY_ADDRESS_KEY]


@dataclass
class Report:
    """Prints each cell at once, with every known secret removed from the line."""

    secret_values: list[str]
    cells: list[Cell] = field(default_factory=list)
    out: Callable[[str], None] = print

    def add(self, *cells: Cell) -> None:
        for cell in cells:
            self.cells.append(cell)
            self.out(redacted(cell.line(), self.secret_values))

    def failed(self) -> bool:
        return any(cell.verdict is Verdict.FAILED for cell in self.cells)


# --- the sign-in (ruled in edge#184, D6) ------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_address(challenge: str, state: str) -> str:
    return f"{OIDC}/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT, "redirect_uri": REDIRECT, "response_type": "code", "scope": "openid",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })  # fmt: skip


def code_in(pasted: str, state: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted.strip()).query)
    if query.get("state") != [state] or "code" not in query:
        raise LiveSmokeFailed("the address holds no code for this sign-in")
    return query["code"][0]


def access_token(
    form: Mapping[str, str], client_secret: str, urlopen=urllib.request.urlopen
) -> str:
    body = urllib.parse.urlencode({**form, "client_id": CLIENT, "client_secret": client_secret})
    try:
        with urlopen(f"{OIDC}/token", body.encode(), timeout=TOKEN_TIMEOUT_SECONDS) as answer:
            return json.load(answer)["access_token"]
    except urllib.error.HTTPError as refusal:
        raise LiveSmokeFailed(f"the token endpoint answered {refusal.code}") from None
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        raise LiveSmokeFailed("the token endpoint gave no access token") from None


def ask_without_echo(prompt: str, stream) -> str:
    """The pasted address holds a one-time code: stop rather than read it echoed."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt, stream=stream)
        except getpass.GetPassWarning:
            raise LiveSmokeFailed(
                "this terminal cannot hide the input; run it in a terminal"
            ) from None


def sign_in(
    client_secret: str,
    ask: Callable[..., str] = ask_without_echo,
    urlopen=urllib.request.urlopen,
) -> Tokens:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    print("Open this address and sign in; the page then fails, copy its address:", file=sys.stderr)
    print(authorization_address(challenge, state), file=sys.stderr)
    pasted = ask("Pasted address (not shown): ", stream=sys.stderr)
    person = access_token(
        {"grant_type": "authorization_code", "code": code_in(pasted, state),
         "redirect_uri": REDIRECT, "code_verifier": verifier},
        client_secret, urlopen,
    )  # fmt: skip
    workload = access_token({"grant_type": "client_credentials"}, client_secret, urlopen)
    return Tokens(person=person, workload=workload)


# --- what a smoke prints ----------------------------------------------------------------------


def last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no output"


def lines_holding(text: str, values: Sequence[str]) -> int:
    return sum(any(value in line for value in values) for line in text.splitlines())


def event_names(output: str) -> list[str]:
    return [
        line.removeprefix("event: ") for line in output.splitlines() if line.startswith("event: ")
    ]


def other_lines(output: str) -> str:
    """The answer and any refusal (`unauthorized`, `forbidden`); the smoke prints no token."""
    kept = [line for line in output.splitlines() if line and not line.startswith("event: ")]
    return " | ".join(kept)[:300]


def turn_cells(returncode: int, output: str, known: Secrets) -> list[Cell]:
    """The secrets go out of the output before it is cut, so that none leaves its beginning."""
    output = known.redact(output)
    events = event_names(output)
    text = other_lines(output)
    said = f"exit {returncode}; events {events}; {text}"
    answered = "assistant_text" in events or bool(text)
    paid = returncode == 0 and events[-1:] == ["succeeded"] and answered
    admitted = "run_started" in events
    return [
        Cell("L2 the paid turn", Verdict.HELD if paid else Verdict.FAILED, said),
        Cell(
            "L3 admission through the gateway",
            Verdict.HELD if admitted else Verdict.FAILED,
            "run_started seen" if admitted else "no run_started: the issuer's keys did not arrive",
        ),
    ]


# --- the service ------------------------------------------------------------------------------


def wait_until_listening(stack: Stack, pause: Callable[[float], None] = time.sleep) -> bool:
    for _ in range(LISTEN_ATTEMPTS):
        logs = stack.compose("logs", SERVICE)
        if LISTENING in logs.stdout + logs.stderr:
            return True
        pause(1)
    return False


def recreate_service(
    stack: Stack, known: Secrets, pause: Callable[[float], None] = time.sleep
) -> Cell:
    started = stack.compose("up", "-d", "--force-recreate", SERVICE)
    if started.returncode != 0:
        reason = known.redact(last_line(started.stderr))[:RECREATE_REASON_CHARACTERS]
        return Cell(
            "recreate", Verdict.FAILED, f"docker compose up exited {started.returncode}: {reason}"
        )
    if wait_until_listening(stack, pause):
        return Cell("recreate", Verdict.HELD, "the service listens")
    return Cell(
        "recreate", Verdict.FAILED,
        f"not listening after {LISTEN_ATTEMPTS} s; read: docker compose logs {SERVICE}",
    )  # fmt: skip


def cgroup_figure(stack: Stack, name: str) -> str | None:
    answer = stack.compose("exec", "-T", SERVICE, "cat", f"/sys/fs/cgroup/{name}")
    return answer.stdout.strip() if answer.returncode == 0 and answer.stdout.strip() else None


def cli_processes(stack: Stack) -> str:
    answer = stack.compose("exec", "-T", SERVICE, "sh", "-c", CLI_PROCESS_COUNT)
    count = answer.stdout.strip()
    return count if answer.returncode == 0 and count.isdigit() else SAMPLE_FAILED


def memory_cell(idle: str | None, peak: str | None) -> Cell:
    sampled = idle is not None and peak is not None
    return Cell(
        "L7 memory", Verdict.HELD if sampled else Verdict.FAILED,
        f"idle memory.current {idle or SAMPLE_FAILED}; "
        f"memory.peak after the turn {peak or SAMPLE_FAILED}",
    )  # fmt: skip


def remove_smoke_containers(stack: Stack) -> None:
    """A smoke the host stopped waiting for leaves its `run --rm` container behind."""
    listed = stack.run([
        "docker", "ps", "-aq",
        "--filter", f"label=com.docker.compose.project={stack.project}",
        "--filter", f"label=com.docker.compose.service={SMOKE_SERVICE}",
    ])  # fmt: skip
    leftovers = listed.stdout.split()
    if leftovers:
        stack.run(["docker", "rm", "-f", *leftovers])


def paid_turn(
    stack: Stack, tokens: Tokens, seconds: int = PAID_TURN_SECONDS
) -> subprocess.CompletedProcess[str]:
    try:
        return stack.compose(
            "run",
            "--rm",
            "-T",
            SMOKE_SERVICE,
            stdin=f"{tokens.person}\n{tokens.workload}\n",
            timeout=seconds,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], TIMED_OUT, f"the host gave up after {seconds} s", "")
    finally:
        remove_smoke_containers(stack)


@dataclass(frozen=True)
class CancelledTurn:
    returncode: int
    output: str
    during: str
    since: str


def end_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=CHILD_END_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def cancelled_turn(
    stack: Stack, tokens: Tokens, seconds: float = CANCELLED_TURN_SECONDS
) -> CancelledTurn:
    """Starts the cancelling smoke and counts the CLI processes while it holds its stream open.
    The host gives up after `seconds`, and the child and its container end however this leaves."""
    since = datetime.now(UTC).strftime(TIME_FORMAT)
    process = stack.start_compose("run", "--rm", "-T", SMOKE_SERVICE, "--cancel")
    gave_up = threading.Event()

    def give_up() -> None:
        gave_up.set()
        process.kill()

    watchdog = threading.Timer(seconds, give_up)
    watchdog.daemon = True
    watchdog.start()
    during = "no hold seen"
    lines = []
    try:
        try:
            process.stdin.write(f"{tokens.person}\n{tokens.workload}\n")
            process.stdin.close()
        except BrokenPipeError:
            pass  # the smoke ended first; its output and exit code say why
        for line in process.stdout:
            lines.append(line)
            if HOLDING_MESSAGE in line:
                during = cli_processes(stack)
        returncode = process.wait()
    finally:
        watchdog.cancel()
        end_child(process)
        remove_smoke_containers(stack)
    if gave_up.is_set():
        lines.append(f"the host gave up after {seconds:g} s\n")
    return CancelledTurn(returncode, "".join(lines), during, since)


def cancel_cells(
    stack: Stack,
    before: str,
    turn: CancelledTurn,
    known: Secrets,
    pause: Callable[[float], None] = time.sleep,
) -> list[Cell]:
    pause(SETTLE_SECONDS)
    after = cli_processes(stack)
    logs = stack.compose("logs", "--since", turn.since, SERVICE)
    errors = (
        lines_holding(logs.stdout + logs.stderr, ["ERROR"])
        if logs.returncode == 0
        else SAMPLE_FAILED
    )
    return [
        Cell(
            "L4 the cancelled turn", Verdict.OBSERVED,
            f"smoke exit {turn.returncode}; CLI processes before (0) {before}, while held "
            f"(1 or more) {turn.during}, left after the cancel (0) {after}; "
            f"ERROR lines since (0) {errors}; {other_lines(known.redact(turn.output))}",
        )
    ]  # fmt: skip


def leak_cells(stack: Stack, known: Secrets, smoke_outputs: str) -> list[Cell]:
    container = stack.compose("ps", "-q", SERVICE).stdout.strip()
    inspected = stack.run(["docker", "inspect", container]) if container else None
    logs = stack.compose("logs", SERVICE)
    searched = {
        "docker inspect": inspected.stdout if inspected and inspected.returncode == 0 else None,
        "the service log": logs.stdout + logs.stderr if logs.returncode == 0 else None,
        "the smoke output": smoke_outputs,
    }
    counts = {
        place: None if text is None else lines_holding(text, known.values())
        for place, text in searched.items()
    }
    return [
        Cell(
            "L6 no secret anywhere",
            Verdict.HELD if all(count == 0 for count in counts.values()) else Verdict.FAILED,
            "; ".join(
                f"{place} {SAMPLE_FAILED if count is None else count} (0)"
                for place, count in counts.items()
            ),
        )
    ]


# --- L5, the egress cells against the running container ---------------------------------------


@dataclass(frozen=True)
class EgressTargets:
    provider: str
    docker0: str
    internet: str
    lan: str
    agent_free: str
    egress_free: str
    agent_gateways: list[str]
    egress_gateways: list[str]


EGRESS_PROGRAM = (
    "import json, sys\n"
    + inspect.getsource(egress_observe)
    + """
targets = json.loads(sys.argv[1])
print(json.dumps({
    "public_tcp": {
        target: tcp_connect(target, 443)["result"]
        for target in (PUBLIC_RESOLVER, targets["provider"])
    },
    "defaults": default_routes()["default_routes"],
    "reach": {
        label: tcp_connect(targets[label], 22)["result"]
        for label in ("docker0", "internet", "lan", "agent_free", "egress_free")
    },
    "ipv6_interfaces": ipv6_interfaces()["interfaces"],
    "ipv6_tcp": tcp_connect(PUBLIC_RESOLVER_V6, 443)["result"],
    "udp": {str(port): udp_connect(PUBLIC_RESOLVER, port)["result"] for port in (443, 53)},
    "outside": resolve_name("example.com")["addresses"],
    "gateway": resolve_name("agent-egress")["addresses"],
}))
"""
)

LISTENERS_PROGRAM = (
    "import json\n"
    + inspect.getsource(egress_observe)
    + "\nprint(json.dumps(listening_sockets()))\n"
)


def network_ids(stack: Stack, name: str) -> list[str]:
    return stack.run([
        "docker", "network", "ls", "-q",
        "--filter", f"label=com.docker.compose.project={stack.project}",
        "--filter", f"label=com.docker.compose.network={name}",
    ]).stdout.split()  # fmt: skip


def inspect_network(stack: Stack, reference: str) -> dict:
    answer = stack.run(["docker", "network", "inspect", reference])
    if answer.returncode != 0:
        raise LiveSmokeFailed(f"docker network inspect {reference} failed")
    return json.loads(answer.stdout)[0]


def project_network(stack: Stack, name: str) -> dict:
    found = network_ids(stack, name)
    if len(found) != 1:
        raise LiveSmokeFailed(f"the project {stack.project} has no single network {name}")
    return inspect_network(stack, found[0])


def gateways_of(network: dict) -> list[str]:
    configs = (network.get("IPAM") or {}).get("Config") or []
    return [config["Gateway"] for config in configs if config.get("Gateway")]


def unused_address(network: dict) -> str:
    """The last host address that no member holds. Docker hands out the low addresses, so a
    smoke container that has come and gone leaves no neighbour entry here, which would make the
    attempt time out instead of finding the host unreachable."""
    owned = {
        ipaddress.ip_interface(member["IPv4Address"]).ip
        for member in (network.get("Containers") or {}).values()
    }
    subnet = ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
    return str(next(host for host in reversed(list(subnet.hosts())) if host not in owned))


def egress_targets(stack: Stack) -> EgressTargets:
    """The host looks up the addresses, because the container has no DNS and no Docker."""
    agent, egress = project_network(stack, "agent"), project_network(stack, "egress")
    return EgressTargets(
        provider=socket.gethostbyname(PROVIDER_HOST),
        docker0=gateways_of(inspect_network(stack, "bridge"))[0],
        internet=gateways_of(project_network(stack, "internet"))[0],
        lan=lan_address(),
        agent_free=unused_address(agent),
        egress_free=unused_address(egress),
        agent_gateways=gateways_of(agent),
        egress_gateways=gateways_of(egress),
    )


def verdict_of(held: bool) -> Verdict:
    return Verdict.HELD if held else Verdict.FAILED


def all_unreachable(codes: Mapping[str, str]) -> bool:
    return all(code in UNREACHABLE for code in codes.values())


def judge_egress(targets: EgressTargets, seen: dict, gateway: str) -> list[Cell]:
    tcp = ", ".join(f"{target}:443 {code}" for target, code in seen["public_tcp"].items())
    reached = ", ".join(
        f"{label} {getattr(targets, label)}:22 {code}" for label, code in seen["reach"].items()
    )
    udp = ", ".join(f"{PUBLIC_RESOLVER}:{port} {code}" for port, code in seen["udp"].items())
    no_gateway = not targets.agent_gateways and not targets.egress_gateways
    no_route = not seen["defaults"] and no_gateway and all_unreachable(seen["reach"])
    no_ipv6 = seen["ipv6_interfaces"] == ["lo"] and seen["ipv6_tcp"] in UNREACHABLE
    names = seen["outside"] == [] and seen["gateway"] == [gateway]
    return [
        Cell("E6 no TCP to the internet", verdict_of(all_unreachable(seen["public_tcp"])), tcp),
        Cell(
            "E7 no route out", verdict_of(no_route),
            f"default routes {seen['defaults']}; gateways of agent {targets.agent_gateways} "
            f"and egress {targets.egress_gateways}; TCP to {reached}",
        ),
        Cell(
            "E8 no IPv6", verdict_of(no_ipv6),
            f"IPv6 interfaces {seen['ipv6_interfaces']}; TCP over IPv6 {seen['ipv6_tcp']}",
        ),
        Cell("E9 no UDP", verdict_of(all_unreachable(seen["udp"])), udp),
        Cell(
            "E10 names", verdict_of(names),
            f"example.com resolves to {seen['outside']}; agent-egress to {seen['gateway']}",
        ),
    ]  # fmt: skip


def judge_listeners(listening: Mapping[str, Sequence[str]], gateway: str) -> Cell:
    """Only the gateway's TCP 443 and Docker's embedded resolver may listen."""
    others = [
        f"{protocol} {name}"
        for protocol, names in listening.items()
        for name in names
        if not name.startswith(f"{EMBEDDED_RESOLVER}:")
    ]
    return Cell(
        "E12 the gateway's listeners", verdict_of(others == [f"tcp {gateway}:443"]),
        f"listening: {others}",
    )  # fmt: skip


def egress_cells(stack: Stack) -> list[Cell]:
    targets = egress_targets(stack)
    service = stack.compose("ps", "-q", SERVICE).stdout.strip()
    inside = stack.compose(
        "exec",
        "-T",
        SERVICE,
        "python",
        "-I",
        "-",
        json.dumps(asdict(targets)),
        stdin=EGRESS_PROGRAM,
    )
    if inside.returncode != 0:
        return [Cell("E6-E10", Verdict.FAILED, f"the egress program exited {inside.returncode}")]
    cells = judge_egress(targets, json.loads(inside.stdout), stack.gateway)
    return [*cells, listeners_cell(stack, service)]


def listeners_cell(stack: Stack, service: str) -> Cell:
    """Read from a throwaway container in the gateway's network namespace: nobody, no capability."""
    gateway = stack.compose("ps", "-q", EGRESS_SERVICE).stdout.strip()
    image = stack.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", service]
    ).stdout.strip()
    listing = stack.run([
        "docker", "run", "--rm", "--network", f"container:{gateway}", "--read-only",
        "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--entrypoint", "python", image, "-I", "-c", LISTENERS_PROGRAM,
    ])  # fmt: skip
    try:
        listening = json.loads(listing.stdout) if listing.returncode == 0 else None
    except ValueError:
        listening = None
    if not isinstance(listening, dict):
        return Cell("E12 the gateway's listeners", Verdict.FAILED, SAMPLE_FAILED)
    return judge_listeners(listening, stack.gateway)


# --- the run ----------------------------------------------------------------------------------


def smoke_cells(
    stack: Stack, known: Secrets, report: Report, pause: Callable[[float], None] = time.sleep
) -> None:
    """Recreate, idle memory, the paid turn, the peak, the cancelled turn, the searches."""
    recreated = recreate_service(stack, known, pause)
    report.add(recreated)
    if recreated.verdict is Verdict.FAILED:
        return
    idle = cgroup_figure(stack, "memory.current")
    paid = paid_turn(stack, known.tokens)
    report.add(*turn_cells(paid.returncode, paid.stdout, known))
    report.add(memory_cell(idle, cgroup_figure(stack, "memory.peak")))
    before = cli_processes(stack)
    cancelled = cancelled_turn(stack, known.tokens)
    report.add(*cancel_cells(stack, before, cancelled, known, pause))
    report.add(*leak_cells(stack, known, paid.stdout + paid.stderr + cancelled.output))


def live_smoke(stack: Stack, known: Secrets, report: Report) -> int:
    smoke_cells(stack, known, report)
    report.add(*egress_cells(stack))
    return 1 if report.failed() else 0


def read_secret(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise LiveSmokeFailed(f"cannot read {what} at {path}") from None


def main(argv: Sequence[str]) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        client_secret = read_secret(CLIENT_SECRET_FILE, "the client secret")
        login_token = read_secret(LOGIN_TOKEN_FILE, "the login token")
        known = Secrets(sign_in(client_secret), login_token, client_secret)
        return live_smoke(Stack(), known, Report(known.values()))
    except LiveSmokeFailed as failure:
        print(f"live smoke: {failure}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("live smoke: a docker command ran out of time", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("live smoke: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
