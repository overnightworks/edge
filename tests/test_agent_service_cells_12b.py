"""The login command and its pure checks (#457, agent-runtime#440's 12b row): the refusals before
any docker call, the preflight of the resolved configuration, the exact-secret gate, the runner
that prints no subprocess output or exception text, and the S00 verdicts. The command itself runs
live under `cells_12b.py --login-directory`."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_support import CONFIGURATION, LOGIN, TOKEN, login_probe_modules, make_login, resolved

MARKER = "SECRET-MARKER-NOT-TO-BE-PRINTED"
STARTED = ("agent-egress", "agent-issuer", "agent-service")


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
    assert capsys.readouterr().out == f"PROBE FAILED: {probe.checks.Refusal.NOT_A_DIRECTORY}\n"


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
    for cell in ("config", "u2", "u3", "u6", "u7", "u8", "i6", "s00", "s1"):
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
    assert run.held[-2:] == ["s00", "s1"]
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
    run.held, run.failed = [], []
    run.login = SimpleNamespace(token=TOKEN)
    run.gate = SimpleNamespace(hits=hits)
    run.container_log = lambda service: TOKEN * in_log
    run.texts = [TOKEN * in_other]
    run.cell_s1()
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert (run.held, run.failed) == (["S1"], []) if verdict == "held" else ([], ["S1"])
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


# --- the refusals before any docker call -------------------------------------------------------


def refusal(probe, text: str) -> str:
    with pytest.raises(probe.checks.LoginFailed) as raised:
        probe.checks.read_login(text)
    return str(raised.value)


def test_a_login_is_read_with_its_token_stripped(probe, tmp_path):
    """#457 row 4: the token file ends in a newline and the login holds it stripped."""
    login = probe.checks.read_login(str(make_login(tmp_path / "login")))
    assert login.token == TOKEN
    assert TOKEN not in repr(login)


def test_a_login_behind_a_symlinked_ancestor_is_resolved_and_accepted(probe, tmp_path):
    """#457 row 2: a path reached through a symlink resolves and is accepted."""
    real = tmp_path / "real"
    real.mkdir()
    make_login(real / "login")
    (tmp_path / "link").symlink_to(real)
    login = probe.checks.read_login(str(tmp_path / "link" / "login"))
    assert login.directory == (real / "login").resolve()


def test_a_missing_directory_and_a_file_are_refused(probe, tmp_path):
    """#457 row 2: no directory is refused naming the flag only."""
    (tmp_path / "file").write_text("x", encoding="utf-8")
    for text in (str(tmp_path / "missing"), str(tmp_path / "file")):
        assert refusal(probe, text) == probe.checks.Refusal.NOT_A_DIRECTORY


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (lambda login: None, "NO_TOKEN_FILE"),
        (lambda login: (login / "token").mkdir(), "NO_TOKEN_FILE"),
        (lambda login: (login / "token").symlink_to(login / "elsewhere"), "NO_TOKEN_FILE"),
        (lambda login: (login / "token").write_bytes(b""), "TOKEN_EMPTY"),
        (lambda login: (login / "token").write_bytes(b" \n\t\n"), "TOKEN_EMPTY"),
        (lambda login: (login / "token").write_bytes(b"x" * (16 * 1024 + 1)), "TOKEN_TOO_LARGE"),
        (lambda login: (login / "token").write_bytes(b"\xff\xfe"), "TOKEN_UNREADABLE"),
    ],
    ids=[
        "no token",
        "token a directory",
        "token a symlink",
        "token empty",
        "token whitespace",
        "token too large",
        "token not text",
    ],
)
def test_a_login_without_a_usable_token_is_refused_without_its_path(
    probe, tmp_path, arrange, reason
):
    """#457 row 2: an unusable token is refused without a path."""
    login = tmp_path / "login"
    login.mkdir()
    arrange(login)
    message = refusal(probe, str(login))
    assert message == probe.checks.Refusal[reason]
    assert str(tmp_path) not in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file")
def test_a_token_that_cannot_be_opened_is_refused(probe, tmp_path):
    """#457 row 2: an unreadable token is refused."""
    login = make_login(tmp_path / "login")
    (login / "token").chmod(0)
    assert refusal(probe, str(login)) == probe.checks.Refusal.TOKEN_UNREADABLE


# --- the preflight -----------------------------------------------------------------------------


def preflight(probe, config: dict) -> None:
    probe.checks.check_resolved(
        config,
        service="agent-service",
        started=STARTED,
        login=(LOGIN, Path("/login")),
        configuration=(CONFIGURATION, Path("/scratch/service.toml")),
    )


def volumes(config: dict) -> list[dict]:
    return config["services"]["agent-service"]["volumes"]


def writable(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config)[index].pop("read_only")


def another_source(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config)[index].update(source="/elsewhere")


