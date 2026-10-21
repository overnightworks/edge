#!/usr/bin/env python3
"""The live smoke of the agent service in one command (README "The agent service", "The smoke").

    cd ~/git/edge && python3 agent-service/live_smoke.py [--project edge] [--gateway 172.31.252.10]

You sign in as yourself (the `songmaker` client's code flow with PKCE): open the printed
address, sign in, and paste the address of the page that then fails; it is read without echo.
The person's and the workload's token live in this process only and reach `agent-smoke`
on its stdin; no token or secret is on a command line, in a file or in the report.
The run recreates the service, takes the idle
memory, runs the paid turn (L2, L3), the memory peak (L7), the cancelled turn (L4, counts only), the
searches for a leaked secret (L6) and the egress cells of the running container (L5: E6-E10, E12).
Exit 1 when a cell prints FAILED; L4 is an observation and never fails the run.

The in-container smoke is `smoke.py`, used as it is. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from smoke import HOLDING_MESSAGE, Tokens

OIDC = "https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect"
CLIENT = "songmaker"
REDIRECT = "https://songmaker.hallucinai.de/auth/callback"
SECRETS_DIRECTORY = Path.home() / ".secrets"
CLIENT_SECRET_FILE = SECRETS_DIRECTORY / "keycloak/clients" / CLIENT
LOGIN_TOKEN_FILE = SECRETS_DIRECTORY / "agent-service/claude/token"
DEFAULT_PROJECT = "edge"
DEFAULT_GATEWAY = "172.31.252.10"
SERVICE = "agent-service"
SMOKE_SERVICE = "agent-smoke"
EGRESS_SERVICE = "agent-egress"
LISTENING = "Uvicorn running"
LISTEN_ATTEMPTS = 120
SETTLE_SECONDS = 5
TOKEN_TIMEOUT_SECONDS = 30
SAMPLE_FAILED = "SAMPLE FAILED"
UNREACHABLE = {"ENETUNREACH", "EHOSTUNREACH", "EADDRNOTAVAIL"}
EMBEDDED_RESOLVER = "127.0.0.11"
PROVIDER_HOST = "api.anthropic.com"
PUBLIC_RESOLVER = "1.1.1.1"
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


@dataclass(frozen=True)
class Stack:
    """One compose project, the address of its egress gateway and how docker is asked.

    `compose_prefix` is everything before the compose subcommand; the probe passes its own."""

    project: str = DEFAULT_PROJECT
    gateway: str = DEFAULT_GATEWAY
    compose_prefix: tuple[str, ...] = ()
    environment: Mapping[str, str] | None = None
    directory: Path | None = None

    def prefix(self) -> list[str]:
        return list(
            self.compose_prefix
            or ("docker", "compose", "-p", self.project, "--profile", "agent-service")
        )

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
        return self.run([*self.prefix(), *arguments], stdin, timeout)

    def start_compose(self, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*self.prefix(), *arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=self.environment, cwd=self.directory,
        )  # fmt: skip


@dataclass
class Report:
    """Prints each cell at once, with every known secret removed from the line."""

    secret_values: list[str]
    cells: list[Cell] = field(default_factory=list)
    out: Callable[[str], None] = print

    def add(self, *cells: Cell) -> None:
        for cell in cells:
            self.cells.append(cell)
            line = cell.line()
            for value in self.secret_values:
                line = line.replace(value, "[secret]")
            self.out(line)

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
    except (OSError, ValueError, KeyError):
        raise LiveSmokeFailed("the token endpoint gave no access token") from None


def sign_in(
    client_secret: str,
    ask: Callable[..., str] = getpass.getpass,
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


def turn_cells(returncode: int, output: str) -> list[Cell]:
    events = event_names(output)
    said = f"exit {returncode}; events {events}; {other_lines(output)}"
    paid = returncode == 0 and events[-1:] == ["succeeded"]
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


def recreate_service(stack: Stack, pause: Callable[[float], None] = time.sleep) -> Cell:
    stack.compose("up", "-d", "--force-recreate", SERVICE)
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


def paid_turn(stack: Stack, tokens: Tokens) -> subprocess.CompletedProcess[str]:
    return stack.compose(
        "run", "--rm", "-T", SMOKE_SERVICE, stdin=f"{tokens.person}\n{tokens.workload}\n"
    )


@dataclass(frozen=True)
class CancelledTurn:
    returncode: int
    output: str
    during: str
    since: str


def cancelled_turn(stack: Stack, tokens: Tokens) -> CancelledTurn:
    """Starts the cancelling smoke and counts the CLI processes while it holds its stream open."""
    since = datetime.now(UTC).strftime(TIME_FORMAT)
    process = stack.start_compose("run", "--rm", "-T", SMOKE_SERVICE, "--cancel")
    try:
        process.stdin.write(f"{tokens.person}\n{tokens.workload}\n")
        process.stdin.close()
    except BrokenPipeError:
        pass  # the smoke ended first; its output and exit code say why
    during = "no hold seen"
    lines = []
    for line in process.stdout:
        lines.append(line)
        if HOLDING_MESSAGE in line:
            during = cli_processes(stack)
    return CancelledTurn(process.wait(), "".join(lines), during, since)


def cancel_cells(
    stack: Stack, before: str, turn: CancelledTurn, pause: Callable[[float], None] = time.sleep
) -> list[Cell]:
    pause(SETTLE_SECONDS)
    after = cli_processes(stack)
    logs = stack.compose("logs", "--since", turn.since, SERVICE)
    errors = lines_holding(logs.stdout + logs.stderr, ["ERROR"])
    return [
        Cell(
            "L4 the cancelled turn", Verdict.OBSERVED,
            f"smoke exit {turn.returncode}; CLI processes before (0) {before}, while held "
            f"(1 or more) {turn.during}, left after the cancel (0) {after}; "
            f"ERROR lines since (0) {errors}; {other_lines(turn.output)}",
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


EGRESS_PROGRAM = """\
import errno, json, socket, sys

