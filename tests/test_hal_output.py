"""The terminal form every hal command prints: one row, one verdict, one error pair."""

from enum import StrEnum

import pytest

from hal_output import Glyph, Row, write_error, write_row, write_verdict


class Event(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


def test_a_row_is_two_spaces_a_glyph_the_subject_and_the_event_in_fixed_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_row(Row(Glyph.DONE, "timers", Event.CHANGED))
    write_row(Row(Glyph.UNCHANGED, "commit hook", Event.UNCHANGED, "nothing to do"))

    assert capsys.readouterr().out.splitlines() == [
        "  ✓ timers          changed",
        "  · commit hook     unchanged      nothing to do",
    ]


def test_a_row_shows_text_from_outside_as_one_line_of_plain_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_row(Row(Glyph.NEEDS_YOU, "secrets", Event.CHANGED, "a\nb\x1b[31m"))

    assert capsys.readouterr().out == "  ! secrets         changed        a\\nb\\x1b[31m\n"


def test_a_verdict_is_a_glyph_the_summary_and_its_detail_flush_left(
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_verdict(Glyph.CHANGE, "setup plan", "2 open, 2 unchanged · run hal setup --apply")

    assert capsys.readouterr().out == "~ setup plan · 2 open, 2 unchanged · run hal setup --apply\n"


def test_an_error_pair_goes_to_stderr_with_both_lines_escaped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_error("stopped\nat system", "run\x1b[0m it")

    captured = capsys.readouterr()
    assert (captured.out, captured.err.splitlines()) == (
        "",
        ["error: stopped\\nat system", "next: run\\x1b[0m it"],
    )
