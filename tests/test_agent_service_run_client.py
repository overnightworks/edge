"""The run client of the login command's R2 (#469, agent-runtime#440 B9): the lines it prints,
the close on the host's command and the status read, on a fake connection and fake clocks. Its
`main`, which imports the runtime packages and connects to the service, runs only live. It also
holds the drive tests of cells_12b's paid turn (R2 and B9) against a fake clock, client and app."""

from __future__ import annotations

import http.client
import io
import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from support.probe_support import CONFIGURATION, TOKEN, login_probe_modules, probe_imports, resolved

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


# --- R2 and B9 (#469): the drive of the paid turn against a fake clock, client and app ---


@pytest.fixture(scope="module")
def probe() -> Iterator[SimpleNamespace]:
    with login_probe_modules() as modules:
        yield modules


PERSON, WORKLOAD = "person-token-value", "workload-token-value"
FRAME_FAILED = "a frame cell did not hold"


class Timeline:
    """The fake clock of one R2 drive: `sleep` is the only thing that advances it, and the
    client's output and the app's log are functions of it."""

    def __init__(self, client_lines, app_lines=lambda now: [], exits_at=None):
        self.now = 0.0
        self.client_lines = client_lines
        self.app = app_lines
        self.exits_at = exits_at
        self.closed_times: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeClient:
    def __init__(self, timeline: Timeline, read) -> None:
        self.timeline = timeline
        self.read = read
        self.finished_with: float | None = None

    def report(self):
        return self.read(self.lines)

    @property
    def lines(self) -> list[str]:
        return self.timeline.client_lines(self.timeline.now, bool(self.timeline.closed_times))

    def alive(self) -> bool:
        exits = self.timeline.exits_at
        return exits is None or self.timeline.now < exits

    def say_close(self) -> None:
        self.timeline.closed_times.append(self.timeline.now)

    def finish(self, seconds: float) -> None:
        self.finished_with = seconds


def watched(probe, **change):
    fields = {
        "group_seen": True, "group_gone_at": 1002.0,
        "tree_seen": True, "tree_gone_at": 1003.0, "samples": 300,
    }  # fmt: skip
    return probe.census.Watched(**{**fields, **change})


def r2_probe(probe, tmp_path, timeline, *, watch=None, failed=(), root_entries=0, commands=None):
    """`commands`: a fake `run` port; the log reads then go through the real FakeApp and
    IssuerProbe, down to it."""
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed, run.not_driven = [], list(failed), []
    run.texts, run.minted, run.windows = [], [], []
    run.gate = probe.checks.Gate([])
    run.login = SimpleNamespace(token=TOKEN)
    run.working_root_entries = root_entries
    run.watch_directory = tmp_path
    run.tokens = lambda: SimpleNamespace(person=PERSON, workload=WORKLOAD)
    run.census = lambda: SimpleNamespace(
        key=b"\x02", watch=lambda seconds: watch(seconds) if callable(watch) else watch
    )
    if commands is None:
        run.container = lambda service: f"{service}-container"
        run.container_log = lambda service: ""
        run.app_lines = lambda seconds=300, container=None: timeline.app(timeline.now)
    else:
        run.run = commands
        run.compose_file, run.overlays = Path("compose.yaml"), []
    run.write_mode = lambda mode: run.windows.append(("mode", mode))
    run.start_client = lambda tokens: (
        run.windows.append("client") or FakeClient(timeline, probe.checks.read_client_output)
    )
    run.clock, run.sleep = lambda: timeline.now, timeline.sleep
    return run


def cells_of(run) -> dict[str, str]:
    return {cell: "held" for cell in run.held} | {
        **{cell: "failed" for cell in run.failed},
        **{cell: "not driven" for cell in run.not_driven},
    }


