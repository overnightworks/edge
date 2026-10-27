"""The login command's pure side (#457, agent-runtime#440's 12b row): the refusals before any
docker call, the preflight of the resolved configuration, the exact-secret gate, the runner that
prints no subprocess output or exception text, and the S00 verdicts. The command itself runs live
under `cells_12b.py --login-directory`."""

from __future__ import annotations

import copy
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_support import probe_imports

MARKER = "SECRET-MARKER-NOT-TO-BE-PRINTED"
TOKEN = "sk-ant-oat01-dummy-token"
LOGIN = "/run/agent-service/login/claude"
CONFIGURATION = "/etc/agent-service/service.toml"
STARTED = ("agent-egress", "agent-issuer", "agent-service")


@pytest.fixture(scope="module")
def probe() -> Iterator[SimpleNamespace]:
    with probe_imports():
        import cells_12b
        import census_client
        import egress
        import image
        import login_checks

        yield SimpleNamespace(
            cells=cells_12b, checks=login_checks, census=census_client, egress=egress, image=image
        )


@pytest.fixture
def restored_signals() -> Iterator[None]:
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGALRM)}
    yield
    signal.alarm(0)
    for number, handler in handlers.items():
        signal.signal(number, handler)


def make_login(directory: Path, content: bytes = TOKEN.encode() + b"\n") -> Path:
    directory.mkdir()
    (directory / "token").write_bytes(content)
    return directory


# --- the refusals before any docker call -------------------------------------------------------


def refusal(probe, text: str) -> str:
    with pytest.raises(probe.checks.LoginFailed) as raised:
        probe.checks.read_login(text)
    return str(raised.value)


def test_a_login_is_read_with_its_token_stripped(probe, tmp_path):
    login = probe.checks.read_login(str(make_login(tmp_path / "login")))
    assert login.token == TOKEN
    assert TOKEN not in repr(login)


def test_a_login_behind_a_symlinked_ancestor_is_resolved_and_accepted(probe, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    make_login(real / "login")
    (tmp_path / "link").symlink_to(real)
    login = probe.checks.read_login(str(tmp_path / "link" / "login"))
    assert login.directory == (real / "login").resolve()


def test_a_missing_directory_and_a_file_are_refused(probe, tmp_path):
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
    login = tmp_path / "login"
    login.mkdir()
    arrange(login)
    message = refusal(probe, str(login))
    assert message == probe.checks.Refusal[reason]
    assert str(tmp_path) not in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file")
def test_a_token_that_cannot_be_opened_is_refused(probe, tmp_path):
    login = make_login(tmp_path / "login")
    (login / "token").chmod(0)
    assert refusal(probe, str(login)) == probe.checks.Refusal.TOKEN_UNREADABLE


def test_the_missing_flag_stops_the_command_before_it_starts_anything(probe, monkeypatch):
    monkeypatch.setattr(probe.cells, "run_command", lambda *_: pytest.fail("started"))
    monkeypatch.setattr(sys, "argv", ["cells_12b.py"])
    with pytest.raises(SystemExit):
        probe.cells.main()


def test_a_refused_login_prints_the_reason_and_exits_1_before_the_lock(
    probe, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(probe.cells, "run_command", lambda *_: pytest.fail("started"))
    monkeypatch.setattr(sys, "argv", ["cells_12b.py", "--login-directory", str(tmp_path / "x")])
    assert probe.cells.main() == 1
    assert capsys.readouterr().out == f"PROBE FAILED: {probe.checks.Refusal.NOT_A_DIRECTORY}\n"


# --- the preflight -----------------------------------------------------------------------------


def resolved(*, login_source="/login", configuration_source="/scratch/service.toml") -> dict:
    return {
        "services": {
            "agent-service": {
                "read_only": True,
                "logging": {"driver": "local"},
                "volumes": [
                    {"type": "bind", "source": login_source, "target": LOGIN, "read_only": True},
                    {
                        "type": "bind",
                        "source": configuration_source,
                        "target": CONFIGURATION,
                        "read_only": True,
                    },
                ],
            },
            "agent-egress": {"logging": {"driver": "local"}},
            "agent-issuer": {"logging": {"driver": "local"}},
        }
    }


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
    config = resolved()
    mutate(config)
    with pytest.raises(probe.checks.LoginFailed) as raised:
        preflight(probe, config)
    assert str(raised.value) == probe.checks.Refusal[reason]


def test_the_expected_resolved_configuration_passes(probe):
    preflight(probe, resolved())


# --- the gate ----------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("secret", [TOKEN, "census-key-hex", "minted-token"])
def test_a_line_with_a_known_secret_is_replaced_and_counted(probe, capsys, stream, secret):
    with probe.checks.gating([TOKEN, "census-key-hex", "minted-token"]) as gate:
        print(f"before {secret} after", file=getattr(sys, stream), flush=True)
    shown = getattr(capsys.readouterr(), "out" if stream == "stdout" else "err")
    assert shown == "before [secret] after\n"
    assert gate.hits == 1


def test_a_last_write_without_a_newline_is_checked_at_exit(probe, capsys):
    with probe.checks.gating([TOKEN]) as gate:
        sys.stdout.write(f"tail {TOKEN}")
    assert capsys.readouterr().out == "tail [secret]"
    assert gate.hits == 1


def test_a_secret_split_over_two_writes_of_one_line_is_replaced(probe, capsys):
    with probe.checks.gating([TOKEN]):
        sys.stdout.write(TOKEN[:5])
        sys.stdout.write(TOKEN[5:] + "\n")
    assert capsys.readouterr().out == "[secret]\n"


def test_a_secret_added_later_is_gated_and_a_clean_line_passes(probe, capsys):
    with probe.checks.gating([TOKEN]) as gate:
        gate.add("late-key")
        gate.add("")
        print("late-key and a clean line", flush=True)
        print("clean", flush=True)
    assert capsys.readouterr().out == "[secret] and a clean line\nclean\n"
    assert gate.hits == 1


def test_the_streams_are_restored_after_the_gate(probe):
    before = sys.stdout, sys.stderr
    with probe.checks.gating([TOKEN]):
        assert sys.stdout is not before[0]
        assert sys.stdout.isatty() is False
    assert (sys.stdout, sys.stderr) == before


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
    assert str(probe.checks.failed_command(command, 3)) == f"{verb} exited 3"


def test_a_failed_command_of_the_probe_relays_no_output(probe):
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
    run = object.__new__(probe.cells.LoginProbe)
    run.texts = []
    run.environment = {}
    result = run.run([sys.executable, "-c", "import sys; sys.exit(2)"], check=False)
    assert result.returncode == 2


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


@pytest.fixture
def untouched_signals(restored_signals):
    return restored_signals


def test_every_cell_held_passes(probe, capsys, untouched_signals):
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
    probe, capsys, untouched_signals, stage, error, shown
):
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
    probe, capsys, untouched_signals, which
):
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


