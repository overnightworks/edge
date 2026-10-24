#!/usr/bin/env python3
"""The live signer proof of the agent service (docs/agent-service.md, "Proofs").

    cd ~/git/edge && python3 agent-service/live_signer.py

It proves what the service signs for one tool, through songmaker's own client and the real
gateway, with no paid turn: both runs fail before any CLI starts. You sign in as for
`live_smoke.py`. The command recreates the service, then runs `agent-smoke` with one tool:
L4a with no backend behind the gateway (the run must end `failed` with no text, the gateway
having refused), L4b with the capture verifier `mcp_signer.py` at songmaker-web's place on the MCP
network (the received capability must verify against the live public.pem). Only the verdict is
printed, never the capability. The stand-in is always removed, and the command refuses while
anything but traefik-mcp is on the network: from then on songmaker's own call is the proof.
It needs `[apps.songmaker] mcp_origin` in agent-service/service.toml.
Exit 1 when a cell prints FAILED. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from device_sign_in import SignInFailed, sign_in
from live_smoke import (
    CLIENT_SECRET_FILE,
    LOGIN_TOKEN_FILE,
    RECREATE_REASON_CHARACTERS,
    REFUSAL_LINE,
    SECRETS_DIRECTORY,
    SERVICE,
    SERVICE_TOML,
    TIME_FORMAT,
    Cell,
    LiveSmokeFailed,
    Report,
    Secrets,
    Stack,
    Verdict,
    cleanup_cell,
    end_child,
    event_names,
    last_line,
    other_lines,
    paid_turn,
    project_network,
    read_secret,
    recreate_service,
)
from mcp_signer import LISTENING_LINE, PUBLIC_KEY_MOUNT, SCRIPT_MOUNT, VERIFIED
from signing_key import PUBLIC_FILE, SIGNING_DIRECTORY
from smoke import Tokens

PUBLIC_KEY = SECRETS_DIRECTORY / SERVICE / SIGNING_DIRECTORY / PUBLIC_FILE
STANDIN_SCRIPT = Path(__file__).resolve().with_name("mcp_signer.py")
PROOF_TOOL = "capability_check"
SMOKE_ARGUMENTS = ("--tool", f"{PROOF_TOOL}:read_only")
GATEWAY_SERVICE = "traefik-mcp"
GATEWAY_PROFILE = "step-ca"
MCP_NETWORK = "mcp-songmaker"
STANDIN_ALIAS = "songmaker-web"
LISTEN_SECONDS = 60
VERDICT_SECONDS = 15
REFUSED_BY_GATEWAY = "UpstreamRefused"
UNREACHABLE_GATEWAY = "UpstreamUnreachable"
NO_TEXT_FAILURE = "provider_error"


def mcp_origin(service_toml: Path) -> str:
    songmaker = tomllib.loads(service_toml.read_text(encoding="utf-8"))["apps"]["songmaker"]
    if "mcp_origin" not in songmaker:
        raise LiveSmokeFailed(
            "service.toml has no [apps.songmaker] mcp_origin; it comes with #346 slice C, "
            "so there is nothing to prove yet"
        )
    return songmaker["mcp_origin"]


def standin_name(project: str) -> str:
    return f"{project}-mcp-signer"


def standin_command(
    project: str, network: str, image: str, origin: str, tool: str = PROOF_TOOL
) -> list[str]:
    """Hardened like the listeners' container; only the key's public half goes in, read-only."""
    return [
        "docker", "run", "--rm", "--name", standin_name(project), "--read-only",
        "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--network", network, "--network-alias", STANDIN_ALIAS,
        "-v", f"{STANDIN_SCRIPT}:{SCRIPT_MOUNT}:ro", "-v", f"{PUBLIC_KEY}:{PUBLIC_KEY_MOUNT}:ro",
        "--entrypoint", "python", image, "-I", "-B", SCRIPT_MOUNT, origin, tool,
    ]  # fmt: skip


def occupied_network_cell(stack: Stack) -> Cell | None:
    """FAILED while any container but traefik-mcp's is on songmaker's MCP network."""
    listed = stack.compose("--profile", GATEWAY_PROFILE, "ps", "-q", GATEWAY_SERVICE)
    if listed.returncode != 0:
        return Cell(
            "L4 the MCP network", Verdict.FAILED, f"docker compose ps {GATEWAY_SERVICE} failed"
        )
    gateway = listed.stdout.split()
    members = project_network(stack, MCP_NETWORK).get("Containers") or {}
    others = [member for member in members if member not in gateway]
    if not others:
        return None
    return Cell(
        "L4 the MCP network", Verdict.FAILED,
        f"{len(others)} container(s) besides {GATEWAY_SERVICE} are on it; songmaker's own "
        "call is the proof now",
    )  # fmt: skip


def failed_without_text(events: list[str], text: str) -> bool:
    return events == ["run_started", "failed"] and text.startswith(NO_TEXT_FAILURE)


def l4a_cell(output: str, service_log: str, known: Secrets) -> Cell:
    output = known.redact(output)
    events, text = event_names(output), other_lines(output)
    said = f"events {events}; {text}"
    refusal = None if events else REFUSAL_LINE.search(output)
    if refusal and refusal["code"] == "forbidden":
        return Cell(
            "L4a no backend", Verdict.FAILED, f"forbidden: the service lacks the origin; {said}"
        )
    if {"succeeded", "assistant_text"} & set(events):
        return Cell("L4a no backend", Verdict.FAILED, f"a fallback answered; {said}")
    if UNREACHABLE_GATEWAY in service_log:
        return Cell(
            "L4a no backend", Verdict.FAILED, f"the gateway was unreachable (TLS or route); {said}"
        )
    held = failed_without_text(events, text) and REFUSED_BY_GATEWAY in service_log
    return Cell(
        "L4a no backend", Verdict.HELD if held else Verdict.FAILED,
        f"the gateway refused and the run failed without text; {said}" if held
        else f"no {REFUSED_BY_GATEWAY} with a failed run; {said}",
    )  # fmt: skip


def l4b_cell(verdict: str | None, output: str, known: Secrets) -> Cell:
    output = known.redact(output)
    events, text = event_names(output), other_lines(output)
    if verdict is None:
        return Cell("L4b the capability", Verdict.FAILED, "no verdict from the stand-in")
    if verdict != VERIFIED:
        return Cell("L4b the capability", Verdict.FAILED, f"the stand-in {verdict}")
    if not failed_without_text(events, text):
        return Cell(
            "L4b the capability", Verdict.FAILED, f"verified, but the run went events {events}"
        )
    return Cell(
        "L4b the capability",
        Verdict.HELD,
        "verified against public.pem; the run failed without text",
    )


def read_line(process: subprocess.Popen[str], seconds: float) -> str | None:
    """The next line of the child, or None at its end or after `seconds`."""
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        return lines.get(timeout=seconds).strip() or None
    except queue.Empty:
        return None


def run_smoke(stack: Stack, known: Secrets) -> tuple[str, str, list[Cell]]:
    """The smoke's output, the service log since it began, and its clean-up cell."""
    since = datetime.now(UTC).strftime(TIME_FORMAT)
    turn = paid_turn(stack, known.tokens, smoke_arguments=SMOKE_ARGUMENTS)
    logs = stack.compose("logs", "--since", since, SERVICE)
    log = logs.stdout + logs.stderr if logs.returncode == 0 else "SAMPLE FAILED"
    return turn.process.stdout, log, cleanup_cell(turn.cleanup, known)


