"""The terminal form every hal command prints: one row, one verdict, one error pair."""

import io
import sys
from enum import StrEnum

import pytest

from hal_output import (
    Glyph,
    Noun,
    Row,
    Tone,
    Verdict,
    counted,
    format_duration,
    write_detail,
    write_error,
    write_row,
    write_verdict,
)


class Event(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    RESTORE_PROVEN = "restore proven"
    NOT_PROVEN = "not proven"


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


def test_a_fourteen_character_event_and_a_short_one_put_their_facts_at_the_same_column(
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_row(Row(Glyph.DONE, "app", Event.RESTORE_PROVEN, "one"))
    write_row(Row(Glyph.NEEDS_YOU, "app", Event.NOT_PROVEN, "two"))

    first, second = capsys.readouterr().out.splitlines()
    assert first.index("one") == second.index("two")


def test_a_row_shows_text_from_outside_as_one_line_of_plain_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 invariant 4: the owner escapes a fact from outside before any renderer sees it."""

    write_row(Row(Glyph.NEEDS_YOU, "secrets", Event.CHANGED, "a\nb\x1b[31m"))

    assert capsys.readouterr().out == "  ! secrets         changed        a\\nb\\x1b[31m\n"


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0.4, "<1s"),
        (1.0, "1s"),
        (47.9, "47s"),
        (59.9, "59s"),
        (60, "1m00s"),
        (65, "1m05s"),
        (278, "4m38s"),
        (3599.9, "59m59s"),
        (3720, "1h02m"),
    ],
)
def test_a_duration_is_whole_seconds_rounded_down_then_minutes_then_hours(
    seconds: float, shown: str
) -> None:
    """#500: under a second `<1s`, from 60 s `XmYYs`, from one hour `XhYYm`."""

    assert format_duration(seconds) == shown


def test_the_run_time_stands_after_the_counts_and_before_the_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#500: every verdict that knows its run time ends its counts with it."""

    write_verdict(
        Verdict(Glyph.CHANGE, "setup plan", ((2, Event.CHANGED),), "run hal setup --apply", 3.2)
    )
    write_verdict(Verdict(Glyph.DONE, "setup done", ((1, Event.CHANGED),), elapsed_seconds=47.9))

    assert capsys.readouterr().out.splitlines() == [
        "~ setup plan · 2 changed · 3s · run hal setup --apply",
        "✓ setup done · 1 changed · 47s",
    ]


def test_on_a_terminal_the_run_time_is_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """#500: the run time is dim like the counts."""

    out, _err = put_on_terminals(monkeypatch)
    write_verdict(Verdict(Glyph.DONE, "setup", ((1, Event.CHANGED),), elapsed_seconds=65))

    assert out.getvalue() == (
        "\x1b[1m✓ setup\x1b[0m · \x1b[2m1 changed\x1b[0m · \x1b[2m1m05s\x1b[0m\n"
    )


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
    """#495 and #498: the glyph repeats the word in colour; subject and event keep the default."""

    out, _err = put_on_terminals(monkeypatch)
    write_row(Row(Glyph.DONE, "timers", Event.CHANGED, "three units"))
    write_row(Row(Glyph.FAILED, "system", Event.CHANGED))
    write_row(Row(Glyph.NEEDS_YOU, "secrets", Event.CHANGED))
    write_row(Row(Glyph.CHANGE, "commit hook", Event.CHANGED))
    write_row(Row(Glyph.ANNOUNCE, "sudo", Event.CHANGED))

    assert out.getvalue().splitlines() == [
        "  \x1b[32m✓\x1b[0m timers          changed        \x1b[2mthree units\x1b[0m",
        "  \x1b[31m✗\x1b[0m system          changed",
        "  \x1b[33m!\x1b[0m secrets         changed",
        "  \x1b[34m~\x1b[0m commit hook     changed",
        "  \x1b[36m\u203a\x1b[0m sudo            changed",
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
    """#495: a verdict without counts has no empty dim escape."""

    out, _err = put_on_terminals(monkeypatch)
    write_verdict(Verdict(Glyph.DONE, "setup", ((0, Event.CHANGED),), "run hal"))

    assert out.getvalue() == "\x1b[1m✓ setup\x1b[0m · run hal\n"


@pytest.mark.parametrize("failure", [AttributeError, ValueError])
def test_a_stream_without_a_working_isatty_prints_plain(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """#495: a stream without a working isatty prints plain."""

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


def test_a_fresh_line_error_starts_with_one_bare_newline_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#296 S2g3b: a hidden prompt leaves the cursor mid-line, so the pair starts on a fresh one."""

    write_error("interrupted", "run it", fresh_line=True)

    assert capsys.readouterr().err == "\nerror: interrupted\nnext: run it\n"


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


class Finding(Noun):
    ACTION = "action"
    AREA_CHECKED = "area checked"


@pytest.mark.parametrize(
    ("count", "shown"),
    [(1, "1 action"), (2, "2 actions")],
)
def test_a_rows_count_carries_the_plural_of_a_noun_event(
    capsys: pytest.CaptureFixture[str], count: int, shown: str
) -> None:
    """#493: the event enum holds the singular word; the renderer shows the count and the plural."""

    write_row(Row(Glyph.NEEDS_YOU, "backup", Finding.ACTION, "a fact", count))

    assert capsys.readouterr().out == f"  ! backup          {shown:<13}  a fact\n"


@pytest.mark.parametrize(
    ("count", "shown"),
    [(1, "1 area checked"), (3, "3 areas checked")],
)
def test_counted_puts_the_count_before_the_noun_and_pluralises_its_first_word(
    count: int, shown: str
) -> None:
    assert counted(count, Finding.AREA_CHECKED) == shown


def test_a_verdict_without_a_summary_is_its_counts_then_its_details_and_its_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#493: counts of nouns take the plural; the next step follows on its own stdout line."""

    write_verdict(
        Verdict(
            Glyph.NEEDS_YOU,
            "",
            ((2, Finding.ACTION), (0, Finding.AREA_CHECKED), (1, Event.CHANGED)),
            details=("mail sent", "report ~/x"),
            next_step="hal backup",
        )
    )
    write_verdict(Verdict(Glyph.DONE, "all well", ((5, Finding.AREA_CHECKED),)))
    write_verdict(Verdict(Glyph.DONE, "all well", ((1, Finding.AREA_CHECKED),)))

    assert capsys.readouterr().out.splitlines() == [
        "! 2 actions, 1 changed · mail sent · report ~/x",
        "next: hal backup",
        "✓ all well · 5 areas checked",
        "✓ all well · 1 area checked",
    ]


def test_a_plain_detail_line_is_indented_and_escaped(capsys: pytest.CaptureFixture[str]) -> None:
    """Text from outside stays one line under a row."""

    write_detail("why: a reason", "[ ] step\x1b[31m")

    assert capsys.readouterr().out.splitlines() == ["    why: a reason", "    [ ] step\\x1b[31m"]


@pytest.mark.parametrize(
    ("tone", "style"), [(Tone.DIM, "2"), (Tone.WARNING, "33")], ids=["dim", "warning is yellow"]
)
def test_on_a_terminal_a_toned_detail_line_is_styled_as_a_whole_and_a_plain_one_is_not(
    monkeypatch: pytest.MonkeyPatch, tone: Tone, style: str
) -> None:
    """The tone styles the indented line; plain carries no style."""

    out, _err = put_on_terminals(monkeypatch)
    write_detail("a line", tone=tone)
    write_detail("a line")

    assert out.getvalue() == f"\x1b[{style}m    a line\x1b[0m\n    a line\n"
