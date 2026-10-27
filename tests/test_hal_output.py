"""The terminal form every hal command prints: one row, one verdict, one error pair."""

from enum import StrEnum

import pytest

from hal_output import Glyph, Row, Verdict, write_error, write_row, write_verdict


class Event(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


def test_a_row_is_two_spaces_a_glyph_the_subject_and_the_event_in_fixed_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 invariant 3: a row is glyph, subject, event and fact in fixed columns."""

    write_row(Row(Glyph.DONE, "timers", Event.CHANGED))
    write_row(Row(Glyph.UNCHANGED, "commit hook", Event.UNCHANGED, "nothing to do"))

    assert capsys.readouterr().out.splitlines() == [
        "  ✓ timers          changed",
        "  · commit hook     unchanged      nothing to do",
    ]


def test_a_row_shows_text_from_outside_as_one_line_of_plain_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 invariant 4: the owner escapes a fact from outside before any renderer sees it."""

    write_row(Row(Glyph.NEEDS_YOU, "secrets", Event.CHANGED, "a\nb\x1b[31m"))

    assert capsys.readouterr().out == "  ! secrets         changed        a\\nb\\x1b[31m\n"


def test_a_verdict_is_a_glyph_the_summary_its_counts_and_its_hint_flush_left(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 invariant 3: the run ends in one verdict line; the renderer leaves a zero count out."""

    write_verdict(
        Verdict(
            Glyph.CHANGE,
            "setup plan",
            ((2, Event.CHANGED), (0, Event.SKIPPED), (2, Event.UNCHANGED)),
            "run hal setup --apply",
        )
    )
    write_verdict(Verdict(Glyph.DONE, "setup done", ((1, Event.CHANGED),)))

    assert capsys.readouterr().out.splitlines() == [
        "~ setup plan · 2 changed, 2 unchanged · run hal setup --apply",
        "✓ setup done · 1 changed",
    ]


def test_an_error_pair_goes_to_stderr_with_both_lines_escaped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 invariant 3 and 4: the run may end in one `error:`/`next:` pair, escaped, on stderr."""

    write_error("stopped\nat system", "run\x1b[0m it")

    captured = capsys.readouterr()
    assert (captured.out, captured.err.splitlines()) == (
        "",
        ["error: stopped\\nat system", "next: run\\x1b[0m it"],
    )
