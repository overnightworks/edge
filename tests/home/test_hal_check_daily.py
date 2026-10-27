"""Tests of the bare `hal check`, the daily check on the terminal."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import FakeDisk, FakeProbe, ProbeThatCannotStart, RunnerWithCleanLogs, Sandbox, message_of, restarting_containers

from hal import check_commands
from hal import cli as hal_cli
from home_tools import cli as home_cli
from home_tools.layout import Layout


pytestmark = pytest.mark.usefixtures("quiet_pc")

ACTION_TITLE = "PC check: action needed – Services"


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


@pytest.fixture
def hal_check(clean_home, runner, monkeypatch, capsys) -> Callable[..., tuple[int, str, str]]:
    """`hal check` through `hal.cli.main`, over the clean home and the fake runner instead of this machine."""

    def run(*arguments: str, probe: FakeProbe | None = None) -> tuple[int, str, str]:
        ports = lambda: home_cli.build_layout_and_ports(home=clean_home, run=runner, probe=probe or FakeProbe(), disk_space=FakeDisk())  # noqa: E731
        monkeypatch.setattr(check_commands, "build_layout_and_ports", ports)
        exit_code = hal_cli.main(["check", *arguments])
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return run


def test_a_bare_hal_check_on_a_clean_pc_prints_the_rows_and_the_verdict_and_exits_0(hal_check) -> None:
    exit_code, out, err = hal_check()

    assert (exit_code, err) == (0, "")
    assert out.splitlines()[0] == "  ✓ services        ok"
    assert "all well" in out.splitlines()[-1]


def test_a_bare_hal_check_with_an_action_exits_1_and_delivers_the_news_and_the_report_like_home_check(hal_check, clean_home, runner) -> None:
    restarting_containers(runner, "presentator-live")

    exit_code, out, err = hal_check()

    assert (exit_code, err) == (1, "")
    assert "1 action" in out
    assert runner.notifications == [ACTION_TITLE]
    assert "| container-restarting |" in Layout(clean_home).check_report_file.read_text()


def test_a_bare_hal_check_mails_the_news_once(hal_check, runner, mail_stub) -> None:
    restarting_containers(runner, "presentator-live")

    hal_check()
    hal_check()

    assert len(mail_stub.sent) == 1


def test_a_bare_hal_check_with_a_broken_area_exits_3_and_names_hal_check_as_the_step(hal_check, runner, mail_stub) -> None:
    restarting_containers(runner, "presentator-live")

    exit_code, out, err = hal_check(probe=ProbeThatCannotStart())

    assert exit_code == 3
    assert "  ✗ secrets         not checked" in out
    assert err.splitlines() == ["error: 1 of 5 areas could not check · mail sent", "next: hal check"]
    assert "Run `hal check`." in message_of(mail_stub.sent[0]).get_content()
    assert runner.notifications == ["PC check: action needed – Services, Secrets"]


def test_a_bare_hal_check_with_an_invalid_manifest_keeps_the_rows_and_names_the_file(hal_check, use_manifest) -> None:
    manifest = use_manifest('[[secret]]\nname = "e"\n')

    exit_code, out, err = hal_check()

    assert exit_code == 2
    assert "  ✗ secrets         not checked" in out
    assert "all well" not in out
    assert err.startswith(f"error: {manifest}: ")
    assert err.count("\n") == 1


@pytest.mark.parametrize("config_text", ["this is not toml\n", "bogus = 1\n"], ids=["syntax error", "unknown key"])
def test_a_bare_hal_check_with_an_invalid_config_names_the_file_once_and_checks_nothing(
    hal_check, clean_home, config_text
) -> None:
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(config_text)

    exit_code, out, err = hal_check()

    assert (exit_code, out) == (2, "")
    assert err.startswith("error: ")
    assert err.count(str(config_file)) == 1
    assert err.count("\n") == 1


def test_a_bare_hal_check_is_not_a_json_command(hal_check, runner) -> None:
    exit_code, out, err = hal_check("--json")

    assert (exit_code, out) == (2, "")
    assert err.splitlines() == ["error: --json belongs to one area", "next: hal check exposure --json"]
    assert runner.calls == []


class ProbeThatIsInterrupted(FakeProbe):
    def __call__(self, argv: Sequence[str]) -> int | None:
        raise KeyboardInterrupt


def test_ctrl_c_in_a_bare_hal_check_exits_130_without_an_unexpected_error(hal_check) -> None:
    exit_code, _, err = hal_check(probe=ProbeThatIsInterrupted())

    assert exit_code == 130
    assert "unexpected error" not in err


def test_hal_check_help_names_the_daily_check_before_the_areas(capsys) -> None:
    with pytest.raises(SystemExit):
        hal_cli.main(["check", "--help"])

    help_lines = capsys.readouterr().out.splitlines()
    daily = next(index for index, line in enumerate(help_lines) if line.startswith("hal check  ") and "daily check" in line)
    assert daily < next(index for index, line in enumerate(help_lines) if line.startswith("hal check exposure"))
