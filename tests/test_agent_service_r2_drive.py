"""R2 and B9 (#469, agent-runtime#440's 12b row): the drive of the paid turn against a fake
clock, a fake client and a fake app. The login command's own tests are in
test_agent_service_cells_12b.py."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_support import CONFIGURATION, TOKEN, login_probe_modules, resolved

MARKER = "SECRET-MARKER-NOT-TO-BE-PRINTED"


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
