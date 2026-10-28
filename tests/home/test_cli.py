from __future__ import annotations

import pytest

from home_tools import cli


@pytest.mark.parametrize(
    ("argv", "heading"),
    [(["--help"], "Examples:"), (["secrets", "--help"], "Examples:"), (["next", "--help"], "Examples:"), (["setup", "--help"], "Examples:")],
)
def test_help_shows_examples(argv: list[str], heading: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)

    assert exited.value.code == 0
    assert heading in capsys.readouterr().out


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