def test_a_frame_failure_posts_nothing_and_leaves_b9_not_driven(probe, tmp_path, capsys):
    """Matrix row 2: R2 is posted only after every frame cell held."""
    timeline = Timeline(lambda *_: [])
    run = r2_probe(probe, tmp_path, timeline, failed=["S00 (token)"])
    run.cell_b9()
    assert "client" not in run.windows
    assert run.not_driven == [f"B9 ({letter})" for letter in "abcde"]
    assert f"B9 (a) NOT DRIVEN: {FRAME_FAILED}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("lines", "alive_for", "reason", "closes"),
    [
        (["answer 409 busy"], 0.0, "R2 was refused: 409 busy", 1),
        (["answer 500"], 0.0, "R2 was refused: 500 no code", 1),
        (["answer 200"], None, "R2 reached no run_started", 1),
        (["answer 200", "event run_started"], 0.0, "no call reached the fake app", 1),
        (["answer 200", "event run_started"], None, "no call reached the fake app", 1),
    ],
    ids=["busy", "refused", "no run_started", "run ended before a call", "no call in time"],
)
def test_a_run_that_reaches_no_call_is_not_driven_and_is_posted_and_closed_once(
    probe, tmp_path, capsys, lines, alive_for, reason, closes
):
    """Matrix rows 8, 14 and 15: one POST, one close, no retry, and the bound on the paid turn
    is the fake clock's R2_CALL_SECONDS."""
    timeline = Timeline(lambda *_: lines, exits_at=alive_for)
    posts = []
    run = r2_probe(probe, tmp_path, timeline)
    run.start_client = lambda tokens: (
        posts.append(tokens) or FakeClient(timeline, probe.checks.read_client_output)
    )
    run.cell_b9()
    assert (len(posts), len(timeline.closed_times)) == (1, closes)
    assert run.not_driven == [f"B9 ({letter})" for letter in "abcde"]
    assert f"B9 (a) NOT DRIVEN: {reason}" in capsys.readouterr().out
    expected_wait = {
        "R2 reached no run_started": probe.checks.START_SECONDS,
        "no call reached the fake app": probe.checks.R2_CALL_SECONDS if alive_for is None else 0,
    }.get(reason, 0)
    assert timeline.closed_times == [expected_wait]


class StalledDocker:
    """The fake `run` port: from the turn's start until the stream is closed every command
    stalls to its timeout; each is recorded with the seconds the turn has left."""

    def __init__(self, timeline: Timeline, turn_seconds: int) -> None:
        self.timeline, self.turn_seconds = timeline, turn_seconds
        self.turn_began: float | None = None
        self.started_in_turn: list[tuple[int, float]] = []

    def begin_turn(self) -> None:
        self.turn_began = self.timeline.now

    def __call__(self, command, *, check=True, timeout=300, **_):
        if self.turn_began is None or self.timeline.closed_times:
            return subprocess.CompletedProcess(command, 0, "a-container-id\n", "")
        left = self.turn_began + self.turn_seconds - self.timeline.now
        self.started_in_turn.append((timeout, left))
        self.timeline.now += timeout
        raise subprocess.TimeoutExpired(command, timeout)


def test_every_command_of_the_paid_turn_gets_at_most_the_seconds_left(probe, tmp_path, capsys):
    """#469 R2_CALL_SECONDS: a docker daemon that stalls ends the turn at its deadline, and no
    command started in it is allowed more than what is left; B9 is not driven."""
    timeline = Timeline(lambda *_: ["answer 200", "event run_started"])
    docker = StalledDocker(timeline, probe.checks.R2_CALL_SECONDS)
    run = r2_probe(probe, tmp_path, timeline, commands=docker)
    start = run.start_client
    run.start_client = lambda tokens: docker.begin_turn() or start(tokens)
    run.cell_b9()
    assert docker.started_in_turn
    assert all(timeout <= left for timeout, left in docker.started_in_turn)
    assert run.not_driven == [f"B9 ({letter})" for letter in "abcde"]
    assert "B9 (a) NOT DRIVEN: no call reached the fake app" in capsys.readouterr().out


def test_a_dummy_run_prints_what_the_app_and_the_watch_saw_and_holds_nothing_of_b9(
    probe, tmp_path, capsys
):
    """Matrix row 1: the login project reached the app (handshake, one listing); no call."""
    reads = []  # the first read is the baseline before R2; the app logs its lines after it
    timeline = Timeline(
        lambda *_: ["answer 200", "event run_started", "event failed", "status failed none"],
        app_lines=lambda now: app_start(probe) if reads.append(now) or len(reads) > 1 else [],
        exits_at=0.0,
    )
    run = r2_probe(probe, tmp_path, timeline, watch=watched(probe, group_seen=False))
    run.cell_b9()
    output = capsys.readouterr().out
    assert (
        "R2 observed: the app logged 2 handshake lines and 1 listings; the client printed "
        "2 events, 0 lines outside its vocabulary; watch: group seen False, tree seen True, "
        "300 samples"
    ) in output
    assert "B9 (e) NOT DRIVEN: no call reached the fake app" in output
    assert (tmp_path / "stop").exists()
    assert (PERSON in run.gate.secrets, WORKLOAD in run.gate.secrets, "02" in run.gate.secrets) == (
        True, True, True,
    )  # fmt: skip


def app_start(probe) -> list[str]:
    """The app's lines of the login project reaching it: the handshake and one listing."""
    return [*probe.fake_app.APP_HANDSHAKE, probe.fake_app.APP_LISTING.format(canary=0)]


