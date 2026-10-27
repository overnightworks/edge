"""The run client of the login command's R2 (#469, agent-runtime#440 B9): the lines it prints,
the close on the host's command and the status read, on a fake connection and fake clocks. Its
`main`, which imports the runtime packages and connects to the service, runs only live."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest
from probe_support import probe_imports

MARKER = "SECRET-MARKER-NOT-TO-BE-PRINTED"
RUN_ID = "0b6c1b0e-6f3a-4a59-9d5b-3f6a8f5f9b11"
WALL = 1761566400.1234
WAIT_SECONDS = 5


@pytest.fixture(scope="module")
def run_client() -> Iterator[ModuleType]:
    with probe_imports():
        import run_client as module

        yield module


@pytest.fixture
def vocabulary(run_client):
    return run_client.Vocabulary(
        events=frozenset({"run_started", "assistant_text", "cancelled"}),
        states=frozenset({"running", "cancelled", "failed"}),
        losses=frozenset({"disconnected", "dropped"}),
        codes=frozenset({"busy", "forbidden"}),
        running="running",
    )


class FakeResponse:
    def __init__(self, status: int, document: object = None, raw: bytes | None = None) -> None:
        self.status = status
        self.raw = json.dumps(document).encode() if raw is None else raw

    def read(self, _: int = -1) -> bytes:
        return self.raw


class FakeSocket:
    def __init__(self) -> None:
        self.shut = threading.Event()
        self.closed = False

    def shutdown(self, _: int) -> None:
        self.shut.set()

    def close(self) -> None:
        self.closed = True


class FakeService:
    """What `connect` hands out: one answer per connection, in order, and the requests seen."""

    def __init__(self, *answers: FakeResponse | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str]] = []
        self.sockets: list[FakeSocket] = []

    def connect(self) -> SimpleNamespace:
        sock = FakeSocket()
        self.sockets.append(sock)

        def request(method, path, body, headers):
            self.requests.append((method, path))

        def getresponse():
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return SimpleNamespace(
            request=request, getresponse=getresponse, close=lambda: None, sock=sock
        )


def frame(event: str, **data) -> SimpleNamespace:
    return SimpleNamespace(event=event, data=data)


def stream(service: FakeService, *frames: SimpleNamespace, then=None):
    """Frames of a held stream: after them it waits for the first socket's shutdown, then ends
    with `then` (an exception) or normally."""

    def read(_response):
        yield from frames
        if then is not None:
            service.sockets[0].shut.wait(WAIT_SECONDS)
            raise then

    return read


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def pause(self, seconds: float) -> None:
        self.now += seconds


def started(run_client, vocabulary, service, frames, stdin="", clock=None):
    clock = clock or Clock()
    out = io.StringIO()
    client = run_client.RunClient(
        connect=service.connect, frames=frames, vocabulary=vocabulary, body=b"{}", headers={},
        stdin=io.StringIO(stdin), out=out, clock=lambda: clock.now, pause=clock.pause,
        wall=lambda: WALL,
    )  # fmt: skip
    code = client.run()
    return code, out.getvalue().splitlines(), client


ENDED = {"state": "cancelled", "client_lost": "disconnected", "ended_at": "2026-10-27T10:00:00Z"}
RUNNING = {"state": "running", "client_lost": None, "ended_at": None}
STARTED = frame("run_started", run_id=RUN_ID)


def test_the_close_command_shuts_the_stream_down_and_stamps_t0(run_client, vocabulary):
    """B9 driven: on `close` the socket is shut down and closed, `closed at=` carries t0, the
    expected read error is no `broke off`, and the status line follows."""
    service = FakeService(FakeResponse(200), FakeResponse(200, ENDED))
    held = stream(service, STARTED, then=OSError(MARKER))
    code, lines, _ = started(run_client, vocabulary, service, held, stdin="close\n")
    assert code == 0
    assert sorted(lines) == sorted(
        ["answer 200", "event run_started", "closed at=1761566400.123",
         "status cancelled disconnected"]
    )  # fmt: skip
    assert lines[-1] == "status cancelled disconnected"
    assert service.sockets[0].closed
    assert service.requests == [("POST", "/runs"), ("GET", f"/runs/{RUN_ID}")]


def test_a_close_after_the_stream_ended_says_nothing(run_client, vocabulary):
    """Matrix row 16: a close after the stream ended prints no `closed` line."""
    service = FakeService(FakeResponse(200), FakeResponse(200, ENDED))
    _, lines, client = started(run_client, vocabulary, service, stream(service, STARTED))
    client.close_on_command(service.connect())
    assert not any(line.startswith("closed") for line in lines)


def test_every_line_is_of_the_closed_vocabulary_and_carries_no_marker(run_client, vocabulary):
    """Matrix row 9: frame texts, an unknown event, a refusal message and unknown status values
    never print."""
    service = FakeService(
        FakeResponse(200),
        FakeResponse(200, {"state": MARKER, "client_lost": MARKER, "ended_at": MARKER}),
    )
    frames = stream(
        service,
        STARTED,
        frame(MARKER, text=MARKER, run_id=RUN_ID),
        frame("assistant_text", text=MARKER, message=MARKER),
    )
    _, lines, _ = started(run_client, vocabulary, service, frames)
    assert lines == [
        "answer 200", "event run_started", "event other", "event assistant_text",
        "status other other",
    ]  # fmt: skip
    assert all(run_client.CLIENT_LINE.fullmatch(line) for line in lines)
    assert not any(MARKER in line or RUN_ID in line or "Call the tool" in line for line in lines)


@pytest.mark.parametrize(
    ("response", "line"),
    [
        (FakeResponse(409, {"code": "busy", "message": MARKER}), "answer 409 busy"),
        (FakeResponse(400, {"code": MARKER, "message": MARKER}), "answer 400 other"),
        (FakeResponse(500, raw=MARKER.encode()), "answer 500"),
        (FakeResponse(502, [MARKER]), "answer 502"),
        (FakeResponse(400, {"message": MARKER}), "answer 400"),
    ],
    ids=["busy", "unknown code", "no JSON", "no object", "no code"],
)
def test_a_refused_run_prints_its_status_and_code_only_and_is_not_retried(
    run_client, vocabulary, response, line
):
    """Matrix row 8: a refused run prints its status and code only and is posted once."""
    service = FakeService(response)
    code, lines, _ = started(run_client, vocabulary, service, stream(service))
    assert (code, lines, service.requests) == (1, [line], [("POST", "/runs")])


def test_a_stream_that_ended_by_itself_is_followed_until_the_run_ended(run_client, vocabulary):
    """B9 (a): a stream that ended by itself is followed by status reads until the run ended."""
    service = FakeService(
        FakeResponse(200), FakeResponse(200, RUNNING), FakeResponse(200, RUNNING),
        FakeResponse(200, ENDED),
    )  # fmt: skip
    clock = Clock()
    _, lines, _ = started(run_client, vocabulary, service, stream(service, STARTED), clock=clock)
    assert lines[-1:] == ["status cancelled disconnected"]
    assert [path for _, path in service.requests].count(f"/runs/{RUN_ID}") == 3
    assert clock.now == 1.0


def test_the_last_read_is_printed_when_the_run_did_not_end_in_time(run_client, vocabulary):
    """B9 (a): the last status read is printed when the run did not end within STATUS_SECONDS."""
    still = {"state": "running", "client_lost": "disconnected", "ended_at": None}
    service = FakeService(FakeResponse(200), *[FakeResponse(200, still)] * 100)
    clock = Clock()
    _, lines, _ = started(run_client, vocabulary, service, stream(service, STARTED), clock=clock)
    assert lines[-1] == "status running disconnected running"
    assert clock.now == run_client.STATUS_SECONDS


def test_a_refused_status_read_prints_its_code(run_client, vocabulary):
    """Matrix row 4: a refused status read prints `refused` and its code."""
    service = FakeService(FakeResponse(200), FakeResponse(403, {"code": "forbidden"}))
    _, lines, _ = started(run_client, vocabulary, service, stream(service, STARTED))
    assert lines[-1] == "status refused forbidden"


def test_a_broken_stream_prints_the_exception_type_and_still_reads_the_status(
    run_client, vocabulary
):
    """Matrix row 9: a broken stream prints the exception type alone and the status is still
    read."""
    service = FakeService(FakeResponse(200), FakeResponse(200, ENDED))

    def breaks(_response):
        yield STARTED
        raise ConnectionResetError(MARKER)

    _, lines, _ = started(run_client, vocabulary, service, breaks)
    assert lines == [
        "answer 200", "event run_started", "broke off ConnectionResetError",
        "status cancelled disconnected",
    ]  # fmt: skip


def test_a_status_read_that_fails_prints_the_exception_type_alone(run_client, vocabulary):
    """Matrix row 9: a failed status read prints the exception type alone."""
    service = FakeService(FakeResponse(200), TimeoutError(MARKER))
    code, lines, _ = started(run_client, vocabulary, service, stream(service, STARTED))
    assert (code, lines[-1]) == (1, "broke off TimeoutError")


def test_a_run_that_never_started_has_no_status_to_read(run_client, vocabulary):
    """Matrix row 8: a run that never started has no run id and so no status to read."""
    service = FakeService(FakeResponse(200))
    code, lines, _ = started(run_client, vocabulary, service, stream(service))
    assert (code, lines) == (1, ["answer 200"])


class _Literal(SimpleNamespace):
    def __init__(self, name: str) -> None:
        super().__init__(__args__=(name,))


def fake_wire(vocabulary):
    """A stand-in for `agent_runtime_client.wire` holding exactly `vocabulary`'s values."""
    import enum

    def event_class(name):
        return SimpleNamespace(model_fields={"type": SimpleNamespace(annotation=_Literal(name))})

    def members(name, values):
        return enum.StrEnum(name, {value.upper(): value for value in values})

    union = SimpleNamespace(
        __args__=(SimpleNamespace(__args__=tuple(event_class(n) for n in vocabulary.events)),)
    )
    return SimpleNamespace(
        RunEvent=union,
        RunState=members("RunState", vocabulary.states),
        ClientLoss=members("ClientLoss", vocabulary.losses),
        RejectionCode=members("RejectionCode", vocabulary.codes),
    )