targets = json.loads(sys.argv[1])
public_v4, public_v6 = "1.1.1.1", "2606:4700:4700::1111"

def name(code):
    return errno.errorcode.get(code, str(code))

def tcp(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as attempt:
        attempt.settimeout(8)
        return name(attempt.connect_ex((address, port)))

def udp(port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as attempt:
        try:
            attempt.connect((public_v4, port))
        except OSError as error:
            return name(error.errno)
    return "0"

def resolved(host):
    try:
        return sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443)})
    except socket.gaierror:
        return []

with open("/proc/net/route") as table:
    rows = [row.split() for row in table.read().splitlines()[1:]]
    defaults = [row[0] for row in rows if row[1] == "00000000"]
with open("/proc/net/if_inet6") as table:
    interfaces = sorted({row.split()[-1] for row in table if row.strip()})
print(json.dumps({
    "public_tcp": {target: tcp(target, 443) for target in (public_v4, targets["provider"])},
    "defaults": defaults,
    "reach": {label: tcp(targets[label], 22) for label in
              ("docker0", "internet", "lan", "agent_free", "egress_free")},
    "ipv6_interfaces": interfaces,
    "ipv6_tcp": tcp(public_v6, 443),
    "udp": {str(port): udp(port) for port in (443, 53)},
    "outside": resolved("example.com"),
    "gateway": resolved("agent-egress"),
}))
"""

LISTENERS_PROGRAM = """\
import socket
for table in ("tcp", "udp", "tcp6", "udp6"):
    for row in open(f"/proc/net/{table}").read().splitlines()[1:]:
        fields = row.split()
        if table.startswith("tcp") and fields[3] != "0A":
            continue
        address, port = fields[1].split(":")
        raw = bytes.fromhex(address)
        if table.endswith("6"):
            words = (raw[i:i + 4][::-1] for i in range(0, 16, 4))
            shown = socket.inet_ntop(socket.AF_INET6, b"".join(words))
        else:
            shown = socket.inet_ntoa(raw[::-1])
        print(table, shown, int(port, 16))
"""


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


def lan_address() -> str:
    """The address the host would use to reach the internet; a UDP connect sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((PUBLIC_RESOLVER, 53))
        return route.getsockname()[0]


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


def judge_listeners(output: str, gateway: str) -> Cell:
    """Only the gateway's TCP 443 and Docker's embedded resolver may listen."""
    rows = [line.split() for line in output.splitlines() if line.strip()]
    others = [row for row in rows if row[1] != EMBEDDED_RESOLVER]
    held = others == [["tcp", gateway, "443"]]
    return Cell(
        "E12 the gateway's listeners", Verdict.HELD if held else Verdict.FAILED,
        f"listening: {[' '.join(row) for row in others]}",
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
    if listing.returncode != 0:
        return Cell("E12 the gateway's listeners", Verdict.FAILED, SAMPLE_FAILED)
    return judge_listeners(listing.stdout, stack.gateway)


# --- the run ----------------------------------------------------------------------------------


def smoke_cells(
    stack: Stack, known: Secrets, report: Report, pause: Callable[[float], None] = time.sleep
) -> None:
    """Recreate, idle memory, the paid turn, the peak, the cancelled turn, the searches."""
    recreated = recreate_service(stack, pause)
    report.add(recreated)
    if recreated.verdict is Verdict.FAILED:
        return
    idle = cgroup_figure(stack, "memory.current")
    paid = paid_turn(stack, known.tokens)
    report.add(*turn_cells(paid.returncode, paid.stdout))
    report.add(memory_cell(idle, cgroup_figure(stack, "memory.peak")))
    before = cli_processes(stack)
    cancelled = cancelled_turn(stack, known.tokens)
    report.add(*cancel_cells(stack, before, cancelled, pause))
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    arguments = parser.parse_args(argv)
    try:
        client_secret = read_secret(CLIENT_SECRET_FILE, "the client secret")
        login_token = read_secret(LOGIN_TOKEN_FILE, "the login token")
        known = Secrets(sign_in(client_secret), login_token, client_secret)
        stack = Stack(project=arguments.project, gateway=arguments.gateway)
        return live_smoke(stack, known, Report(known.values()))
    except LiveSmokeFailed as failure:
        print(f"live smoke: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
