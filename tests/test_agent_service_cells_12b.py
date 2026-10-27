"""The login command (#457, agent-runtime#440's 12b row): the missing-flag and refused-login stops,
the runner that prints no subprocess output or exception text, and the S00 verdicts. The pure
checks are in test_agent_service_login_checks.py; the command itself runs live under
`cells_12b.py --login-directory`."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_support import CONFIGURATION, TOKEN, login_probe_modules, make_login, resolved

MARKER = "SECRET-MARKER-NOT-TO-BE-PRINTED"


@pytest.fixture(scope="module")
def probe() -> Iterator[SimpleNamespace]:
    with login_probe_modules() as modules:
        yield modules


@pytest.fixture
def restored_signals() -> Iterator[None]:
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGALRM)}
    yield
    signal.alarm(0)
    for number, handler in handlers.items():
        signal.signal(number, handler)


def never_started(login) -> int:
    pytest.fail("started")


def test_the_missing_flag_stops_the_command_before_it_starts_anything(probe):
    """#457 row 2: a missing flag stops the command before anything starts."""
    with pytest.raises(SystemExit):
        probe.cells.main([], never_started)


def test_a_refused_login_prints_the_reason_and_exits_1_before_the_lock(probe, tmp_path, capsys):
    """#457 row 2: a refused login prints the reason and starts nothing."""
    assert probe.cells.main(["--login-directory", str(tmp_path / "x")], never_started) == 1
    reason = probe.checks.Refusal.NOT_A_DIRECTORY
    assert capsys.readouterr().out == f"PROBE FAILED: {reason}\n"


# --- what a failure prints ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "verb"),
    [
        (["docker", "exec", "-i", "id", "python", "-I", "-"], "exec"),
        (["docker", "run", "--rm", "--name", "x", "image"], "run"),
        (["docker", "inspect", "--format", "{{.Image}}", "id"], "inspect"),
        (
            [
                "docker",
                "compose",
                "-p",
                "project",
                "--project-directory",
                "/path",
                "--file",
                "f",
                "--profile",
                "p",
                "up",
                "--detach",
            ],
            "compose up",
        ),
        (["docker", "compose", "-p", "project"], "compose"),
        (["docker", "compose", "--dry-run", "up"], "compose up"),
        (["docker"], "docker"),
        (["/usr/bin/openssl", "genrsa"], "openssl"),
    ],
    ids=[
        "exec",
        "run",
        "inspect",
        "compose up",
        "compose alone",
        "compose flag",
        "docker alone",
        "other tool",
    ],
)
def test_a_failed_command_is_named_by_its_verb_alone(probe, command, verb):
    """#457 row 5: a failed command is named by its verb and exit code."""
    assert str(probe.checks.failed_command(command, 3)) == f"{verb} exited 3"


def test_a_failed_command_of_the_probe_relays_no_output(probe):
    """#457 row 5: a failed command relays none of its output."""
    run = object.__new__(probe.cells.LoginProbe)
    run.texts = []
    run.environment = {}
    code = f"import sys; print({MARKER!r}); print({MARKER!r}, file=sys.stderr); sys.exit(3)"
    with pytest.raises(probe.checks.LoginFailed) as raised:
        run.run([sys.executable, "-c", code])
    assert MARKER not in str(raised.value)
    assert "exited 3" in str(raised.value)
    assert run.texts == [f"{MARKER}\n{MARKER}\n"]


def test_a_command_may_fail_unchecked_and_its_output_stays_in_memory(probe):
    """#457 row 5: an unchecked command keeps its output in memory."""
    run = object.__new__(probe.cells.LoginProbe)
    run.texts = []
    run.environment = {}
    code = f"import sys; print({MARKER!r}); sys.exit(2)"
    result = run.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 2
    assert run.texts == [f"{MARKER}\n"]


