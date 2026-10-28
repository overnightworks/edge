"""The door `hal`: its help, its error form, its exit codes, run as the console script is run
(`hal_program`) from a directory no checkout encloses, under a throwaway HOME."""

import contextlib
import os
import signal
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from git_support import (
    SOURCE,
    assert_quiet_run,
    elsewhere,
    hal_command,
    hal_environment,
    hal_program,
    main_checkout,
    run_hal,
)

from hal import backup_commands, check_commands, cli, legal_commands, stack_commands
from hal.grammar import Asks, Command, ExitCode
from hal_output.command import Verb

LOCK = SOURCE.parent / "uv.lock"
DEADLINE_SECONDS = 30
FORBIDDEN_STDIN = """
class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("stdin was touched")
sys.stdin = Forbidden()
"""


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME and XDG_CONFIG_HOME: no global config but the one a test writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LC_ALL", "C")
    return home


def error_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return result.stderr.splitlines()


def test_hal_alone_shows_the_usage_on_stdout_and_nothing_on_stderr(tmp_path):
    """G-1"""
    result = run_hal(tmp_path, hal_program())

    assert (result.returncode, result.stderr) == (0, "")
    assert "usage: hal" in result.stdout


def test_the_help_lists_setup_with_one_sentence(tmp_path):
    result = run_hal(tmp_path, hal_program(), "--help")

    setup_lines = [line for line in result.stdout.splitlines() if line.strip().startswith("setup ")]
    assert len(setup_lines) == 1
    assert "commit hook" in setup_lines[0]


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_the_help_flags_show_what_hal_alone_shows(tmp_path, flag):
    """G-2"""
    alone = run_hal(tmp_path, hal_program())

    flagged = run_hal(tmp_path, hal_program(), flag)

    assert (flagged.returncode, flagged.stdout) == (0, alone.stdout)


def test_a_command_shows_its_own_help(tmp_path):
    """G-3"""
    result = run_hal(tmp_path, hal_program(), "version", "-h")

    assert result.returncode == 0
    assert result.stdout.startswith("usage: hal version")
    assert "--verbose" in result.stdout


def test_an_unknown_command_is_a_usage_error_that_names_the_help(tmp_path):
    """G-4"""
    result = run_hal(tmp_path, hal_program(), "frobnicate")

    assert result.returncode == ExitCode.USAGE
    assert result.stdout == ""
    assert error_lines(result)[0].startswith("error: ")
    assert error_lines(result)[1:] == ["next: hal --help"]


@pytest.mark.parametrize("option", ["--bogus", "-y", "x\nnext: y\x1b[31m"])
def test_an_unknown_option_names_the_help_of_its_command(tmp_path, option):
    """G-5: there is no `-y` (#132 decision 4); text from the command line cannot add a line or a
    control character to the error form."""
    result = run_hal(tmp_path, hal_program(), "version", option)

    assert result.returncode == ExitCode.USAGE
    assert result.stdout == ""
    assert error_lines(result)[0].startswith("error: ")
    assert error_lines(result)[1:] == ["next: hal version --help"]
    assert "\x1b" not in result.stderr


@pytest.mark.parametrize("command", cli.COMMANDS, ids=lambda command: command.name)
def test_every_command_declares_whether_it_prints_json_and_whether_it_asks(command):
    """G-10: a later command joins by registering, and cannot leave either out."""
    assert isinstance(command.json, bool)
    assert isinstance(command.asks, Asks)


@pytest.mark.parametrize(
    "command", [command for command in cli.COMMANDS if not command.json], ids=lambda c: c.name
)
def test_a_command_without_json_refuses_the_flag(tmp_path, command):
    """G-6: `--json` only where a program reads the result (#132 decision 5)."""
    result = run_hal(tmp_path, hal_program(), command.name, "--json")

    assert result.returncode == ExitCode.USAGE
    assert error_lines(result)[1:] == [f"next: hal {command.name} --help"]


def test_a_command_that_declares_json_offers_the_flag():
    report = Command(
        Verb.CHECK, "print a report", lambda arguments: ExitCode.OK, lambda p: None,
        json=True, asks=Asks.NEVER,
    )  # fmt: skip

    parsed = cli.build_parser([report]).parse_args(["check", "--json"])

    assert parsed.json is True


def test_an_unexpected_error_shows_only_its_type(tmp_path):
    """G-7: the message of the exception is someone else's text."""
    fault = """
import hal.version
class Poison:
    def __truediv__(self, other):
        raise RuntimeError("do not print this")
hal.version.CODE_TREE = Poison()
"""
    result = run_hal(tmp_path, hal_program(before=fault), "version")

    assert result.returncode == ExitCode.FAILURE
    assert result.stdout == ""
    assert result.stderr == "error: unexpected error (RuntimeError)\n"