def second_bind(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config).append(copy.deepcopy(volumes(config)[index]))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (writable(0), "LOGIN_BIND"),
        (another_source(0), "LOGIN_BIND"),
        (second_bind(0), "LOGIN_BIND"),
        (lambda config: volumes(config).pop(0), "LOGIN_BIND"),
        (writable(1), "CONFIGURATION_BIND"),
        (another_source(1), "CONFIGURATION_BIND"),
        (second_bind(1), "CONFIGURATION_BIND"),
        (
            lambda config: config["services"]["agent-service"].pop("read_only"),
            "WRITABLE_ROOT",
        ),
        (
            lambda config: config["services"]["agent-issuer"].update(
                logging={"driver": "journald"}
            ),
            "LOG_DRIVER",
        ),
        (lambda config: config["services"]["agent-egress"].pop("logging"), "LOG_DRIVER"),
    ],
    ids=[
        "login bind writable",
        "login bind another source",
        "second login bind",
        "no login bind",
        "config bind writable",
        "config bind another source",
        "second config bind",
        "root writable",
        "another log driver",
        "no log driver",
    ],
)
def test_a_resolved_configuration_that_binds_anything_else_is_refused(probe, mutate, reason):
    """#457 row 3 and 16: a resolved configuration with another bind is refused before up."""
    config = resolved()
    mutate(config)
    with pytest.raises(probe.checks.LoginFailed) as raised:
        preflight(probe, config)
    assert str(raised.value) == probe.checks.Refusal[reason]


def test_the_expected_resolved_configuration_passes(probe):
    """#457 row 3 and 16: the expected resolved configuration passes."""
    preflight(probe, resolved())


# --- the gate ----------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("secret", [TOKEN, "census-key-hex", "minted-token"])
def test_a_line_with_a_known_secret_is_replaced_and_counted(probe, capsys, stream, secret):
    """#457 row 4: a printed line holding a known secret is replaced and counted."""
    with probe.checks.gating([TOKEN, "census-key-hex", "minted-token"]) as gate:
        print(f"before {secret} after", file=getattr(sys, stream), flush=True)
    shown = getattr(capsys.readouterr(), "out" if stream == "stdout" else "err")
    assert shown == "before [secret] after\n"
    assert gate.hits == 1


def test_a_last_write_without_a_newline_is_checked_at_exit(probe, capsys):
    """#457 row 4: the unterminated last write is checked at exit."""
    with probe.checks.gating([TOKEN]) as gate:
        sys.stdout.write(f"tail {TOKEN}")
    assert capsys.readouterr().out == "tail [secret]"
    assert gate.hits == 1


def test_a_secret_split_over_two_writes_of_one_line_is_replaced(probe, capsys):
    """#457 row 4: a secret split over two writes of one line is replaced."""
    with probe.checks.gating([TOKEN]):
        sys.stdout.write(TOKEN[:5])
        sys.stdout.write(TOKEN[5:] + "\n")
    assert capsys.readouterr().out == "[secret]\n"


def test_a_secret_added_later_is_gated_and_a_clean_line_passes(probe, capsys):
    """#457 row 4: a secret added later is gated."""
    with probe.checks.gating([TOKEN]) as gate:
        gate.add("late-key")
        gate.add("")
        print("late-key and a clean line", flush=True)
        print("clean", flush=True)
    assert capsys.readouterr().out == "[secret] and a clean line\nclean\n"
    assert gate.hits == 1


def test_the_streams_are_restored_after_the_gate(probe):
    """#457 row 4: the gate restores the streams."""
    before = sys.stdout, sys.stderr
    with probe.checks.gating([TOKEN]):
        assert sys.stdout is not before[0]
        assert sys.stdout.isatty() is False
    assert (sys.stdout, sys.stderr) == before


# --- S00 ---------------------------------------------------------------------------------------


def mount(probe, *, entries=0, skips=(), token_files=0):
    listings = tuple((f"n-{index}", "file") for index in range(entries))
    return probe.census.Mount(listings, tuple(skips), token_files)


@pytest.mark.parametrize(
    ("entries", "skips", "line"),
    [
        (3, (), "S00 role: listed: 3 entries; skips: none; 0 files hold the token"),
        (0, (), "S00 role: listed: 0 entries; skips: none; 0 files hold the token"),
        (
            2,
            (("refused", 1),),
            "S00 role: listed: 2 entries; skips: refused 1; 0 files hold the token",
        ),
        (
            0,
            (("replaced", 1),),
            "S00 role: not listed (replaced); skips: replaced 1; 0 files hold the token",
        ),
        (
            0,
            ((MARKER, 1),),
            "S00 role: not listed (other); skips: other 1; 0 files hold the token",
        ),
    ],
    ids=["entries", "empty", "entries and a skip", "root not listed", "unknown reason"],
)
def test_a_mount_line_says_listed_or_not_listed_with_its_reason(probe, entries, skips, line):
    """#457 row 8: a mount line says listed or not listed with its reason."""
    assert probe.checks.mount_line("role", mount(probe, entries=entries, skips=skips)) == line


@pytest.mark.parametrize(
    ("skips", "unlisted"),
    [
        ((("replaced", 1),), 1),
        ((("refused", 1),), 1),
        ((("login excluded", 1),), 1),
        ((("file mount", 1),), 0),
        ((), 0),
    ],
    ids=["replaced", "refused", "login excluded", "file mount", "listed empty"],
)
def test_a_directory_root_that_was_not_listed_is_counted_and_a_file_mount_is_not(
    probe, skips, unlisted
):
    """#457 row 9: an unlisted directory root is counted, a file mount is not."""
    mounts = {"/tmp": mount(probe, entries=1), "n-0": mount(probe, skips=skips)}
    assert probe.checks.unlisted_roots(mounts) == unlisted


def test_token_files_are_summed_over_every_mount(probe):
    """#457 row 10: token files are summed over every mount."""
    mounts = {"/tmp": mount(probe, token_files=1), "n-0": mount(probe, token_files=2)}
    assert probe.checks.token_files(mounts) == 3
