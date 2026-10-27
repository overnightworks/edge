from __future__ import annotations

import os

import pytest
from conftest import FakeDisk

from home_tools import cli
from home_tools.layout import Layout


@pytest.mark.parametrize(
    "config_text",
    ["[notify]\nnot_a_real_key = 1\n", "[notify]\nenabled = [\n", "[other]\nenabled = true\n"],
)
def test_check_exits_2_on_a_config_it_cannot_trust(clean_home, runner, capsys: pytest.CaptureFixture[str], config_text: str) -> None:
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text(config_text)

    code = cli.main(["check"], home=clean_home, run=runner)

    assert code == 2
    assert capsys.readouterr().err.startswith("home: ")


def test_a_tidy_table_in_the_config_is_refused_by_name(clean_home, runner, capsys: pytest.CaptureFixture[str]) -> None:
    """#486 D1: the operator's old file holds only a [tidy] table, so the message says to delete it."""
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[tidy]\ntrash_days = 5\n")

    code = cli.main(["check"], home=clean_home, run=runner)

    assert code == 2
    assert capsys.readouterr().err == f"home: the [tidy] table is no longer read; delete it from {config_file}\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_an_unreadable_config_exits_2(clean_home, runner, capsys: pytest.CaptureFixture[str]) -> None:
    config_file = Layout(clean_home).config_file
    config_file.parent.mkdir(parents=True)
    config_file.write_text("[notify]\nenabled = true\n")
    config_file.chmod(0)
    try:
        code = cli.main(["check"], home=clean_home, run=runner)
    finally:
        config_file.chmod(0o600)

    assert code == 2
    assert capsys.readouterr().err.startswith("home: ")


@pytest.mark.parametrize("argv", [["--help"], ["check", "--help"], ["setup", "--help"]])
def test_help_shows_german_examples(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)

    assert exited.value.code == 0
    assert "Beispiele:" in capsys.readouterr().out


def test_home_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main([])

    assert exited.value.code == 2


@pytest.mark.parametrize("subcommand", ["check"])
def test_an_unexpected_error_exits_3_with_one_german_line_never_1(
    clean_home, runner, probe, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    state_dir = Layout(clean_home).state_dir
    state_dir.parent.mkdir(parents=True)
    state_dir.write_text("a file where the state directory belongs")

    code = cli.main([subcommand], home=clean_home, run=runner, probe=probe, disk_space=FakeDisk())

    error = capsys.readouterr().err
    assert code == 3
    assert error.startswith("home: unerwarteter Fehler (")
    assert error.count("\n") == 1


def test_the_help_promises_a_notification_only_when_an_action_appears_or_goes(capsys: pytest.CaptureFixture[str]) -> None:
    """#415 Z4: the help says a mail comes only when an action appears or goes."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    help_text = " ".join(capsys.readouterr().out.split())  # argparse wraps long help lines
    assert "meldet nur, wenn ein Handlungsbedarf neu auftaucht oder verschwindet" in help_text
