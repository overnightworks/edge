"""The operator's smoke of the agent service (#214 cells S3 and S5): `agent-service/smoke.py`
against a fake stream, and the `agent-smoke` service of compose.yaml as a file.

The smoke against the real service is proven by probe/agent-service/unit.py (S4) and by the
operator's runbook (README "The agent service", "The smoke").
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PERSON_TOKEN = "person-token-0123456789abcdef"
WORKLOAD_TOKEN = "workload-token-fedcba9876543210"
BOTH_TOKENS = f"{PERSON_TOKEN}\n{WORKLOAD_TOKEN}\n"


def load_smoke():
    spec = importlib.util.spec_from_file_location("agent_smoke", ROOT / "agent-service/smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = load_smoke()


def stream_of(*events: tuple[str, dict]) -> bytes:
    return b"".join(
        f"id: {number}\nevent: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode()
        for number, (name, data) in enumerate(events)
    )


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


class FakeConnection:
    """What the service answers; it records the request and whether it was closed."""

    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[tuple[str, str, bytes, dict]] = []
        self.closed = False

    def request(self, method: str, path: str, body: bytes, headers: dict) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


def drive(
    answer: FakeResponse | Exception, *arguments: str, stdin: str = BOTH_TOKENS
) -> tuple[int, str, FakeConnection]:
    connection = FakeConnection(answer)
    out = io.StringIO()
    exit_code = smoke.main(list(arguments), io.StringIO(stdin), out, lambda *_: connection)
    return exit_code, out.getvalue(), connection


def run_events(*events: tuple[str, dict]) -> FakeResponse:
    return FakeResponse(200, stream_of(*events))


SUCCESS = (
    ("run_started", {"heartbeat_interval_s": 15}),
    ("assistant_text", {"text": "ok"}),
    ("succeeded", {"text": "ok"}),
)


def test_a_successful_run_prints_the_event_types_and_the_text_and_exits_0():
    exit_code, out, connection = drive(run_events(*SUCCESS))

    assert exit_code == 0
    assert out.splitlines() == [
        "event: run_started",
        "event: assistant_text",
        "ok",
        "event: succeeded",
        "ok",
    ]
    assert connection.closed


def test_the_tokens_go_to_the_service_as_headers_and_nowhere_else():
    _, _, connection = drive(run_events(*SUCCESS))

    ((method, path, body, headers),) = connection.requests
    assert (method, path) == ("POST", "/runs")
    assert headers["Authorization"] == f"Bearer {PERSON_TOKEN}"
    assert headers["Agent-Workload-Token"] == WORKLOAD_TOKEN
    assert PERSON_TOKEN.encode() not in body
    assert WORKLOAD_TOKEN.encode() not in body


def test_a_failed_run_prints_its_code_and_message_and_exits_1():
    failure = ("failed", {"code": "provider_error", "message": "The provider failed."})

    exit_code, out, _ = drive(run_events(SUCCESS[0], failure))

    assert exit_code == 1
    assert "provider_error: The provider failed." in out


def test_a_refusal_prints_the_services_code_and_message_and_exits_1():
    refusal = json.dumps({"v": 1, "code": "unauthorized", "message": "Sign in again."}).encode()

    exit_code, out, _ = drive(FakeResponse(401, refusal))

    assert exit_code == 1
    assert out.strip() == "refused with 401 unauthorized: Sign in again."


def test_a_refusal_that_is_no_typed_body_prints_the_status_only():
    exit_code, out, _ = drive(FakeResponse(502, b"<html>bad gateway</html>"))

    assert exit_code == 1
    assert out.strip() == "refused with 502"


def test_a_stream_cut_short_fails_the_run():
    exit_code, _, _ = drive(run_events(SUCCESS[0]))

    assert exit_code == 1


def test_cancel_closes_the_stream_after_the_first_text_and_reads_no_further():
    response = run_events(*SUCCESS)

    exit_code, out, connection = drive(response, "--cancel")

    assert exit_code == 0
    assert out.splitlines()[-1] == "closing the stream during the answer"
    assert "event: succeeded" not in out
    assert connection.closed
    assert b"succeeded" in response.read()


def test_cancel_of_a_run_that_ended_first_fails():
    exit_code, out, _ = drive(run_events(*SUCCESS[:1]), "--cancel")

    assert exit_code == 1
    assert "ended before the stream could be closed" in out


@pytest.mark.parametrize(
    "stdin", ["", f"{PERSON_TOKEN}\n", f"{PERSON_TOKEN}\n\n", f"{BOTH_TOKENS}third\n", "a b\nc\n"]
)
def test_anything_but_two_token_lines_on_stdin_starts_no_run(stdin, capsys):
    exit_code, out, connection = drive(run_events(*SUCCESS), stdin=stdin)

    assert exit_code == 1
    assert connection.requests == []
    assert out == ""
    assert "two lines" in capsys.readouterr().err


def test_a_token_in_the_environment_or_in_argv_is_not_taken(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_PERSON_TOKEN", PERSON_TOKEN)
    monkeypatch.setenv("AGENT_WORKLOAD_TOKEN", WORKLOAD_TOKEN)

    exit_code, _, connection = drive(run_events(*SUCCESS), stdin="")
    stdin, argv = io.StringIO(BOTH_TOKENS), ["--person-token", PERSON_TOKEN]
    with pytest.raises(SystemExit):
        smoke.main(argv, stdin)

    assert exit_code == 1
    assert connection.requests == []
    capsys.readouterr()


@pytest.mark.parametrize(
    "answer",
    [
        run_events(
            ("run_started", {"heartbeat_interval_s": 15}),
            ("assistant_text", {"text": f"I was given {PERSON_TOKEN}"}),
            ("failed", {"code": "provider_error", "message": f"{WORKLOAD_TOKEN} failed"}),
        ),
        FakeResponse(401, json.dumps({"code": "unauthorized", "message": PERSON_TOKEN}).encode()),
        OSError(f"connection to the service broke with {WORKLOAD_TOKEN}"),
        FakeResponse(200, b"event: x\ndata: {" + PERSON_TOKEN.encode() + b"\n\n"),
    ],
    ids=["events", "refusal", "connection error", "broken frame"],
)
def test_no_token_reaches_the_output_whichever_path_a_run_takes(answer, capsys):
    _, out, _ = drive(answer)

    printed = out + capsys.readouterr().out + capsys.readouterr().err
    assert PERSON_TOKEN not in printed
    assert WORKLOAD_TOKEN not in printed


# --- the compose service (S5) ---------------------------------------------------------------


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]


def test_agent_smoke_is_hardened_like_the_realm_observer(services):
    smoke_service, observer = services["agent-smoke"], services["realm-observer"]

    for key in ("user", "read_only", "cap_drop", "security_opt"):
        assert smoke_service[key] == observer[key]
    assert smoke_service["profiles"] == ["apply"]


def test_agent_smoke_runs_the_service_image_that_is_never_pulled(services):
    assert services["agent-smoke"]["image"] == services["agent-service"]["image"]
    assert services["agent-smoke"]["pull_policy"] == "never"


def test_agent_smoke_sits_on_the_agent_network_alone_with_no_port_secret_or_published_address(
    services,
):
    smoke_service = services["agent-smoke"]

    assert smoke_service["networks"] == ["agent"]
    assert not {"ports", "expose", "secrets", "extra_hosts", "network_mode", "depends_on"} & (
        smoke_service.keys()
    )
    assert all(
        volume.endswith(":ro") and volume.startswith("./agent-service/smoke.py:")
        for volume in smoke_service["volumes"]
    )
    assert "environment" not in smoke_service or set(smoke_service["environment"]) <= {
        "PYTHONDONTWRITEBYTECODE"
    }


def test_agent_smoke_starts_the_mounted_script_in_isolated_mode(services):
    smoke_service = services["agent-smoke"]

    assert smoke_service["entrypoint"][:3] == ["python", "-I", "-B"]
    assert smoke_service["entrypoint"][3] == "/opt/smoke/smoke.py"
    assert smoke_service["volumes"] == ["./agent-service/smoke.py:/opt/smoke/smoke.py:ro"]


def test_agent_smoke_reaches_the_service_by_the_name_and_port_the_smoke_defaults_to(services):
    assert smoke.DEFAULT_BASE_URL == "http://agent-service:8080"
    assert "agent" in services["agent-service"]["networks"]


def test_the_agent_network_has_the_fixed_name_apps_join_as_an_external_network():
    networks = yaml.safe_load((ROOT / "compose.yaml").read_text())["networks"]

    assert networks["agent"]["name"] == "hal_agent"
    assert networks["agent"]["internal"] is True