class FakeRun:
    """What `run_login` drives: each step may raise."""

    def __init__(self, *, enter=None, drive=None, leave=None, failed=(), not_driven=()):
        self.held = ["A"]
        self.failed = list(failed)
        self.not_driven = list(not_driven)
        self.steps = {"enter": enter, "drive": drive, "leave": leave}
        self.ran: list[str] = []

    def step(self, name: str) -> None:
        self.ran.append(name)
        if (error := self.steps[name]) is not None:
            raise error

    def __enter__(self):
        self.step("enter")
        return self

    def __exit__(self, *_):
        self.step("leave")

    def drive(self):
        self.step("drive")


def run(probe, fake: FakeRun | Exception) -> int:
    def open_probe():
        if isinstance(fake, Exception):
            raise fake
        return fake

    return probe.checks.run_login(open_probe)


def test_every_cell_held_passes(probe, capsys, restored_signals):
    """#457 row 1: every cell held ends in PROBE PASSED."""
    fake = FakeRun()
    assert run(probe, fake) == 0
    assert capsys.readouterr().out == "PROBE PASSED: held A\n"
    assert fake.ran == ["enter", "drive", "leave"]


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (ValueError(MARKER), "ValueError"),
        (RuntimeError(MARKER), "RuntimeError"),
    ],
)
@pytest.mark.parametrize("stage", ["enter", "drive", "leave"])
def test_an_unexpected_exception_prints_its_type_name_and_the_teardown_runs(
    probe, capsys, restored_signals, stage, error, shown
):
    """#457 row 6: an unexpected exception prints its type name and the teardown runs."""
    fake = FakeRun(**{stage: error})
    assert run(probe, fake) == 1
    output = capsys.readouterr().out
    assert MARKER not in output
    name = "setup" if stage == "enter" else "drive" if stage == "drive" else "teardown"
    assert f"{name} FAILED ({shown})" in output
    assert output.splitlines()[-1].startswith("PROBE FAILED")
    assert ("leave" in fake.ran) == (stage != "enter")


@pytest.mark.parametrize("which", ["egress", "image", "timeout"])
def test_a_probe_failure_is_withheld_in_setup_and_in_a_stage(
    probe, capsys, restored_signals, which
):
    """#457 row 11: a probe failure is withheld in setup and in a stage."""
    errors = {
        "egress": probe.egress.ProbeFailed(MARKER),
        "image": probe.image.ProbeFailed(MARKER),
        "timeout": subprocess.TimeoutExpired([MARKER], 1),
    }
    assert run(probe, errors[which]) == 1
    output = capsys.readouterr().out
    assert MARKER not in output
    assert "setup FAILED (reason withheld)" in output
    assert run(probe, FakeRun(drive=errors[which])) == 1
    output = capsys.readouterr().out
    assert MARKER not in output
    assert "drive FAILED (reason withheld)" in output


def test_a_login_failure_prints_its_own_message(probe, capsys, restored_signals):
    """#457 row 5: a LoginFailed prints its own fixed message."""
    failure = probe.checks.LoginFailed("up exited 1")
    assert run(probe, FakeRun(drive=failure)) == 1
    assert "drive FAILED: up exited 1" in capsys.readouterr().out


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGALRM])
def test_a_signal_during_a_stage_unwinds_it_and_the_teardown_runs(
    probe, capsys, restored_signals, number
):
    """#457 row 17: a signal during a stage unwinds it and the teardown runs."""
    probe.checks.start_time_bound(1000)
    fake = FakeRun()
    fake.steps["drive"] = None

    def interrupted():
        fake.ran.append("drive")
        signal.raise_signal(number)

    fake.drive = interrupted
    assert run(probe, fake) == 1
    assert fake.ran == ["enter", "drive", "leave"]
    output = capsys.readouterr().out
    assert "interrupted" in output
    assert output.splitlines()[-1].startswith("PROBE FAILED")


