"""The live smoke command (`agent-service/live_smoke.py`, #221): the sign-in, the report and the
no-secret rule against a scripted docker. The command against a running service is driven by
probe/agent-service/unit.py (cell U16) and by the operator.
"""

import base64
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))

import live_smoke  # noqa: E402
from live_smoke import Cell, LiveSmokeFailed, Report, Secrets, Stack, Verdict  # noqa: E402
from smoke import Tokens  # noqa: E402

TOKENS = Tokens(person="person-token-0123456789", workload="workload-token-9876543210")
KNOWN = Secrets(TOKENS, login_token="login-token-abcdef", client_secret="client-secret-fedcba")
GATEWAY = "172.31.252.10"


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- the sign-in ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pasted", "refused"),
    [
        ("https://songmaker.hallucinai.de/auth/callback?state=S&code=C", False),
        ("  https://songmaker.hallucinai.de/auth/callback?code=C&state=S\n", False),
        ("https://songmaker.hallucinai.de/auth/callback?state=other&code=C", True),
        ("https://songmaker.hallucinai.de/auth/callback?state=S", True),
        ("not an address", True),
    ],
)
def test_the_pasted_address_must_hold_the_code_of_this_sign_in(pasted, refused):
    if refused:
        with pytest.raises(LiveSmokeFailed, match="no code for this sign-in"):
            live_smoke.code_in(pasted, "S")
    else:
        assert live_smoke.code_in(pasted, "S") == "C"


class FakeTokenEndpoint:
    """Answers each token request in turn and keeps what was asked."""

    def __init__(self, *answers: dict | urllib.error.HTTPError) -> None:
        self.answers = list(answers)
        self.forms: list[dict[str, str]] = []

    def __call__(self, address: str, body: bytes, timeout: float):
        assert address == f"{live_smoke.OIDC}/token"
        self.forms.append({k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())


def test_the_sign_in_exchanges_the_pasted_code_and_asks_for_the_workload_token(capsys):
    endpoint = FakeTokenEndpoint({"access_token": "person"}, {"access_token": "workload"})
    shown: list[str] = []

    def ask(prompt: str, stream) -> str:
        shown.extend(capsys.readouterr().err.splitlines())
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(shown[-1]).query)["state"][0]
        return f"https://songmaker.hallucinai.de/auth/callback?state={state}&code=the-code"

    tokens = live_smoke.sign_in("the-secret", ask, endpoint)

    assert tokens == Tokens(person="person", workload="workload")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(shown[-1]).query)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(endpoint.forms[0]["code_verifier"].encode()).digest()
    ).rstrip(b"=")
    assert query["code_challenge"] == [challenge.decode()]
    assert endpoint.forms[0]["code"] == "the-code"
    assert endpoint.forms[1]["grant_type"] == "client_credentials"
    assert all(form["client_secret"] == "the-secret" for form in endpoint.forms)
    assert endpoint.forms[0]["code_verifier"] not in "\n".join(shown)


def test_a_refusing_token_endpoint_stops_with_its_status_and_no_secret():
    refusal = urllib.error.HTTPError("x", 401, "no", {}, None)
    endpoint = FakeTokenEndpoint(refusal)

    with pytest.raises(LiveSmokeFailed) as stopped:
        live_smoke.access_token({}, "the-secret", endpoint)

    assert str(stopped.value) == "the token endpoint answered 401"


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
        (1, "event: run_started\nevent: failed\nprovider_error: x\n", False, True),
        (1, "refused with 401 unauthorized: expired\n", False, False),
    ],
)
def test_the_paid_turn_holds_only_on_succeeded_and_admission_on_run_started(
    returncode, output, paid, admitted
):
    l2, l3 = live_smoke.turn_cells(returncode, output)

    assert (l2.verdict is Verdict.HELD, l3.verdict is Verdict.HELD) == (paid, admitted)
    assert ("unauthorized" in l2.figure) == ("401" in output)


# --- the service, against a scripted docker ---------------------------------------------------


@dataclass(frozen=True)
class FakeProcess:
    lines: list[str]
    returncode: int = 0
    stdin: io.StringIO = field(default_factory=io.StringIO)

    @property
    def stdout(self):
        return iter(self.lines)

    def wait(self) -> int:
        return self.returncode


@dataclass(frozen=True)
class FakeStack(Stack):
    """A docker that answers by what is asked; `answer` gets the command after the prefix."""

    answer: Callable[[list[str]], subprocess.CompletedProcess] = lambda _: completed()
    cancelling: FakeProcess | None = None
    asked: list[list[str]] = field(default_factory=list)

    def run(self, command, stdin=None, timeout=300):
        self.asked.append([*command])
        return self.answer(list(command))

    def start_compose(self, *arguments):
        self.asked.append([*self.prefix(), *arguments])
        return self.cancelling


def healthy_service(command: list[str]) -> subprocess.CompletedProcess:
    text = " ".join(command)
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


# --- L5, judged from what the container saw ---------------------------------------------------


TARGETS = live_smoke.EgressTargets(
    provider="160.79.104.10", docker0="172.17.0.1", internet="172.31.251.1", lan="192.168.1.5",
    agent_free="172.31.250.2", egress_free="172.31.252.2", agent_gateways=[], egress_gateways=[],
)  # fmt: skip


def held_sighting() -> dict:
    refused = "ENETUNREACH"
    return {
        "public_tcp": {"1.1.1.1": refused, TARGETS.provider: refused},
        "defaults": [],
        "reach": dict.fromkeys(
            ("docker0", "internet", "lan", "agent_free", "egress_free"), refused
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
        ("E7", lambda seen: seen["reach"].update({"lan": "0"})),
        ("E7", lambda seen: seen.update(defaults=["eth0"])),
        ("E8", lambda seen: seen.update(ipv6_interfaces=["lo", "eth0"])),
        ("E9", lambda seen: seen["udp"].update({"53": "0"})),
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
    ("listing", "held"),
    [
        (f"tcp {GATEWAY} 443\ntcp 127.0.0.11 40001\nudp 127.0.0.11 50002\n", True),
        (f"tcp {GATEWAY} 443\ntcp 0.0.0.0 8080\n", False),
        (f"tcp {GATEWAY} 443\nudp {GATEWAY} 53\n", False),
        (f"tcp {GATEWAY} 443\ntcp6 :: 443\n", False),
        ("tcp 127.0.0.11 40001\n", False),
    ],
)
def test_only_the_gateways_443_and_the_embedded_resolver_may_listen(listing, held):
    assert (live_smoke.judge_listeners(listing, GATEWAY).verdict is Verdict.HELD) is held


def test_the_unused_address_is_the_last_one_no_member_holds():
    network = {
        "IPAM": {"Config": [{"Subnet": "172.31.250.0/24", "Gateway": "172.31.250.1"}]},
        "Containers": {"a": {"IPv4Address": "172.31.250.254/24"}},
    }

    assert live_smoke.unused_address(network) == "172.31.250.253"
    assert live_smoke.gateways_of(network) == ["172.31.250.1"]
    assert live_smoke.gateways_of({"IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}) == []


def test_the_project_and_the_gateway_default_to_the_live_stack():
    stack = Stack()

    assert stack.prefix()[:4] == ["docker", "compose", "-p", "edge"]
    assert stack.gateway == "172.31.252.10"