def hold_and_release(probe):
    """The app holds the call from 1 s, and releases it 0.5 s after the close at 2 s."""

    def lines(now: float) -> list[str]:
        held = [probe.fake_app.HELD_LINE] if now >= 1.0 else []
        released = [f"{probe.fake_app.RELEASED_PREFIX}1002.500"] if now >= 2.5 else []
        return [*app_start(probe), *held, *released]

    return lines


def closed_run(now: float, closed: bool) -> list[str]:
    return ["answer 200", "event run_started", "closed at=1002.000"] if closed else [
        "answer 200", "event run_started",
    ]  # fmt: skip


def test_a_call_that_is_closed_at_t0_gives_the_b9_verdicts_and_the_timing(probe, tmp_path, capsys):
    """Matrix row 3: (a)-(e) from the client's lines, the watch's times, the app's release."""
    lines = [
        "answer 200", "event run_started", "closed at=1002.000", "status cancelled disconnected",
    ]  # fmt: skip
    timeline = Timeline(lambda now, closed: lines if closed else lines[:2], hold_and_release(probe))
    run = r2_probe(probe, tmp_path, timeline, watch=watched(probe))
    run.cell_b9()
    output = capsys.readouterr().out
    assert cells_of(run) == {f"B9 ({letter})": "held" for letter in "abcde"}
    assert "B9 timing (observed, no bound): max(t_reap, t_tree, t_up) - t0 = 1.00 s" in output
    assert len(timeline.closed_times) == 1
    assert any(PERSON in text for text in run.texts) is False
    assert any(probe.fake_app.HELD_LINE in text for text in run.texts)


def test_a_watch_that_failed_leaves_the_group_and_the_tree_not_driven(probe, tmp_path, capsys):
    """Matrix row 6: a watch that failed leaves B9 (b) and (c) NOT DRIVEN and prints nothing of its
    failure."""
    lines = ["answer 200", "event run_started", "closed at=1002.000", "status cancelled dropped"]
    timeline = Timeline(lambda now, closed: lines, hold_and_release(probe))

    def failing(_seconds):
        raise probe.egress.ProbeFailed(MARKER)

    run = r2_probe(probe, tmp_path, timeline, watch=failing)
    run.cell_b9()
    assert cells_of(run)["B9 (b)"] == cells_of(run)["B9 (c)"] == "not driven"
    assert cells_of(run)["B9 (a)"] == cells_of(run)["B9 (d)"] == "held"
    assert MARKER not in capsys.readouterr().out


def test_s1_fails_when_a_minted_token_is_in_the_clients_output(probe, capsys):
    """Matrix row 10: a minted token in the client's output fails S1, and is not printed."""
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed = [], []
    run.minted = [SimpleNamespace(person=PERSON, workload=WORKLOAD)]
    run.login = SimpleNamespace(token=TOKEN)
    run.gate = SimpleNamespace(hits=0)
    run.container_log = lambda service: ""
    run.texts = [f"event run_started\nstatus {WORKLOAD}"]
    run.cell_s1()
    assert (run.failed, run.held) == (["S1"], [])
    output = capsys.readouterr().out
    assert WORKLOAD not in output
    assert output.startswith("S1 FAILED: 0 gate hits, 0 in the service log, 1 in all")


def test_the_fake_app_is_started_with_the_local_log_driver_or_nothing_starts(probe):
    """#469 acceptance: the fake app starts with the local log driver, or nothing starts."""
    config = resolved()
    assert "agent-app" in probe.cells.STARTED
    del config["services"]["agent-app"]["logging"]
    with pytest.raises(probe.checks.LoginFailed) as raised:
        probe.checks.check_resolved(
            config,
            service="agent-service",
            started=probe.cells.STARTED,
            login=("/run/agent-service/login/claude", Path("/login")),
            configuration=(CONFIGURATION, Path("/scratch/service.toml")),
        )
    assert str(raised.value) == probe.checks.Refusal.LOG_DRIVER


def test_a_failed_log_read_still_closes_the_stream_and_stops_the_watch(probe, tmp_path):
    """#469 R2: a wait that raises leaves neither the stream open nor the watch running."""
    reads = []

    def failing_app_lines(now):
        if reads.append(now) or len(reads) > 1:
            raise probe.egress.ProbeFailed("docker logs exited 1")
        return []

    timeline = Timeline(lambda *_: ["answer 200", "event run_started"], failing_app_lines)
    run = r2_probe(probe, tmp_path, timeline, watch=watched(probe))
    with pytest.raises(probe.egress.ProbeFailed):
        run.drive_r2()
    assert len(timeline.closed_times) == 1
    assert (tmp_path / "stop").exists()
