"""The live smoke command (`agent-service/live_smoke.py`, #221): the report, the cells and the
no-secret rule against a scripted docker. The command against a running service is driven by
probe/agent-service/unit.py (cell U16) and by the operator.
"""

import io
import json
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))

import live_signer  # noqa: E402
import live_smoke  # noqa: E402
from live_smoke import Cell, LiveSmokeFailed, Report, Secrets, Stack, Verdict  # noqa: E402
from smoke import Tokens  # noqa: E402

TOKENS = Tokens(person="person-token-0123456789", workload="workload-token-9876543210")
KNOWN = Secrets(TOKENS, login_token="login-token-abcdef", client_secret="client-secret-fedcba")
GATEWAY = "172.31.252.10"


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- the report -------------------------------------------------------------------------------


def test_the_report_removes_every_known_secret_from_a_line():
    printed: list[str] = []
    report = Report(KNOWN.values(), out=printed.append)

    report.add(Cell("L2", Verdict.HELD, f"the answer held {TOKENS.person} and {KNOWN.login_token}"))

    assert printed == ["L2 held: the answer held [secret] and [secret]"]


def test_one_failed_cell_fails_the_run_and_an_observation_does_not():
    report = Report([], out=lambda _: None)
    report.add(Cell("L4", Verdict.OBSERVED, "x"), Cell("L2", Verdict.HELD, "x"))
    assert not report.failed()

    report.add(Cell("L6", Verdict.FAILED, "x"))
    assert report.failed()


@pytest.mark.parametrize(
    ("returncode", "output", "paid", "admitted"),
    [
        (0, "event: run_started\nevent: assistant_text\nok\nevent: succeeded\nok\n", True, True),
        (0, "event: run_started\nevent: succeeded\n\n", False, True),
        (0, "event: run_started\nevent: assistant_text\n\nevent: succeeded\n", False, True),
        (0, "event: run_started\nevent: assistant_text\n   \nevent: succeeded\n \n", False, True),
        (1, "event: run_started\nevent: failed\nprovider_error: x\n", False, True),
        (1, "refused with 401 unauthorized: expired\n", False, False),
    ],
)
def test_the_paid_turn_holds_only_on_succeeded_and_admission_on_run_started(
    returncode, output, paid, admitted
):
    l2, l3 = live_smoke.turn_cells(returncode, output, KNOWN)

    assert (l2.verdict is Verdict.HELD, l3.verdict is Verdict.HELD) == (paid, admitted)
    assert ("unauthorized" in l2.figure) == ("401" in output)


def test_a_forbidden_refusal_names_the_membership_and_the_token_binding():
    l2, _ = live_smoke.turn_cells(1, "refused with 403 forbidden: no\n", KNOWN)

    assert f"/apps/{live_smoke.CLIENT}" in l2.figure
    assert "azp" in l2.figure


def test_a_not_enabled_refusal_names_the_subscription_groups_of_the_service_toml():
    l2, _ = live_smoke.turn_cells(1, "refused with 403 not_enabled: no\n", KNOWN)

    service = tomllib.loads((ROOT / "agent-service/service.toml").read_text(encoding="utf-8"))
    assert service["identity"]["subscription_groups"]
    assert all(group in l2.figure for group in service["identity"]["subscription_groups"])


@pytest.mark.parametrize("code", ["forbidden", "not_enabled"])
def test_an_answer_line_shaped_like_a_refusal_holds_and_gets_no_hint(code):
    answer = f"refused with 403 {code}: no"
    output = f"event: run_started\nevent: assistant_text\n{answer}\nevent: succeeded\n"

    l2, _ = live_smoke.turn_cells(0, output, KNOWN)

    assert l2.verdict is Verdict.HELD
    assert "/apps/" not in l2.figure
    assert "subscription" not in l2.figure


# --- the service, against a scripted docker ---------------------------------------------------


