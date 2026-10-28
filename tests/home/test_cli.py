from __future__ import annotations

import pytest

from home_tools import cli


@pytest.mark.parametrize("argv", [["--help"], ["secrets", "--help"], ["next", "--help"], ["setup", "--help"]])
def test_help_is_english_and_shows_examples(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)

    help_text = capsys.readouterr().out
    assert exited.value.code == 0
    assert "Examples:" in help_text
    assert help_text.isascii()


def test_home_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main([])

    assert exited.value.code == 2


def test_an_unexpected_error_exits_3_with_one_error_line_never_1(
    clean_home, probe, capsys: pytest.CaptureFixture[str]
) -> None:
    def runner_that_breaks(*_arguments, **_keywords):
        raise RuntimeError("boom")

    code = cli.main(["setup"], home=clean_home, run=runner_that_breaks, probe=probe)

    error = capsys.readouterr().err
    assert code == 3
    assert error == "error: unexpected error (RuntimeError): boom\n"