def test_a_failed_or_not_driven_cell_fails_the_run_and_is_named(probe, capsys, restored_signals):
    """#457 row 9: a failed or not driven cell fails the run and is named."""
    fake = FakeRun(failed=["U2"], not_driven=["S00 (roots)"])
    assert run(probe, fake) == 1
    assert capsys.readouterr().out == ("PROBE FAILED: held A; failed U2; not driven S00 (roots)\n")


def test_the_teardown_ignores_sigterm_and_cancels_the_alarm(probe, restored_signals):
    """#457 row 17: the teardown ignores SIGTERM and cancels the alarm."""
    probe.checks.start_time_bound(1000)
    seen = {}

    class Observing(FakeRun):
        def __exit__(self, *_):
            seen["handler"] = signal.getsignal(signal.SIGTERM)
            seen["alarm"] = signal.alarm(0)

    run(probe, Observing())
    assert seen == {"handler": signal.SIG_IGN, "alarm": 0}


@pytest.mark.parametrize("failing", ["ProbeFailed", "ValueError"])
def test_a_failing_cell_is_named_and_the_next_cells_still_run(probe, capsys, failing):
    """#457 row 11: a failing cell is named and the next cells still run."""
    checks = probe.checks
    failed: list[str] = []
    error = probe.egress.ProbeFailed(MARKER) if failing == "ProbeFailed" else ValueError(MARKER)
    ran = []
    assert not checks.guarded("U2", lambda: (_ for _ in ()).throw(error), failed)
    assert checks.guarded("U3", lambda: ran.append("U3"), failed)
    output = capsys.readouterr().out
    assert MARKER not in output
    assert failed == ["U2"]
    assert ran == ["U3"]


# --- the cells of the probe --------------------------------------------------------------------


def stub_probe(probe) -> SimpleNamespace:
    """A LoginProbe whose cells are replaced; `drive` is the real one."""
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed, run.not_driven, run.texts = [], [], [], []
    run.wait_until_listening = lambda: None
    for cell in ("config", "u2", "u3", "u6", "u7", "u8", "i6", "s00", "b9", "s1"):
        setattr(run, f"cell_{cell}", lambda cell=cell: run.held.append(cell))
    return run


def test_a_cell_that_fails_does_not_stop_the_cells_after_it(probe, capsys):
    """#457 row 11: a failing cell does not stop the cells after it."""
    run = stub_probe(probe)

    def fails():
        raise probe.image.ProbeFailed(MARKER)

    run.cell_i6 = fails
    run.drive()
    assert run.failed == ["I6"]
    assert run.held[-3:] == ["s00", "b9", "s1"]
    assert capsys.readouterr().out == "I6 FAILED (reason withheld)\n"


def test_no_cell_runs_when_the_service_does_not_listen(probe, capsys):
    """#457 row 11: no cell runs when the service does not listen."""
    run = stub_probe(probe)

    def silent():
        raise probe.egress.ProbeFailed(MARKER)

    run.wait_until_listening = silent
    run.drive()
    assert (run.held, run.failed) == ([], ["listen"])


def test_a_reused_cell_is_held_by_its_name_alone(probe, capsys):
    """#457 row 11: a reused cell is held by its name alone."""
    run = object.__new__(probe.cells.LoginProbe)
    run.held = []
    run.hold("U2", MARKER)
    assert capsys.readouterr().out == "U2 held\n"
    assert run.held == ["U2"]


@pytest.mark.parametrize(
    ("hits", "in_log", "in_other", "verdict"),
    [(0, 0, 0, "held"), (1, 0, 0, "failed"), (0, 1, 0, "failed"), (0, 0, 1, "failed")],
    ids=["clean", "gate hit", "token in the log", "token in an output"],
)
def test_s1_holds_only_when_the_gate_and_every_text_are_clean(
    probe, capsys, hits, in_log, in_other, verdict
):
    """#457 row S1 held: the cell holds only when the gate and every text are clean."""
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed, run.minted = [], [], []
    run.login = SimpleNamespace(token=TOKEN)
    run.gate = SimpleNamespace(hits=hits)
    run.container_log = lambda service: TOKEN * in_log
    run.texts = [TOKEN * in_other]
    run.cell_s1()
    output = capsys.readouterr().out
    assert TOKEN not in output
    expected = (["S1"], []) if verdict == "held" else ([], ["S1"])
    actual = run.held, run.failed
    assert actual == expected
    assert output.startswith(f"S1 {'held' if verdict == 'held' else 'FAILED'}: {hits} gate hits")