class FakeProcess:
    """The cancelling smoke: prints its lines, then ends, hangs until killed, or is interrupted."""

    def __init__(
        self, lines: list[str], returncode: int = 0, hangs: bool = False, interrupted: bool = False
    ) -> None:
        self.lines, self.returncode = lines, returncode
        self.hangs, self.interrupted = hangs, interrupted
        self.stdin = io.StringIO()
        self.exited = threading.Event()
        self.terminated = self.killed = False

    @property
    def stdout(self):
        yield from self.lines
        if self.interrupted:
            raise KeyboardInterrupt
        if self.hangs:
            self.exited.wait(10)

    def poll(self):
        return self.returncode if self.exited.is_set() else None

    def wait(self, timeout=None) -> int:
        self.exited.set()
        return -9 if self.killed else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.exited.set()

    def kill(self) -> None:
        self.killed = True
        self.exited.set()


@dataclass(frozen=True)
class FakeStack(Stack):
    """A docker that answers by what is asked; `answer` gets the command after the prefix."""

    answer: Callable[[list[str]], subprocess.CompletedProcess] = lambda _: completed()
    cancelling: FakeProcess | None = None
    standin: object | None = None
    asked: list[list[str]] = field(default_factory=list)

    def run(self, command, stdin=None, timeout=300):
        self.asked.append([*command])
        return self.answer(list(command))

    def start(self, command):
        self.asked.append([*command])
        return self.standin if command[:2] == ["docker", "run"] else self.cancelling


def healthy_service(command: list[str]) -> subprocess.CompletedProcess:
    text = " ".join(command)
    if "config" in command:
        return completed(json.dumps({"name": "edge", "x-egress-address": GATEWAY}))
    if "cat /sys/fs/cgroup/memory" in text:
        return completed("1234\n")
    if "grep -c" in text:
        return completed("0\n")
    if "ps" in command:
        return completed("container-id\n")
    if "logs" in command:
        return completed("Uvicorn running on http://0.0.0.0:8080\n")
    if "agent-smoke" in command:
        return completed("event: run_started\nevent: assistant_text\nok\nevent: succeeded\nok\n")
    if "network" in command and "ls" in command:
        return completed("network-id\n")
    return completed("")


def held_cancel() -> FakeProcess:
    return FakeProcess(["event: run_started\n", "holding the stream open 3 s\n", "closing\n"])


def run_smoke_cells(stack: FakeStack) -> list[Cell]:
    report = Report(KNOWN.values(), out=lambda _: None)
    live_smoke.smoke_cells(stack, KNOWN, report, pause=lambda _: None)
    return report.cells


def test_a_clean_run_holds_l2_l3_l6_and_l7_and_only_observes_l4():
    stack = FakeStack(answer=healthy_service, cancelling=held_cancel())

    cells = {cell.name.split()[0]: cell for cell in run_smoke_cells(stack)}

    assert [cells[name].verdict for name in ("L2", "L3", "L6", "L7")] == [Verdict.HELD] * 4
    assert cells["L4"].verdict is Verdict.OBSERVED
    assert "idle memory.current 1234" in cells["L7"].figure


def test_the_tokens_go_to_the_smoke_on_stdin_and_on_no_command_line():
    stack = FakeStack(answer=healthy_service, cancelling=held_cancel())

    run_smoke_cells(stack)

    commands = " ".join(" ".join(command) for command in stack.asked)
    assert all(secret not in commands for secret in KNOWN.values())
    assert stack.cancelling.stdin.closed


def test_a_service_that_never_listens_fails_the_recreate_and_goes_no_further():
    def silent(command):
        return completed("starting\n") if "logs" in command else completed()

    stack = FakeStack(answer=silent)

    cells = run_smoke_cells(stack)

    assert [(cell.name, cell.verdict) for cell in cells] == [("recreate", Verdict.FAILED)]
    assert not any("agent-smoke" in command for command in stack.asked)