def test_a_login_failure_prints_its_own_message(probe, capsys, untouched_signals):
    failure = probe.checks.LoginFailed("up exited 1")
    assert run(probe, FakeRun(drive=failure)) == 1
    assert "drive FAILED: up exited 1" in capsys.readouterr().out


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGALRM])
def test_a_signal_during_a_stage_unwinds_it_and_the_teardown_runs(
    probe, capsys, restored_signals, number
):
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


def test_a_failed_or_not_driven_cell_fails_the_run_and_is_named(probe, capsys, untouched_signals):
    fake = FakeRun(failed=["U2"], not_driven=["S00 (roots)"])
    assert run(probe, fake) == 1
    assert capsys.readouterr().out == ("PROBE FAILED: held A; failed U2; not driven S00 (roots)\n")


def test_the_teardown_ignores_sigterm_and_cancels_the_alarm(probe, restored_signals):
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
    run = stub_probe(probe)

    def fails():
        raise probe.image.ProbeFailed(MARKER)

    run.cell_i6 = fails
    run.drive()
    assert run.failed == ["I6"]
    assert run.held[-2:] == ["s00", "s1"]
    assert capsys.readouterr().out == "I6 FAILED (reason withheld)\n"


def test_no_cell_runs_when_the_service_does_not_listen(probe, capsys):
    run = stub_probe(probe)

    def silent():
        raise probe.egress.ProbeFailed(MARKER)

    run.wait_until_listening = silent
    run.drive()
    assert (run.held, run.failed) == ([], ["listen"])


def test_a_reused_cell_is_held_by_its_name_alone(probe, capsys):
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
    run = object.__new__(probe.cells.LoginProbe)
    run.held, run.failed = [], []
    run.login = SimpleNamespace(token=TOKEN)
    run.gate = SimpleNamespace(hits=hits)
    log = TOKEN * in_log
    run.container_log = lambda service: log
    run.texts = [log, TOKEN * in_other]
    run.cell_s1()
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert (run.held, run.failed) == (["S1"], []) if verdict == "held" else ([], ["S1"])
    assert output.startswith(f"S1 {'held' if verdict == 'held' else 'FAILED'}: {hits} gate hits")


def test_u2_expects_each_probes_own_command(probe):
    bootstrap = probe.cells.BOOTSTRAP
    config = probe.cells.SERVICE_COMMAND
    login = object.__new__(probe.cells.LoginProbe)
    import unit

    plain = object.__new__(unit.UnitProbe)
    service = ["/usr/local/bin/python", "-I", "-c", bootstrap, *config]
    runtime = ["/usr/local/bin/agent-runtime-service", *config]
    assert (login.serves(service), login.serves(runtime)) == (True, False)
    assert (plain.serves(service), plain.serves(runtime)) == (False, True)


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
    mounts = {"/tmp": mount(probe, entries=1), "n-0": mount(probe, skips=skips)}
    assert probe.checks.unlisted_roots(mounts) == unlisted


def test_token_files_are_summed_over_every_mount(probe):
    mounts = {"/tmp": mount(probe, token_files=1), "n-0": mount(probe, token_files=2)}
    assert probe.checks.token_files(mounts) == 3


# --- the lock ----------------------------------------------------------------------------------


def test_the_command_says_it_waits_for_another_holder_and_starts_after_the_release(probe, tmp_path):
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
