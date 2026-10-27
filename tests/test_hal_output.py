"""The terminal form every hal command prints: one row, one verdict, one error pair."""

import io
import sys
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


class Terminal(io.StringIO):
    """A stream a person watches."""

    def isatty(self) -> bool:
        return True


def put_on_terminals(monkeypatch: pytest.MonkeyPatch) -> tuple[Terminal, Terminal]:
    """Both streams on a terminal, neither NO_COLOR nor TERM=dumb set. Called in the test body:
    pytest swaps `sys.stdout` again between fixture setup and the call."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    out, err = Terminal(), Terminal()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_on_a_terminal_a_row_colours_only_its_glyph_and_dims_its_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: the glyph repeats the word in colour; subject and event keep the default."""

    out, _err = put_on_terminals(monkeypatch)
    write_row(Row(Glyph.DONE, "timers", Event.CHANGED, "three units"))
    write_row(Row(Glyph.FAILED, "system", Event.CHANGED))
    write_row(Row(Glyph.NEEDS_YOU, "secrets", Event.CHANGED))
    write_row(Row(Glyph.CHANGE, "commit hook", Event.CHANGED))

    assert out.getvalue().splitlines() == [
        "  \x1b[32m✓\x1b[0m timers          changed        \x1b[2mthree units\x1b[0m",
        "  \x1b[31m✗\x1b[0m system          changed",
        "  \x1b[33m!\x1b[0m secrets         changed",
        "  \x1b[34m~\x1b[0m commit hook     changed",
    ]


def test_on_a_terminal_an_unchanged_row_is_dim_as_a_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: a `·` row steps back entirely."""

    out, _err = put_on_terminals(monkeypatch)
    write_row(Row(Glyph.UNCHANGED, "commit hook", Event.UNCHANGED, "nothing to do"))

    assert out.getvalue() == ("\x1b[2m  · commit hook     unchanged      nothing to do\x1b[0m\n")


@pytest.mark.parametrize(
    ("glyph", "style"),
    [
        (Glyph.DONE, "1"),
        (Glyph.CHANGE, "1;34"),
        (Glyph.NEEDS_YOU, "1;33"),
        (Glyph.FAILED, "1;31"),
    ],
)
def test_on_a_terminal_a_verdict_is_bold_in_its_glyphs_colour_with_dim_counts(
    monkeypatch: pytest.MonkeyPatch, glyph: Glyph, style: str
) -> None:
    """#495: the glyph and summary are bold, the counts dim, the hint default."""

    out, _err = put_on_terminals(monkeypatch)
    write_verdict(Verdict(glyph, "setup", ((2, Event.CHANGED),), "run hal"))

    assert out.getvalue() == (
        f"\x1b[{style}m{glyph.value} setup\x1b[0m · \x1b[2m2 changed\x1b[0m · run hal\n"
    )


def test_on_a_terminal_a_verdict_without_counts_carries_no_empty_dim_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _err = put_on_terminals(monkeypatch)
    write_verdict(Verdict(Glyph.DONE, "setup", ((0, Event.CHANGED),), "run hal"))

    assert out.getvalue() == "\x1b[1m✓ setup\x1b[0m · run hal\n"


@pytest.mark.parametrize("failure", [AttributeError, ValueError])
def test_a_stream_without_a_working_isatty_prints_plain(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    class Broken(io.StringIO):
        def isatty(self) -> bool:
            raise failure

    out = Broken()
    monkeypatch.setattr(sys, "stdout", out)
    write_row(Row(Glyph.DONE, "timers", Event.CHANGED, "three units"))

    assert out.getvalue() == "  ✓ timers          changed        three units\n"


def test_on_a_terminal_an_error_pair_is_bold_red_error_and_cyan_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: only the labels carry colour, the rest of each line stays default."""

    out, err = put_on_terminals(monkeypatch)
    write_error("stopped", "run it")

    assert (out.getvalue(), err.getvalue()) == (
        "",
        "\x1b[1;31merror:\x1b[0m stopped\n\x1b[36mnext:\x1b[0m run it\n",
    )


def test_colour_is_decided_per_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: stdout in a pipe stays plain while stderr on a terminal is coloured, and back."""

    _out, err = put_on_terminals(monkeypatch)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    write_row(Row(Glyph.DONE, "timers", Event.CHANGED))
    write_error("stopped")

    assert sys.stdout.getvalue() == "  ✓ timers          changed\n"
    assert err.getvalue() == "\x1b[1;31merror:\x1b[0m stopped\n"


@pytest.mark.parametrize(
    ("variable", "value"), [("NO_COLOR", "1"), ("NO_COLOR", "anything"), ("TERM", "dumb")]
)
def test_a_terminal_that_asks_for_no_colour_gets_the_plain_bytes(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    """#296 invariant 2: NO_COLOR or TERM=dumb turns colour off on a terminal."""

    out, err = put_on_terminals(monkeypatch)
    monkeypatch.setenv(variable, value)

    write_row(Row(Glyph.UNCHANGED, "commit hook", Event.UNCHANGED, "nothing to do"))
    write_verdict(Verdict(Glyph.DONE, "setup", ((1, Event.CHANGED),)))
    write_error("stopped", "run it")

    assert out.getvalue().splitlines() == [
        "  · commit hook     unchanged      nothing to do",
        "✓ setup · 1 changed",
    ]
    assert err.getvalue().splitlines() == ["error: stopped", "next: run it"]


def test_an_empty_no_colour_does_not_turn_colour_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#495: only a non-empty NO_COLOR turns colour off."""

    _out, err = put_on_terminals(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "")

    write_error("stopped")

    assert err.getvalue().startswith("\x1b[")