@pytest.mark.parametrize("place", ["docker inspect", "logs", "smoke"])
def test_a_secret_in_any_searched_place_fails_l6_and_names_the_place(place):
    def leaking(command):
        if place == "docker inspect" and "inspect" in command:
            return completed(f'"Env": ["X={KNOWN.client_secret}"]')
        if place == "logs" and "logs" in command:
            return completed(f"token {TOKENS.workload}")
        if "ps" in command:
            return completed("container-id\n")
        return completed("")

    stack = FakeStack(answer=leaking)
    smoke_output = f"oops {KNOWN.login_token}" if place == "smoke" else ""

    (cell,) = live_smoke.leak_cells(stack, KNOWN, smoke_output)

    assert cell.verdict is Verdict.FAILED
    searched = {"docker inspect": "docker inspect", "logs": "the service log", "smoke": "the smoke"}
    assert f"{searched[place]} 1 (0)" in cell.figure.replace(" output", "")
    assert all(secret not in cell.figure for secret in KNOWN.values())


def test_a_search_that_could_not_run_prints_sample_failed_never_zero():
    def broken(command):
        return completed("", returncode=1)

    (cell,) = live_smoke.leak_cells(FakeStack(answer=broken), KNOWN, "")

    assert cell.verdict is Verdict.FAILED
    assert "docker inspect SAMPLE FAILED" in cell.figure
    assert "the service log SAMPLE FAILED" in cell.figure


def test_a_cli_count_that_could_not_be_read_prints_sample_failed():
    stack = FakeStack(answer=lambda _: completed("not a number", returncode=0))

    assert live_smoke.cli_processes(stack) == "SAMPLE FAILED"


def with_leftover(command: list[str]) -> subprocess.CompletedProcess:
    if command[:2] == ["docker", "ps"]:
        return completed("leftover\n")
    return healthy_service(command)


def asked_commands(stack: FakeStack) -> list[str]:
    return [" ".join(command) for command in stack.asked]


@pytest.mark.parametrize("secret", KNOWN.values())
def test_a_secret_across_the_cut_of_the_paid_answer_leaves_no_beginning(secret):
    output = f"event: run_started\nevent: succeeded\n{'x' * 290}{secret}\n"

    l2, _ = live_smoke.turn_cells(0, output, KNOWN)

    assert secret[:8] not in l2.figure


@pytest.mark.parametrize("secret", KNOWN.values())
def test_a_secret_across_the_cut_of_the_cancelled_answer_leaves_no_beginning(secret):
    lines = ["event: run_started\n", "holding the stream open 3 s\n", f"{'x' * 290}{secret}\n"]
    stack = FakeStack(answer=healthy_service, cancelling=FakeProcess(lines))

    turn = live_smoke.cancelled_turn(stack, TOKENS)
    (cell,) = live_smoke.cancel_cells(stack, "0", turn, KNOWN, pause=lambda _: None)

    assert secret[:8] not in cell.figure


def test_a_log_that_failed_to_be_read_does_not_make_the_recreate_ready():
    def failing_logs(command):
        if "logs" in command:
            return completed("Uvicorn running on http://0.0.0.0:8080\n", returncode=1)
        return healthy_service(command)

    stack = FakeStack(answer=failing_logs)

    cells = run_smoke_cells(stack)

    assert [(cell.name, cell.verdict) for cell in cells] == [("recreate", Verdict.FAILED)]


@pytest.mark.parametrize("failing", ["ps", "rm"])
def test_a_cleanup_that_failed_is_reported_without_the_secret(failing):
    secret = KNOWN.client_secret

    def failing_cleanup(command):
        if command[:2] == ["docker", "ps"]:
            if failing == "ps":
                return completed("", returncode=1, stderr=f"no daemon {secret}\n")
            return completed("leftover\n")
        if command[:2] == ["docker", "rm"]:
            return completed("", returncode=1, stderr=f"busy {secret}\n")
        return healthy_service(command)

    stack = FakeStack(answer=failing_cleanup, cancelling=held_cancel())

    cells = [c for c in run_smoke_cells(stack) if c.name.startswith("cleanup")]

    assert len(cells) == 2  # the paid turn's and the cancelled turn's, each on its own
    assert all(cell.verdict is Verdict.FAILED for cell in cells)
    assert all(f"docker {failing} exited 1" in cell.figure for cell in cells)
    assert all(secret[:8] not in cell.figure for cell in cells)