def test_the_vocabulary_is_read_from_the_wire_package(run_client, vocabulary):
    """#469 Decision 3: the closed sets and the running state come from the wire package."""
    assert run_client.vocabulary_from(fake_wire(vocabulary)) == vocabulary


def test_a_wire_without_run_started_fails_loud(run_client, vocabulary):
    """#469 Decision 3: the host's `run_started` is checked against the wire."""
    wire = fake_wire(vocabulary._replace(events=frozenset({"failed"})))
    with pytest.raises(ValueError, match="wire package"):
        run_client.vocabulary_from(wire)


def test_a_status_read_that_stalls_ends_at_its_socket_timeout(run_client, vocabulary):
    """#469 STATUS: a service that never answers a status read does not hold the client past it."""
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        client = run_client.RunClient(
            connect=lambda: http.client.HTTPConnection("127.0.0.1", port),
            frames=lambda _: [], vocabulary=vocabulary, body=b"{}", headers={},
            stdin=io.StringIO(), out=io.StringIO(),
        )  # fmt: skip
        client.run_id = RUN_ID
        ended: list[BaseException] = []

        def read() -> None:
            try:
                client.status_once(0.2)
            except OSError as problem:
                ended.append(problem)

        reader = threading.Thread(target=read, daemon=True)  # a regression must not hang the job
        reader.start()
        reader.join(WAIT_SECONDS)
        assert not reader.is_alive()
        assert len(ended) == 1
