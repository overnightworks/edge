"""The live signer proof (`agent-service/live_signer.py`) against a scripted docker."""

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))

import live_signer  # noqa: E402
from live_smoke import LiveSmokeFailed, Report, Verdict  # noqa: E402
from live_smoke_fakes import (  # noqa: E402
    KNOWN,
    SECRET_FILES,
    SERVICE_NETWORKS,
    TOKENS,
    FakeStack,
    completed,
    healthy_service,
    resolved_configuration,
)

# --- the live signer proof -------------------------------------------------------------

TOOL = live_signer.PROOF_TOOL
ORIGIN = "https://songmaker.mcp.internal"
NETWORK = "hal_mcp_songmaker"
FAILED_RUN = "event: run_started\nevent: failed\nprovider_error: the runner failed\n"


class FakeStandIn:
    """The stand-in container's `docker run` child: it has printed these lines and is gone."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = io.StringIO("".join(lines))
        self.stdin = io.StringIO()
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self) -> None:
        self.ended = True

    def wait(self, timeout=None) -> int:
        return 0


def signer_docker(
    members=("traefik-id",),
    smoke=FAILED_RUN,
    log="UpstreamRefused",
    removal=0,
    gone=False,
    gateway_listing=0,
    late_member=None,
    shared=None,
    joined=None,
    mounted=None,
    open_network=None,
):
    """A docker with the project's MCP network, traefik-mcp and the service's logs.

    `gone` is a stand-in `--rm` already removed; `late_member` joins the network once the first
    smoke has run. `shared` is the members of the live `hal_agent` network (None: it is gone),
    `joined` and `mounted` what the recreated container holds, `open_network` a service network
    that is not internal.
    """
    smokes = []
    joined = tuple(SERVICE_NETWORKS.values()) if joined is None else joined
    mounted = tuple(map(str, SECRET_FILES.values())) if mounted is None else mounted

    def network(key: str) -> dict:
        late = (late_member,) if late_member and smokes else ()
        present = {"mcp-songmaker": (*members, *late), "agent": shared or ()}.get(key, ())
        return {
            "Name": SERVICE_NETWORKS.get(key, NETWORK),
            "Internal": key != open_network,
            "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
            "Containers": {member: {} for member in present},
        }

    def answer(command: list[str]) -> subprocess.CompletedProcess:
        if "config" in command:
            document = resolved_configuration()
            document["networks"]["mcp-songmaker"] = {"name": NETWORK}
            return completed(json.dumps(document))
        if command[:2] == ["docker", "inspect"]:
            container = {
                "NetworkSettings": {"Networks": {name: {} for name in joined}},
                "Mounts": [
                    {"Destination": f"/run/secrets/{name}", "Source": source}
                    for name, source in zip(SECRET_FILES, mounted, strict=False)
                ],
            }
            return completed(json.dumps([container]))
        if command[:3] == ["docker", "network", "ls"]:
            key = next(a for a in command if "compose.network=" in a).rpartition("=")[2]
            gone_key = key == "agent" and shared is None
            return completed("" if gone_key else f"id-{key}\n")
        if command[:2] == ["docker", "ps"]:
            return completed("" if gone else "standin-id\n")
        if "traefik-mcp" in command:
            return completed("traefik-id\n", returncode=gateway_listing)
        if command[:3] == ["docker", "network", "inspect"]:
            return completed(json.dumps([network(command[3].removeprefix("id-"))]))
        if command[:2] == ["docker", "rm"]:
            return completed(returncode=removal, stderr=f"removal failed {TOKENS.person}")
        if "logs" in command and "--since" in command:
            return completed(log)
        if "agent-smoke" in command:
            smokes.append(command)
            if callable(smoke):
                return smoke()
            return completed(smoke, returncode=1)
        return healthy_service(command)

    return answer


def run_signer(stack: FakeStack, origin: str = ORIGIN) -> tuple[int, list[str]]:
    printed: list[str] = []
    report = Report(KNOWN.values(), out=printed.append)
    code = live_signer.live_signer(stack, KNOWN, report, origin, pause=lambda _: None)
    return code, printed


def standin_stack(lines=("listening\n", "verified\n"), **docker) -> FakeStack:
    return FakeStack(answer=signer_docker(**docker), standin=FakeStandIn(list(lines)))


def started_standins(stack: FakeStack) -> list[list[str]]:
    return [command for command in stack.asked if command[:2] == ["docker", "run"]]


def test_p39_the_standin_has_no_port_no_writable_mount_and_the_one_resolved_network():
    command = live_signer.standin_command("edge", NETWORK, "i:1", ORIGIN, TOOL)

    flags = [argument for argument in command if argument.startswith("-")]
    assert command[:2] == ["docker", "run"]
    assert {"--rm", "--read-only", "--cap-drop", "--security-opt"} <= set(flags)
    assert command[command.index("--name") + 1] == "edge-mcp-signer"
    assert command[command.index("--user") + 1] == "65534:65534"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert flags.count("--network") == 1
    assert command[command.index("--network") + 1] == NETWORK
    assert command[command.index("--network-alias") + 1] == "songmaker-web"
    assert not {"-p", "--publish", "-P", "--publish-all", "--privileged"} & set(flags)
    binds = [command[at + 1] for at, argument in enumerate(command) if argument == "-v"]
    assert len(binds) == 2
    assert all(bind.endswith(":ro") for bind in binds)
    sources = [bind.split(":")[0] for bind in binds]
    assert sources[1].endswith("signing/public.pem")
    assert not any(
        "private.pem" in bind or bind.split(":")[0].endswith("signing") for bind in binds
    )
    assert command[-2:] == [ORIGIN, TOOL]


def test_p40_a_member_besides_traefik_mcp_fails_before_any_recreate_stand_in_or_run():
    stack = standin_stack(members=("traefik-id", "someone-else"))

    code, printed = run_signer(stack)

    assert code == 1
    assert len(printed) == 1
    assert "FAILED" in printed[0]
    assert "songmaker's own" in printed[0]
    assert not any("up" in command or "agent-smoke" in command for command in stack.asked)
    assert not started_standins(stack)


def test_p40_a_failed_gateway_listing_is_named_not_mistaken_for_another_container():
    stack = standin_stack(gateway_listing=1)

    code, printed = run_signer(stack)

    assert code == 1
    assert len(printed) == 1
    assert "ps traefik-mcp failed" in printed[0]
    assert not started_standins(stack)


def test_p40_a_member_that_joins_during_l4a_stops_before_the_standin_starts():
    stack = standin_stack(late_member="late")

    code, printed = run_signer(stack)

    assert code == 1
    assert [line.split(" ")[0] for line in printed] == ["recreate", "L3", "L3", "L3", "L4a", "L4"]
    assert "FAILED" in printed[-1]
    assert "songmaker's own" in printed[-1]
    assert not started_standins(stack)


def test_p44_a_service_toml_without_the_mcp_origin_stops_naming_the_missing_origin(tmp_path):
    service = tmp_path / "service.toml"
    service.write_text('[apps.songmaker]\nworkload_client = "songmaker"\n', encoding="utf-8")

    with pytest.raises(LiveSmokeFailed, match=r"no \[apps.songmaker\] mcp_origin"):
        live_signer.mcp_origin(service)


def test_p44_the_missing_origin_stops_before_any_sign_in(tmp_path, capsys):
    service = tmp_path / "service.toml"
    service.write_text("[apps.songmaker]\n", encoding="utf-8")

    def signed_in(_: str):
        pytest.fail("signed in")

    def reached_docker():
        pytest.fail("reached the stack")

    code = live_signer.main(
        [], service_toml=service, sign_in_with=signed_in, stack_of=reached_docker
    )

    assert code == 1
    assert "mcp_origin" in capsys.readouterr().err


def test_p44_a_service_toml_with_the_mcp_origin_gives_it(tmp_path):
    service = tmp_path / "service.toml"
    service.write_text(f'[apps.songmaker]\nmcp_origin = "{ORIGIN}"\n', encoding="utf-8")

    assert live_signer.mcp_origin(service) == ORIGIN


@pytest.mark.parametrize(
    ("output", "log", "verdict", "named"),
    [
        (FAILED_RUN, "UpstreamRefused", Verdict.HELD, "the gateway refused"),
        (FAILED_RUN, "UpstreamUnreachable", Verdict.FAILED, "unreachable"),
        (FAILED_RUN, "nothing", Verdict.FAILED, "no UpstreamRefused"),
        (
            "event: run_started\nevent: assistant_text\nhi\nevent: succeeded\nhi\n",
            "UpstreamRefused",
            Verdict.FAILED,
            "a fallback answered",
        ),
        (
            "event: run_started\nevent: assistant_text\nhi\nevent: failed\nprovider_error: x\n",
            "UpstreamRefused",
            Verdict.FAILED,
            "a fallback answered",
        ),
        ("refused with 403 forbidden: no\n", "", Verdict.FAILED, "the service lacks the origin"),
    ],
)
def test_p42_l4a_holds_only_on_a_failed_run_without_text_refused_by_the_gateway(
    output, log, verdict, named
):
    cell = live_signer.l4a_cell(output, log, KNOWN)

    assert cell.verdict is verdict
    assert named in cell.figure
    assert cell.name.startswith("L4a")


@pytest.mark.parametrize(
    ("line", "output", "verdict", "named"),
    [
        ("verified", FAILED_RUN, Verdict.HELD, ""),
        ("refused: invalid_signature", FAILED_RUN, Verdict.FAILED, "invalid_signature"),
        (None, FAILED_RUN, Verdict.FAILED, "no verdict"),
        ("verified", "event: run_started\nevent: succeeded\nhi\n", Verdict.FAILED, "succeeded"),
    ],
)
def test_p43_l4b_holds_on_a_verdict_and_a_failed_run_and_names_what_it_refused(
    line, output, verdict, named
):
    cell = live_signer.l4b_cell(line, output, KNOWN)

    assert cell.verdict is verdict
    assert named in cell.figure


def test_p43_a_clean_proof_recreates_runs_l4a_then_l4b_and_removes_the_standin():
    stack = standin_stack()

    code, printed = run_signer(stack)

    assert code == 0
    assert [line.split(" ")[0] for line in printed] == ["recreate", "L3", "L3", "L3", "L4a", "L4b"]
    commands = [" ".join(command) for command in stack.asked]
    recreated = next(at for at, text in enumerate(commands) if "up -d --force-recreate" in text)
    first_run = next(at for at, text in enumerate(commands) if "--tool" in text)
    started = next(at for at, text in enumerate(commands) if text.startswith("docker run"))
    assert recreated < first_run < started
    assert all(
        f"--tool {TOOL}:read_only" in text
        for text in commands
        if "agent-smoke" in text and "run --rm" in text and "--filter" not in text
    )
    assert ["docker", "rm", "-f", "edge-mcp-signer"] in stack.asked
    assert stack.standin.ended


@pytest.mark.parametrize("case", ["no verdict", "refused", "run succeeded"])
def test_p41_the_standin_is_removed_by_its_exact_name_whatever_the_verdict(case):
    lines = {"no verdict": ["listening\n"], "refused": ["listening\n", "refused: kid\n"]}
    stack = standin_stack(lines=lines.get(case, ["listening\n", "verified\n"]))
    if case == "run succeeded":
        stack = standin_stack(smoke="event: run_started\nevent: succeeded\nhi\n")

    code, _ = run_signer(stack)

    assert code == 1
    assert ["docker", "rm", "-f", "edge-mcp-signer"] in stack.asked
    assert stack.standin.ended


def test_p41_a_standin_that_is_gone_by_itself_is_a_clean_removal():
    code, printed = run_signer(standin_stack(removal=1, gone=True))

    assert code == 0
    assert not [line for line in printed if line.startswith("cleanup of the stand-in")]


def test_p41_a_standin_that_prints_nothing_gives_no_verdict_at_the_deadline():
    read_end, write_end = os.pipe()
    silent = os.fdopen(read_end)
    reader_done = threading.Event()

    class ObservedStream:
        def readline(self) -> str:
            try:
                return silent.readline()
            finally:
                reader_done.set()

    process = FakeStandIn([])
    process.stdout = ObservedStream()

    answers: list[str | None] = []
    caller = threading.Thread(target=lambda: answers.append(live_signer.read_line(process, 0.01)))
    caller.start()
    caller.join(1)

    finished = not caller.is_alive()
    os.close(write_end)  # the reader thread sees end of file and ends
    caller.join(1)
    assert reader_done.wait(1)  # the abandoned reader has returned before the stream closes
    silent.close()
    assert finished
    assert answers == [None]


def test_p41_a_smoke_the_host_gave_up_on_still_removes_the_standin():
    def hanging():
        raise subprocess.TimeoutExpired("smoke", 1)

    stack = standin_stack(smoke=hanging)

    code, _ = run_signer(stack)

    assert code == 1
    assert ["docker", "rm", "-f", "edge-mcp-signer"] in stack.asked


def test_p41_an_interrupt_during_the_second_run_still_removes_the_standin():
    runs = []

    def interrupted():
        runs.append(1)
        if len(runs) == 2:
            raise KeyboardInterrupt
        return completed(FAILED_RUN, returncode=1)

    stack = standin_stack(smoke=interrupted)

    with pytest.raises(KeyboardInterrupt):
        run_signer(stack)

    assert ["docker", "rm", "-f", "edge-mcp-signer"] in stack.asked
    assert stack.standin.ended


def test_p41_a_removal_that_failed_is_a_failed_cleanup_cell_without_the_secret():
    code, printed = run_signer(standin_stack(removal=1))

    assert code == 1
    cleanup = [line for line in printed if line.startswith("cleanup of the stand-in")]
    assert len(cleanup) == 1
    assert "FAILED" in cleanup[0]
    assert TOKENS.person not in cleanup[0]


def test_p45_no_token_or_secret_reaches_a_printed_line():
    secrets = f"{TOKENS.workload} {KNOWN.login_token}"
    leaking = f"event: run_started\nevent: failed\nprovider_error: {secrets}\n"
    stack = standin_stack(smoke=leaking, log=f"UpstreamRefused {KNOWN.client_secret}")

    _, printed = run_signer(stack)

    assert printed
    assert not [line for line in printed for secret in KNOWN.values() if secret in line]


def l3_lines(stack: FakeStack) -> dict[str, str]:
    _, printed = run_signer(stack)
    return {line.split(" held")[0].split(" FAILED")[0]: line for line in printed if "L3" in line}


def test_l3a_holds_when_networks_and_mount_sources_equal_the_resolved_configuration():
    lines = l3_lines(standin_stack(shared=()))

    assert all(" held" in line for line in lines.values())
    assert set(lines) == {"L3 networks", "L3 mounts", "L3 shared network"}


@pytest.mark.parametrize(
    ("docker", "cell"),
    [
        ({"joined": ("hal_agent_songmaker", "edge_mcp", "edge_egress")}, "L3 networks"),
        ({"joined": (*SERVICE_NETWORKS.values(), "hal_agent")}, "L3 networks"),
        ({"open_network": "agent-presentator"}, "L3 networks"),
        ({"open_network": "mcp"}, "L3 networks"),
        (
            {"mounted": ("/home/x/.secrets/agent-service/signing", "/other/root_ca.crt")},
            "L3 mounts",
        ),
        ({"mounted": ("/home/x/.secrets/agent-service/signing/private.pem",)}, "L3 mounts"),
    ],
)
def test_l3a_fails_its_cell_and_stops_before_any_smoke_when_the_recreate_differs(docker, cell):
    stack = standin_stack(**docker)

    code, printed = run_signer(stack)

    assert code == 1
    assert next(line for line in printed if "FAILED" in line).startswith(cell)
    assert not any("agent-smoke" in " ".join(command) for command in stack.asked)


def test_l3b_an_empty_shared_network_is_found_by_its_compose_labels_and_removed():
    stack = standin_stack(shared=())

    run_signer(stack)

    listing = next(c for c in stack.asked if "label=com.docker.compose.network=agent" in c)
    assert listing[:3] == ["docker", "network", "ls"]
    assert "label=com.docker.compose.project=edge" in listing
    assert ["docker", "network", "rm", "id-agent"] in stack.asked


def test_l3b_a_shared_network_with_members_fails_and_nothing_is_removed():
    stack = standin_stack(shared=("someone",))

    code, printed = run_signer(stack)

    assert code == 1
    assert any(line.startswith("L3 shared network") and "FAILED" in line for line in printed)
    assert not any(command[:3] == ["docker", "network", "rm"] for command in stack.asked)


def test_l3b_an_absent_shared_network_is_held_and_nothing_is_removed():
    stack = standin_stack(shared=None)

    code, printed = run_signer(stack)

    assert code == 0
    assert any(line.startswith("L3 shared network") and " held" in line for line in printed)
    assert not any(command[:3] == ["docker", "network", "rm"] for command in stack.asked)