def test_a_network_inspect_that_is_not_json_fails_in_one_line():
    stack = FakeStack(answer=lambda _: completed("not json"))

    with pytest.raises(LiveSmokeFailed, match="gave no network"):
        live_smoke.inspect_network(stack, "network-id")


def test_a_failed_recreate_is_failed_even_when_the_old_container_still_listens():
    secret = KNOWN.client_secret

    def failing_up(command):
        if "up" in command:
            return completed("", returncode=1, stderr=f"first\n{'e' * 190}{secret}\n")
        return healthy_service(command)

    stack = FakeStack(answer=failing_up)

    cells = run_smoke_cells(stack)

    assert [(cell.name, cell.verdict) for cell in cells] == [("recreate", Verdict.FAILED)]
    assert "exited 1" in cells[0].figure
    assert secret[:8] not in cells[0].figure
    assert not any("agent-smoke" in command for command in stack.asked)


def test_a_log_that_could_not_be_read_prints_sample_failed_for_l4():
    def unreadable_since(command):
        if "--since" in command:
            return completed("", returncode=1)
        return healthy_service(command)

    stack = FakeStack(answer=unreadable_since, cancelling=held_cancel())

    cells = {cell.name.split()[0]: cell for cell in run_smoke_cells(stack)}

    assert "ERROR lines since (0) SAMPLE FAILED" in cells["L4"].figure
    assert cells["L4"].verdict is Verdict.OBSERVED


def test_a_paid_turn_the_host_stops_waiting_for_fails_l2_and_removes_its_container():
    def hanging(command):
        if "agent-smoke" in command and "--cancel" not in command:
            raise subprocess.TimeoutExpired(command, 1)
        return with_leftover(command)

    stack = FakeStack(answer=hanging, cancelling=held_cancel())

    cells = {cell.name.split()[0]: cell for cell in run_smoke_cells(stack)}

    assert cells["L2"].verdict is Verdict.FAILED
    assert "the host gave up" in cells["L2"].figure
    assert "docker rm -f leftover" in asked_commands(stack)


def test_an_interrupted_paid_turn_still_removes_its_container():
    def interrupted(command):
        if "agent-smoke" in command:
            raise KeyboardInterrupt
        return with_leftover(command)

    stack = FakeStack(answer=interrupted)

    with pytest.raises(KeyboardInterrupt):
        live_smoke.paid_turn(stack, TOKENS)

    assert "docker rm -f leftover" in asked_commands(stack)


def test_a_cancelled_turn_that_hangs_is_killed_at_the_deadline_and_its_container_removed():
    hanging = FakeProcess(["event: run_started\n"], hangs=True)
    stack = FakeStack(
        answer=with_leftover,
        cancelling=hanging,
    )

    turn = live_smoke.cancelled_turn(stack, TOKENS, seconds=0.05)

    assert hanging.killed
    assert "the host gave up after 0.05 s" in turn.output
    assert "docker rm -f leftover" in asked_commands(stack)


def test_an_interrupted_cancelled_turn_ends_its_child_and_removes_its_container():
    interrupted = FakeProcess(["event: run_started\n"], interrupted=True)
    stack = FakeStack(
        answer=with_leftover,
        cancelling=interrupted,
    )

    with pytest.raises(KeyboardInterrupt):
        live_smoke.cancelled_turn(stack, TOKENS)

    assert interrupted.terminated
    assert "docker rm -f leftover" in asked_commands(stack)


# --- L5, judged from what the container saw ---------------------------------------------------


TARGETS = live_smoke.EgressTargets(
    provider="160.79.104.10", docker0="172.17.0.1", internet="172.31.251.1", lan="192.168.1.5",
    agent_free="172.31.250.2", songmaker_free="172.31.248.2", presentator_free="172.31.249.2",
    egress_free="172.31.252.2",
    agent_gateways=[], egress_gateways=[],
)  # fmt: skip