def test_u2_expects_each_probes_own_command(probe):
    """#457 row 12: each probe's serves holds only its own command."""
    bootstrap = probe.cells.BOOTSTRAP
    config = probe.cells.SERVICE_COMMAND
    login = object.__new__(probe.cells.LoginProbe)
    import unit

    plain = object.__new__(unit.UnitProbe)
    service = ["/usr/local/bin/python", "-I", "-c", bootstrap, *config]
    runtime = ["/usr/local/bin/agent-runtime-service", *config]
    assert (login.serves(service), login.serves(runtime)) == (True, False)
    assert (plain.serves(service), plain.serves(runtime)) == (False, True)


def census_stub(mounts):
    return SimpleNamespace(
        key=b"\x01",
        mounts=lambda: mounts,
        working_root="/work",
        names=SimpleNamespace(show_mount=lambda root: root),
    )


def s00_run(probe, mounts):
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed, run.not_driven = [], [], []
    run.gate = probe.checks.Gate([])
    run.census = lambda: census_stub(mounts)
    run.container = lambda service: service
    run.cell_s00()
    return run


@pytest.mark.parametrize(
    ("mounts", "held", "failed", "not_driven", "line"),
    [
        ({"/tmp": ((), 0)}, ["S00 (roots)", "S00 (token)"], [], [], "S00 (token) held: 0 files"),
        (
            {"n-0": ((("replaced", 1),), 0)},
            [],
            [],
            ["S00 (roots)", "S00 (token)"],
            "S00 (roots) NOT DRIVEN: 1 directory roots were not listed",
        ),
        (
            {"/tmp": ((), 2)},
            ["S00 (roots)"],
            ["S00 (token)"],
            [],
            "S00 (token) FAILED: 2 files hold the login token",
        ),
        (
            {"/tmp": ((("refused", 1),), 0)},
            ["S00 (roots)"],
            [],
            ["S00 (token)"],
            "S00 (token) NOT DRIVEN: 1 entries could not be read for the token",
        ),
        (
            {"/tmp": ((), 0), "n-0": ((("file mount", 1),), 0)},
            ["S00 (roots)", "S00 (token)"],
            [],
            [],
            "S00 (token) held: 0 files",
        ),
        (
            {},
            [],
            [],
            ["S00 (roots)", "S00 (token)"],
            "S00 (token) NOT DRIVEN: 0 writable mounts were shown",
        ),
    ],
    ids=["clean", "root not listed", "token found", "unreadable file", "file mount", "no mounts"],
)
def test_the_s00_verdicts_decide_the_cells_and_the_run(
    probe, capsys, mounts, held, failed, not_driven, line
):
    """#457 rows 9 and 10: the cell turns the census into held, failed and not driven."""
    census_mounts = {
        shown: probe.census.Mount((("n-0", "file"),) if shown == "/tmp" else (), skips, token_files)
        for shown, (skips, token_files) in mounts.items()
    }
    run = s00_run(probe, census_mounts)
    assert (run.held, run.failed, run.not_driven) == (held, failed, not_driven)
    assert line in capsys.readouterr().out


def test_the_census_key_joins_the_gate(probe):
    """#457 row 4: the census key is gated from the moment the cell asks for it."""
    run = s00_run(probe, {})
    assert run.gate.secrets == ["01"]


# --- the setup of the command ------------------------------------------------------------------