def fake_git(directory: Path, body: str) -> Path:
    """An executable `git` first on PATH that runs `body`."""
    directory.mkdir()
    script = directory / "git"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return directory


def test_a_git_that_fails_is_a_failure_that_names_the_checkout(tmp_path):
    """G-9"""
    tree = main_checkout(tmp_path)
    failing = fake_git(tmp_path / "fail", "#!/bin/sh\necho 'fatal: x' >&2\nexit 128\n")

    result = run_hal(tmp_path, hal_program(tree), "version", path_first=failing)

    assert result.returncode == ExitCode.FAILURE
    assert result.stdout == ""
    assert error_lines(result)[0].startswith(f"error: cannot read the checkout {tree}, ")
    assert "fatal: x" in error_lines(result)[0]


def test_ctrl_c_while_git_runs_exits_130_without_a_traceback(tmp_path):
    """G-8: git's group is cleaned up by `run_git` (test_edge_checkout), not asserted here."""
    tree = main_checkout(tmp_path)
    started, silence = tmp_path / "started", tmp_path / "silence"
    os.mkfifo(silence)
    blocking = fake_git(
        tmp_path / "block",
        f"#!{sys.executable} -IS\n"
        f"import os\n"
        f"with open({str(started)!r}, 'a') as file:\n"
        f"    file.write(str(os.getpid()) + '\\n')\n"
        f"open({str(silence)!r}, 'rb').read(1)\n",
    )
    interruptible = "import signal\nsignal.signal(signal.SIGINT, signal.default_int_handler)\n"
    holder = os.open(silence, os.O_RDWR)  # a writer that never writes: the fake blocks on read
    hal = subprocess.Popen(
        hal_command(hal_program(tree, before=interruptible), "version"),
        cwd=elsewhere(tmp_path), env=hal_environment(path_first=blocking),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", start_new_session=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        while not (started.exists() and started.read_text()):
            assert time.monotonic() < deadline, "git never started"
            time.sleep(0.02)
        os.killpg(hal.pid, signal.SIGINT)
        stdout, stderr = hal.communicate(timeout=DEADLINE_SECONDS)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(hal.pid, signal.SIGKILL)
        if started.exists() and started.read_text():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(started.read_text().split()[0]), signal.SIGKILL)
        hal.communicate()
        os.close(holder)

    assert (hal.returncode, stdout) == (ExitCode.INTERRUPTED, "")
    assert_quiet_run(stdout, stderr)


@pytest.mark.parametrize(
    "command",
    [
        command
        for command in cli.COMMANDS
        if command.asks is Asks.NEVER
        and command not in stack_commands.COMMANDS
        and command not in backup_commands.COMMANDS
        and command not in legal_commands.COMMANDS
        and command not in check_commands.COMMANDS
    ],
    ids=lambda command: command.name,
)
def test_a_command_that_never_asks_never_reads_stdin(tmp_path, command):
    """G-11. The stack's commands are left out: run here they would reach the real system manager;
    their stdin cells run on fakes in test_hal_stack_commands. The backup commands would reach the
    real repository; their stdin cell runs on the fake host in test_edge_backup. `hal check` would
    run the real docker and ss; its cell runs on fakes in test_hal_check. `hal legal` needs a key;
    its stdin cell runs with its own arguments in test_hal_legal. `hal secrets` runs `--by-service`,
    the one form that reads no home and starts no probe."""
    tree = main_checkout(tmp_path)

    extra_arguments = {"rename": ("former", "current"), "secrets": ("--by-service",)}
    arguments = extra_arguments.get(command.name, ())

    result = run_hal(tmp_path, hal_program(tree, before=FORBIDDEN_STDIN), command.name, *arguments)

    assert result.returncode == ExitCode.OK


def test_hal_secrets_has_no_setup_option_and_names_its_own_help(tmp_path):
    """The wizard has one door, `hal setup --apply`; `hal secrets` only views."""
    result = run_hal(tmp_path, hal_program(), "secrets", "--setup")

    assert result.returncode == ExitCode.USAGE
    assert result.stdout == ""
    assert error_lines(result) == [
        "error: unrecognized arguments: --setup",
        "next: hal secrets --help",
    ]


def test_no_package_of_the_lock_is_named_hal():
    """G-23, the lock half: `hal` is the import name of this tree alone (docs/naming.md)."""
    packages = tomllib.loads(LOCK.read_text())["package"]

    assert "hal" not in {package["name"] for package in packages}