def held_sighting() -> dict:
    refused = "ENETUNREACH"
    return {
        "public_tcp": {"1.1.1.1": refused, TARGETS.provider: refused},
        "defaults": [],
        "reach": dict.fromkeys(
            (
                "docker0",
                "internet",
                "lan",
                "agent_free",
                "songmaker_free",
                "presentator_free",
                "egress_free",
            ),
            refused,
        ),
        "ipv6_interfaces": ["lo"],
        "ipv6_tcp": refused,
        "udp": {"443": refused, "53": refused},
        "outside": [],
        "gateway": [GATEWAY],
    }


def test_the_unreachable_world_holds_e6_to_e10():
    cells = live_smoke.judge_egress(TARGETS, held_sighting(), GATEWAY)

    assert [cell.name.split()[0] for cell in cells] == ["E6", "E7", "E8", "E9", "E10"]
    assert {cell.verdict for cell in cells} == {Verdict.HELD}


@pytest.mark.parametrize(
    ("cell", "change"),
    [
        ("E6", lambda seen: seen["public_tcp"].update({"1.1.1.1": "ECONNREFUSED"})),
        ("E7", lambda seen: seen["reach"].update({"lan": "connected"})),
        ("E7", lambda seen: seen.update(defaults=["eth0"])),
        ("E8", lambda seen: seen.update(ipv6_interfaces=["lo", "eth0"])),
        ("E9", lambda seen: seen["udp"].update({"53": "routable"})),
        ("E10", lambda seen: seen.update(outside=["93.184.216.34"])),
        ("E10", lambda seen: seen.update(gateway=["10.0.0.1"])),
    ],
)
def test_a_reachable_address_fails_exactly_its_cell(cell, change):
    seen = held_sighting()
    change(seen)

    cells = live_smoke.judge_egress(TARGETS, seen, GATEWAY)

    assert {c.name.split()[0] for c in cells if c.verdict is Verdict.FAILED} == {cell}


def test_a_gateway_on_the_agent_network_fails_e7():
    targets = live_smoke.EgressTargets(**{**TARGETS.__dict__, "agent_gateways": ["172.31.250.1"]})

    cells = live_smoke.judge_egress(targets, held_sighting(), GATEWAY)

    assert [c.name.split()[0] for c in cells if c.verdict is Verdict.FAILED] == ["E7"]


@pytest.mark.parametrize(
    ("listening", "held"),
    [
        ({"tcp": [f"{GATEWAY}:443", "127.0.0.11:40001"], "udp": ["127.0.0.11:50002"]}, True),
        ({"tcp": [f"{GATEWAY}:443", "0.0.0.0:8080"], "udp": []}, False),
        ({"tcp": [f"{GATEWAY}:443"], "udp": [f"{GATEWAY}:53"]}, False),
        ({"tcp": [f"{GATEWAY}:443", "[::]:443"], "udp": []}, False),
        ({"tcp": ["127.0.0.11:40001"], "udp": []}, False),
    ],
)
def test_only_the_gateways_443_and_the_embedded_resolver_may_listen(listening, held):
    assert (live_smoke.judge_listeners(listening, GATEWAY).verdict is Verdict.HELD) is held