def test_a_lock_that_cannot_be_opened_prints_the_type_alone_and_exits_1(
    probe, tmp_path, capsys, restored_signals
):
    """#457 row 6: a failure before the gate prints no exception text."""
    login = probe.checks.read_login(str(make_login(tmp_path / "login")))
    assert probe.cells.run_command(login, tmp_path / "missing" / "lock", {}) == 1
    captured = capsys.readouterr()
    assert captured.out == "setup FAILED (FileNotFoundError)\nPROBE FAILED: setup\n"
    assert captured.err == ""


def test_a_scratch_directory_inside_the_login_directory_is_refused_and_nothing_is_written(
    probe, tmp_path, capsys, restored_signals
):
    """#457 row 2: the login directory is never written to."""
    directory = make_login(tmp_path / "login")
    (directory / "inner").mkdir()
    login = probe.checks.read_login(str(directory))
    for inside in (directory, directory / "inner"):
        assert probe.cells.run_command(login, tmp_path / "lock", {"TMPDIR": str(inside)}) == 1
        assert capsys.readouterr().out == (
            f"setup FAILED: {probe.checks.Refusal.SCRATCH_IN_LOGIN}\nPROBE FAILED: setup\n"
        )
    assert sorted(path.name for path in directory.iterdir()) == ["inner", "token"]
    assert not (tmp_path / "lock").exists()


def test_a_failed_enter_tears_down_with_the_alarm_cancelled_and_sigterm_ignored(
    probe, tmp_path, restored_signals
):
    """#457 row 17: a signal during the teardown after a failed enter cannot leave the stack."""
    config = resolved(login_source=str(tmp_path / "login"), configuration_source="/scratch/c.toml")
    config["services"]["agent-service"]["volumes"][0]["target"] = probe.cells.login_directory()
    config["services"]["agent-service"]["volumes"][1]["target"] = CONFIGURATION
    seen = []

    def compose(*arguments, **_):
        if arguments[0] == "up":
            raise probe.egress.ProbeFailed(MARKER)
        return SimpleNamespace(stdout=json.dumps(config))

    run = object.__new__(probe.cells.LoginProbe)
    run.login = SimpleNamespace(directory=tmp_path / "login")
    run.configuration = Path("/scratch/c.toml")
    run.listener, run.windows = None, []
    run.compose_file = run.static_file = tmp_path / "unused"
    run.compose = compose
    run.write_on_probe_subnet = lambda *_: None
    run.guard = SimpleNamespace(expect_clean_teardown=lambda: None)

    def docker(command, **_):
        remaining = signal.alarm(0)
        signal.alarm(remaining)  # read the alarm without cancelling it
        seen.append((signal.getsignal(signal.SIGTERM), remaining))
        return subprocess.CompletedProcess(command, 0, "", "")

    run.run = docker
    probe.checks.start_time_bound(1000)
    with pytest.raises(probe.egress.ProbeFailed):
        run.__enter__()
    assert seen[-1] == (signal.SIG_IGN, 0)


# --- the lock ----------------------------------------------------------------------------------


def test_the_command_says_it_waits_for_another_holder_and_starts_after_the_release(probe, tmp_path):
    """#457 row 15: the command says it waits and starts after the release."""
    lock = tmp_path / "lock"
    waiting = threading.Event()
    started = threading.Event()

    def second() -> None:
        with probe.checks.probe_lock(lock, waiting.set):
            started.set()

    with probe.checks.probe_lock(lock, lambda: pytest.fail("the first holder waited")):
        thread = threading.Thread(target=second)
        thread.start()
        assert waiting.wait(timeout=10)
        assert not started.is_set()
    thread.join(timeout=10)
    assert started.is_set()


# --- R2 and B9 (#469) --------------------------------------------------------------------------

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


def r2_probe(probe, tmp_path, timeline, *, watch=None, failed=(), root_entries=0):
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
    run.container_log = lambda service: ""
    run.app_lines = lambda seconds=300: timeline.app(timeline.now)
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