def standin_is_gone(stack: Stack, name: str) -> bool:
    """`--rm` may have removed the stand-in already; only a confirmed absence counts."""
    listed = stack.run(["docker", "ps", "-a", "-q", "--filter", f"name=^{name}$"])
    return listed.returncode == 0 and not listed.stdout.strip()


def remove_standin(stack: Stack, name: str, known: Secrets) -> list[Cell]:
    removed = stack.run(["docker", "rm", "-f", name])
    if removed.returncode == 0 or standin_is_gone(stack, name):
        return []
    reason = known.redact(last_line(removed.stderr))[:RECREATE_REASON_CHARACTERS]
    return [
        Cell(
            "cleanup of the stand-in",
            Verdict.FAILED,
            f"docker rm exited {removed.returncode}: {reason}",
        )
    ]


def standin_cells(stack: Stack, known: Secrets, report: Report, origin: str) -> None:
    if busy := occupied_network_cell(stack):
        return report.add(busy)
    network = project_network(stack, MCP_NETWORK)["Name"]
    image = stack.configuration["services"][SERVICE]["image"]
    process = stack.start(standin_command(stack.project, network, image, origin))
    try:
        if read_line(process, LISTEN_SECONDS) != LISTENING_LINE:
            report.add(
                Cell("L4b the capability", Verdict.FAILED, "the stand-in printed no listening line")
            )
            return
        output, _, cleanup = run_smoke(stack, known)
        report.add(l4b_cell(read_line(process, VERDICT_SECONDS), output, known), *cleanup)
    finally:
        end_child(process)
        report.add(*remove_standin(stack, standin_name(stack.project), known))


def live_signer(
    stack: Stack, known: Secrets, report: Report, origin: str,
    pause: Callable[[float], None] = time.sleep,
) -> int:  # fmt: skip
    if busy := occupied_network_cell(stack):
        report.add(busy)
        return 1
    recreated = recreate_service(stack, known, pause)
    report.add(recreated)
    if recreated.verdict is Verdict.FAILED:
        return 1
    output, log, cleanup = run_smoke(stack, known)
    report.add(l4a_cell(output, log, known), *cleanup)
    standin_cells(stack, known, report, origin)
    return 1 if report.failed() else 0


def main(
    argv: Sequence[str],
    service_toml: Path = SERVICE_TOML,
    sign_in_with: Callable[[str], Tokens] = sign_in,
    stack_of: Callable[[], Stack] = Stack,
) -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        origin = mcp_origin(service_toml)
        stack = stack_of()
        if busy := occupied_network_cell(stack):
            Report([]).add(busy)  # before the sign-in, so that nothing secret exists yet
            return 1
        client_secret = read_secret(CLIENT_SECRET_FILE, "the client secret")
        login_token = read_secret(LOGIN_TOKEN_FILE, "the login token")
        known = Secrets(sign_in_with(client_secret), login_token, client_secret)
        return live_signer(stack, known, Report(known.values()), origin)
    except (LiveSmokeFailed, SignInFailed) as failure:
        print(f"live signer: {failure}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("live signer: a docker command ran out of time", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("live signer: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