def test_the_unused_address_is_the_last_one_no_member_holds():
    network = {
        "IPAM": {"Config": [{"Subnet": "172.31.250.0/24", "Gateway": "172.31.250.1"}]},
        "Containers": {"a": {"IPv4Address": "172.31.250.254/24"}},
    }

    assert live_smoke.unused_address(network) == "172.31.250.253"
    assert live_smoke.gateways_of(network) == ["172.31.250.1"]
    assert live_smoke.gateways_of({"IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}) == []


def test_the_stack_is_anchored_to_the_repository_and_names_what_compose_resolves():
    stack = FakeStack(answer=healthy_service)

    assert stack.compose_prefix[:5] == (
        "docker", "compose", "--project-directory", str(ROOT), "--file",
    )  # fmt: skip
    assert (stack.project, stack.gateway) == ("edge", GATEWAY)


def test_a_compose_that_cannot_resolve_stops_the_run_with_its_last_line():
    def refusing(command):
        return completed(
            "", returncode=1, stderr="warning\nrequired variable EDGE_DOMAIN missing\n"
        )

    with pytest.raises(LiveSmokeFailed, match="config failed: required variable EDGE_DOMAIN"):
        FakeStack(answer=refusing).project  # noqa: B018


# --- the live signer proof (#369) -------------------------------------------------------------

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


def signer_docker(members=("traefik-id",), smoke=FAILED_RUN, log="UpstreamRefused", removal=0):
    """A docker with the project's MCP network, traefik-mcp and the service's logs."""

    def answer(command: list[str]) -> subprocess.CompletedProcess:
        if "config" in command:
            document = {"name": "edge", "x-egress-address": GATEWAY}
            return completed(
                json.dumps({**document, "services": {"agent-service": {"image": "i:1"}}})
            )
        if "traefik-mcp" in command:
            return completed("traefik-id\n")
        if command[:3] == ["docker", "network", "inspect"]:
            containers = {member: {} for member in members}
            return completed(json.dumps([{"Name": NETWORK, "Containers": containers}]))
        if command[:2] == ["docker", "rm"]:
            return completed(returncode=removal, stderr=f"removal failed {TOKENS.person}")
        if "logs" in command and "--since" in command:
            return completed(log)
        if "agent-smoke" in command:
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


def test_p44_a_service_toml_without_the_mcp_origin_stops_naming_slice_c(tmp_path):
    service = tmp_path / "service.toml"
    service.write_text('[apps.songmaker]\nworkload_client = "songmaker"\n', encoding="utf-8")

    with pytest.raises(LiveSmokeFailed, match="#346 slice C"):
        live_signer.mcp_origin(service)


def test_p44_the_missing_origin_stops_before_any_sign_in(tmp_path, monkeypatch, capsys):
    service = tmp_path / "service.toml"
    service.write_text("[apps.songmaker]\n", encoding="utf-8")
    monkeypatch.setattr(live_signer, "SERVICE_TOML", service)
    monkeypatch.setattr(live_signer, "sign_in", lambda *_: pytest.fail("signed in"))

    assert live_signer.main([]) == 1
    assert "#346 slice C" in capsys.readouterr().err


def test_the_origin_is_read_from_the_songmaker_app(tmp_path):
    service = tmp_path / "service.toml"
    service.write_text(f'[apps.songmaker]\nmcp_origin = "{ORIGIN}"\n', encoding="utf-8")

    assert live_signer.mcp_origin(service) == ORIGIN


@pytest.mark.parametrize(
    ("output", "log", "verdict"),
    [
        (FAILED_RUN, "UpstreamRefused", Verdict.HELD),
        (FAILED_RUN, "UpstreamUnreachable", Verdict.FAILED),
        (FAILED_RUN, "nothing", Verdict.FAILED),
        (
            "event: run_started\nevent: assistant_text\nhi\nevent: succeeded\nhi\n",
            "UpstreamRefused",
            Verdict.FAILED,
        ),
        (
            "event: run_started\nevent: assistant_text\nhi\nevent: failed\nprovider_error: x\n",
            "UpstreamRefused",
            Verdict.FAILED,
        ),
        ("refused with 403 forbidden: no\n", "", Verdict.FAILED),
    ],
)
def test_p42_l4a_holds_only_on_a_failed_run_without_text_refused_by_the_gateway(
    output, log, verdict
):
    cell = live_signer.l4a_cell(output, log, KNOWN)

    assert cell.verdict is verdict
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


def test_a_clean_proof_recreates_runs_l4a_then_l4b_and_removes_the_standin():
    stack = standin_stack()

    code, printed = run_signer(stack)

    assert code == 0
    assert [line.split(" ")[0] for line in printed] == ["recreate", "L4a", "L4b"]
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
