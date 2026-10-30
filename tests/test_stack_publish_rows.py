"""The Cloudflare plan as hal rows: `tofu plan -json` read into diagnostics, and what each says.

The three streams below are lines the pinned OpenTofu 1.13.1 wrote for a throwaway configuration
(`terraform_data` and a `check` block; no provider, no token, no state), so the parser meets the
real shape, including the snippet and difference objects whose values it must not read.
"""

import json
import signal
from collections.abc import Callable
from pathlib import Path

import pytest
from test_stack_external import (  # noqa: F401 - pytest finds the fixtures by name here
    PLANS_DIRECTORY,
    TOKEN_FILES,
    WRITE_TOKEN_FILES,
    FakeTofu,
    fake_tofu,
    home,
    live_checkout,
    publish,
    said,
    terminal,
)

from hal.output import Glyph, Row
from hal.stack.external import publish_rows, tofu
from hal.stack.external.publish_rows import PublishEvent

SUBJECT = "cloudflare"
TOKEN = "write-token-value-9f3a"

REAL_EMPTY_PLAN_WITH_WARNING = tuple(
    json.dumps(each)
    for each in (
        {
            "@level": "info",
            "@message": "OpenTofu 1.13.1",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.458594+01:00",
            "tofu": "1.13.1",
            "type": "version",
            "ui": "1.2",
        },
        {
            "@level": "info",
            "@message": "Plan: 0 to add, 0 to change, 0 to destroy.",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.463986+01:00",
            "changes": {
                "add": 0,
                "change": 0,
                "import": 0,
                "remove": 0,
                "forget": 0,
                "operation": "plan",
            },
            "type": "change_summary",
        },
        {
            "@level": "warn",
            "@message": "Warning: Check block assertion failed",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.464154+01:00",
            "diagnostic": {
                "severity": "warning",
                "summary": "Check block assertion failed",
                "detail": "input differs",
                "range": {
                    "filename": "main.tf",
                    "start": {"line": 6, "column": 21, "byte": 76},
                    "end": {"line": 6, "column": 37, "byte": 92},
                },
                "snippet": {
                    "context": 'check "c"',
                    "code": '    condition     = var.x == "other"',
                    "start_line": 6,
                    "highlight_start_offset": 20,
                    "highlight_end_offset": 36,
                    "values": [{"traversal": "var.x", "statement": 'is "v"'}],
                },
                "difference": {
                    "before": "v",
                    "after": "other",
                    "after_unknown": False,
                    "before_sensitive": False,
                    "after_sensitive": False,
                },
            },
            "type": "diagnostic",
        },
    )
)

REAL_PLAN_WITH_CHANGES = tuple(
    json.dumps(each)
    for each in (
        {
            "@level": "info",
            "@message": "OpenTofu 1.13.1",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.577858+01:00",
            "tofu": "1.13.1",
            "type": "version",
            "ui": "1.2",
        },
        {
            "@level": "info",
            "@message": "terraform_data.a: Plan to create",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.583499+01:00",
            "change": {
                "resource": {
                    "addr": "terraform_data.a",
                    "module": "",
                    "resource": "terraform_data.a",
                    "implied_provider": "terraform",
                    "resource_type": "terraform_data",
                    "resource_name": "a",
                    "resource_key": None,
                },
                "action": "create",
            },
            "type": "planned_change",
        },
        {
            "@level": "info",
            "@message": "Plan: 1 to add, 0 to change, 0 to destroy.",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.583677+01:00",
            "changes": {
                "add": 1,
                "change": 0,
                "import": 0,
                "remove": 0,
                "forget": 0,
                "operation": "plan",
            },
            "type": "change_summary",
        },
    )
)

REAL_FAILED_PLAN = tuple(
    json.dumps(each)
    for each in (
        {
            "@level": "info",
            "@message": "OpenTofu 1.13.1",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.513170+01:00",
            "tofu": "1.13.1",
            "type": "version",
            "ui": "1.2",
        },
        {
            "@level": "error",
            "@message": "Error: Invalid function argument",
            "@module": "tofu.ui",
            "@timestamp": "2026-10-30T13:02:56.516142+01:00",
            "diagnostic": {
                "severity": "error",
                "summary": "Invalid function argument",
                "detail": (
                    'Invalid value for "path" parameter: no file exists at '
                    '"/nonexistent/zzz"; this function works only with files that are '
                    "distributed as part of the configuration source code, so if this file "
                    "will be created by a resource in this configuration you must instead "
                    "obtain this result from an attribute of that resource."
                ),
                "range": {
                    "filename": "main.tf",
                    "start": {"line": 1, "column": 47, "byte": 46},
                    "end": {"line": 1, "column": 63, "byte": 62},
                },
                "snippet": {
                    "context": 'resource "terraform_data" "a"',
                    "code": 'resource "terraform_data" "a" { input = file("/nonexistent/zzz") }',
                    "start_line": 1,
                    "highlight_start_offset": 46,
                    "highlight_end_offset": 62,
                    "values": [],
                    "function_call": {
                        "called_as": "file",
                        "signature": {
                            "name": "file",
                            "params": [{"name": "path", "type": "string"}],
                            "return_type": "string",
                        },
                    },
                },
            },
            "type": "diagnostic",
        },
    )
)


def stream_lines(*objects: object) -> str:
    return "\n".join(json.dumps(each) for each in objects)


def diagnostic(severity: object = "error", summary: object = "boom", detail: object = "") -> dict:
    return {
        "type": "diagnostic",
        "diagnostic": {"severity": severity, "summary": summary, "detail": detail},
    }


def test_the_warning_of_a_real_empty_plan_is_read_with_its_summary_and_detail():
    stream = tofu.read_plan_stream("\n".join(REAL_EMPTY_PLAN_WITH_WARNING), {})
    assert stream.diagnostics == (
        tofu.Diagnostic(tofu.Severity.WARNING, "Check block assertion failed", "input differs"),
    )
    assert stream.unparsed == ()


def test_the_error_of_a_real_failed_plan_is_read_and_a_plan_with_changes_has_no_diagnostic():
    failed = tofu.read_plan_stream("\n".join(REAL_FAILED_PLAN), {})
    (error,) = failed.diagnostics
    assert (error.severity, error.summary) == (tofu.Severity.ERROR, "Invalid function argument")
    assert error.detail.startswith('Invalid value for "path" parameter')
    assert tofu.read_plan_stream("\n".join(REAL_PLAN_WITH_CHANGES), {}).diagnostics == ()


def test_a_line_that_is_not_a_json_object_is_kept_apart_and_other_types_are_ignored():
    output = "\n".join(
        ["panic: tofu crashed", "42", "", json.dumps({"type": "planned_change"}), "[1]"]
    )
    stream = tofu.read_plan_stream(output, {})
    assert stream.diagnostics == ()
    assert stream.unparsed == ("panic: tofu crashed", "42", "[1]")


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(diagnostic(summary=None), id="no-summary"),
        pytest.param(diagnostic(summary=""), id="empty-summary"),
        pytest.param(diagnostic(summary=7), id="summary-not-text"),
        pytest.param(diagnostic(severity=None), id="no-severity"),
        pytest.param(diagnostic(severity="info"), id="unknown-severity"),
        pytest.param({"type": "diagnostic"}, id="no-diagnostic-object"),
        pytest.param({"type": "diagnostic", "diagnostic": "text"}, id="diagnostic-not-an-object"),
        pytest.param(diagnostic(detail=7), id="detail-not-text"),
    ],
)
def test_a_diagnostic_without_severity_or_summary_is_refused_not_guessed(broken):
    lines = stream_lines(diagnostic(), broken)
    with pytest.raises(tofu.MalformedPlanStream):
        tofu.read_plan_stream(lines, {})


def test_a_missing_detail_reads_as_empty():
    stream = tofu.read_plan_stream(
        stream_lines({"type": "diagnostic", "diagnostic": {"severity": "warning", "summary": "w"}}),
        {},
    )
    assert stream.diagnostics == (tofu.Diagnostic(tofu.Severity.WARNING, "w", ""),)


def test_the_empty_plan_is_one_current_row():
    assert publish_rows.current_row() == Row(
        Glyph.DONE, SUBJECT, PublishEvent.CURRENT, "nothing to publish"
    )


def test_a_warning_is_a_row_with_the_detail_lines_under_it():
    stream = tofu.read_plan_stream(
        stream_lines(diagnostic("warning", "careful", "one\n\ntwo\n")), {}
    )
    assert publish_rows.warning_rows(stream, {}) == (
        Row(Glyph.NEEDS_YOU, SUBJECT, PublishEvent.WARNING, "careful", details=("one", "two")),
    )


def test_each_error_of_a_failed_plan_is_a_row_and_a_warning_is_not():
    stream = tofu.read_plan_stream(
        stream_lines(
            diagnostic("error", "first", "why"),
            diagnostic("warning", "side note"),
            diagnostic("error", "second"),
        ),
        {},
    )
    assert publish_rows.failed_plan_rows(stream, {}) == (
        Row(Glyph.FAILED, SUBJECT, PublishEvent.NOT_PLANNED, "first", details=("why",)),
        Row(Glyph.FAILED, SUBJECT, PublishEvent.NOT_PLANNED, "second"),
    )


def test_a_non_json_line_of_a_failed_plan_is_a_detail_under_its_last_error():
    stream = tofu.read_plan_stream(stream_lines(diagnostic("error", "first", "why")) + "\nSEGV", {})
    (row,) = publish_rows.failed_plan_rows(stream, {})
    assert row.details == ("why", "SEGV")


def test_a_failed_plan_without_an_error_still_prints_one_row_with_the_crash_text():
    stream = tofu.read_plan_stream("panic: tofu crashed", {})
    assert publish_rows.failed_plan_rows(stream, {}) == (
        Row(
            Glyph.FAILED,
            SUBJECT,
            PublishEvent.NOT_PLANNED,
            "tofu ended without naming a reason",
            details=("panic: tofu crashed",),
        ),
    )


def test_a_token_in_any_text_of_a_row_is_redacted():
    tokens = {"write": TOKEN}
    failed = tofu.read_plan_stream(
        stream_lines(diagnostic("error", f"bad {TOKEN}", f"line {TOKEN}")) + f"\ncrash {TOKEN}",
        tokens,
    )
    warned = tofu.read_plan_stream(stream_lines(diagnostic("warning", TOKEN, TOKEN)), tokens)
    shown = [
        *publish_rows.failed_plan_rows(failed, tokens),
        *publish_rows.warning_rows(warned, tokens),
        publish_rows.failed_show_row(f"out {TOKEN}", tokens),
    ]
    assert TOKEN not in repr(shown)
    assert "***" in repr(shown)


def test_a_failed_show_is_one_row_with_the_redacted_text_of_tofu_under_it():
    assert publish_rows.failed_show_row("no such plan\n\nsorry", {}) == Row(
        Glyph.FAILED,
        SUBJECT,
        PublishEvent.NOT_PLANNED,
        "tofu could not show the plan",
        details=("no such plan", "sorry"),
    )


def test_a_broken_stream_is_one_row_that_names_no_tofu_text():
    row = publish_rows.malformed_stream_row()
    assert (row.glyph, row.subject, row.event) == (Glyph.FAILED, SUBJECT, PublishEvent.NOT_PLANNED)
    assert row.fact == "tofu's plan stream has a diagnostic without severity or summary"


# The publish cells below drive `apply_command` and `hal apply` on a pty against the fake tofu of
# test_stack_external, whose fixtures they request by name.
@pytest.fixture
def fake(fake_tofu: FakeTofu) -> FakeTofu:  # noqa: F811
    return fake_tofu


@pytest.fixture
def plans(home: Path) -> Path:  # noqa: F811
    return home / PLANS_DIRECTORY


@pytest.fixture
def pty_apply(terminal: Callable) -> Callable:  # noqa: F811
    return terminal


def configure_plan(fake: FakeTofu, *, plan_lines=(), show_lines=(), **exit_codes) -> None:
    """The fake tofu says `plan_lines` for `plan` and `show_lines` for `show`; it exits as told."""
    fake.configure(output_lines={"plan": list(plan_lines), "show": list(show_lines)}, **exit_codes)


def diagnostic_line(severity: str, summary: str, detail: str = "") -> str:
    return json.dumps(diagnostic(severity, summary, detail))


WRITE_TOKEN = WRITE_TOKEN_FILES[".secrets/cloudflare/fleet-write"]
PASSPHRASE = TOKEN_FILES[".secrets/opentofu/state-passphrase"]
TOFU_TEXT = ("Acquiring state lock", "Reading...", "Refreshing state...", "No changes.")


@pytest.fixture
def publish_answering(monkeypatch, publish: Callable[[], int]) -> Callable[[str], int]:  # noqa: F811
    """`publish` with `answer` typed at the prompt; a cell that expects no prompt gives none."""

    def with_answer(answer: str | None = None) -> int:
        def asked(prompt: str) -> str:
            assert answer is not None, f"unexpected prompt {prompt!r}"
            return answer

        monkeypatch.setattr("builtins.input", asked)
        return publish()

    return with_answer


def plan_file_of(fake: FakeTofu) -> Path:
    out_argument = next(a for a in fake.call("plan")["argv"] if a.startswith("-out="))
    return Path(out_argument.removeprefix("-out="))


def commands_of(fake: FakeTofu) -> list[str]:
    return [call["argv"][0] for call in fake.calls()]


def test_an_empty_plan_prints_one_current_row_and_no_tofu_text(fake, capsys, publish_answering):
    """The stream's own lines are not shown."""
    configure_plan(fake, plan=0, plan_lines=REAL_EMPTY_PLAN_WITH_WARNING[:2])
    assert publish_answering() == 0
    printed = capsys.readouterr()
    assert said(printed.out) == "✓ cloudflare current nothing to publish"
    assert not any(text in printed.out for text in TOFU_TEXT)
    assert printed.err == ""
    assert "apply" not in commands_of(fake)


def test_an_empty_plan_with_a_warning_prints_the_warning_row_then_the_current_row(
    fake, capsys, publish_answering
):
    """The warning's reason is tofu's summary, its explanation dim under it."""
    configure_plan(fake, plan=0, plan_lines=REAL_EMPTY_PLAN_WITH_WARNING)
    assert publish_answering() == 0
    lines = [said(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "! cloudflare warning Check block assertion failed",
        "input differs",
        "✓ cloudflare current nothing to publish",
    ]


def test_a_plan_with_changes_shows_tofus_rendering_of_the_written_plan_file_before_the_yes(
    fake,
    capsys,
    monkeypatch,
    publish,  # noqa: F811
):
    """`show` reads the file `plan` wrote, `apply` gets that file, after the diff."""
    configure_plan(
        fake,
        plan=2,
        plan_lines=REAL_PLAN_WITH_CHANGES,
        show_lines=["  # terraform_data.a will be created"],
    )
    seen_before_asking = []
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: seen_before_asking.append(capsys.readouterr().out) or "yes",
    )
    assert publish() == 0
    (before_the_prompt,) = seen_before_asking
    assert before_the_prompt == "  # terraform_data.a will be created\n"
    assert commands_of(fake) == ["version", "init", "plan", "show", "apply"]
    plan_file = plan_file_of(fake)
    assert fake.call("show")["argv"] == ["show", "-no-color", str(plan_file)]
    assert fake.call("show")["plan_file_existed"]
    assert fake.call("apply")["argv"] == ["apply", str(plan_file)]


def test_a_warning_of_a_plan_with_changes_stands_before_the_diff(fake, capsys, publish_answering):
    """A warning of the plan stands before the diff."""
    configure_plan(
        fake,
        plan=2,
        plan_lines=[*REAL_EMPTY_PLAN_WITH_WARNING, *REAL_PLAN_WITH_CHANGES],
        show_lines=["the diff"],
    )
    assert publish_answering("no") == 1
    out = capsys.readouterr().out
    assert out.index("Check block assertion failed") < out.index("the diff")


def test_a_failed_plan_prints_a_row_for_each_error_and_asks_for_nothing(
    plans, fake, capsys, publish_answering
):
    """No `tofu plan failed` line, no show, no apply, the plan file gone."""
    configure_plan(
        fake,
        plan=1,
        plan_lines=[
            diagnostic_line("error", "first reason", "first explanation"),
            diagnostic_line("error", "second reason"),
        ],
    )
    assert publish_answering() == 2
    printed = capsys.readouterr()
    assert [said(line) for line in printed.out.splitlines()] == [
        "✗ cloudflare not planned first reason",
        "first explanation",
        "✗ cloudflare not planned second reason",
    ]
    assert "tofu plan failed" not in printed.out + printed.err
    assert commands_of(fake) == ["version", "init", "plan"]
    assert list(plans.glob("*.tfplan")) == []


def test_a_show_that_fails_ends_like_a_failed_plan(plans, fake, capsys, publish_answering):
    """Nothing is applied, the plan file is deleted, the exit is the failed plan's."""
    configure_plan(fake, plan=2, show=1, show_lines=["plan file is unreadable"])
    assert publish_answering() == 2
    lines = [said(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "✗ cloudflare not planned tofu could not show the plan",
        "plan file is unreadable",
    ]
    assert "apply" not in commands_of(fake)
    assert list(plans.glob("*.tfplan")) == []


def test_no_token_or_passphrase_reaches_any_line_of_the_plan(fake, capsys, publish_answering):
    """Summary, detail, a crash line and the show text are all redacted."""
    secrets_in_text = f"{WRITE_TOKEN} and {PASSPHRASE}"
    configure_plan(
        fake,
        plan=2,
        plan_lines=[
            diagnostic_line("warning", secrets_in_text, secrets_in_text),
            f"crash {secrets_in_text}",
        ],
        show_lines=[f"diff {secrets_in_text}"],
    )
    assert publish_answering("no") == 1
    printed = capsys.readouterr()
    for secret in (WRITE_TOKEN, PASSPHRASE):
        assert secret not in printed.out + printed.err
    assert "*** and ***" in printed.out


def test_a_summary_with_a_control_character_stays_one_escaped_line(fake, capsys, publish_answering):
    """Tofu's text is escaped by `hal.output`, whatever it carries."""
    configure_plan(
        fake,
        plan=0,
        plan_lines=[diagnostic_line("warning", "red\x1b[31m and\nsplit", "detail\x1b[0m")],
    )
    assert publish_answering() == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "red\\x1b[31m and\\nsplit" in out
    assert "detail\\x1b[0m" in out


def test_a_line_that_is_no_json_stands_under_the_row_of_a_failed_plan(
    fake, capsys, publish_answering
):
    """A crash is redacted and escaped like any tofu text."""
    configure_plan(fake, plan=1, plan_lines=[f"panic \x1b[1m {WRITE_TOKEN}"])
    assert publish_answering() == 2
    lines = [said(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "✗ cloudflare not planned tofu ended without naming a reason",
        "panic \\x1b[1m ***",
    ]


def test_a_line_outside_the_stream_of_an_empty_plan_is_a_warning_before_the_current_row(
    fake, capsys, publish_answering
):
    """A truncated line is shown, the exit stays 0."""
    configure_plan(fake, plan=0, plan_lines=["panic: truncated {"])
    assert publish_answering() == 0
    lines = [said(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        "! cloudflare warning tofu printed text outside its stream",
        "panic: truncated {",
        "✓ cloudflare current nothing to publish",
    ]
    assert "apply" not in commands_of(fake)


def test_a_line_outside_the_stream_of_a_plan_with_changes_is_a_warning_before_the_diff(
    fake,
    capsys,
    monkeypatch,
    publish,  # noqa: F811
):
    """The warning stands before the diff and the prompt."""
    configure_plan(
        fake,
        plan=2,
        plan_lines=["panic: truncated {", *REAL_PLAN_WITH_CHANGES],
        show_lines=["the diff"],
    )
    seen_before_asking = []
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: seen_before_asking.append(capsys.readouterr().out) or "yes",
    )
    assert publish() == 0
    (before_the_prompt,) = seen_before_asking
    assert [said(line) for line in before_the_prompt.splitlines()] == [
        "! cloudflare warning tofu printed text outside its stream",
        "panic: truncated {",
        "the diff",
    ]
    assert commands_of(fake) == ["version", "init", "plan", "show", "apply"]


MULTILINE_PASSPHRASE = "alpha\nbeta"


@pytest.fixture
def passphrase_file(home: Path) -> Path:  # noqa: F811
    return home / ".secrets/opentofu/state-passphrase"


@pytest.fixture
def multiline_passphrase(passphrase_file: Path) -> None:
    passphrase_file.write_text(MULTILINE_PASSPHRASE + "\n")


def test_a_passphrase_with_a_newline_in_crash_text_is_not_printed(
    fake, capsys, publish_answering, multiline_passphrase
):
    """The secret is redacted whole before the text is split into lines."""
    configure_plan(fake, plan=1, plan_lines=[f"panic: {MULTILINE_PASSPHRASE} leaked"])
    assert publish_answering() == 2
    printed = capsys.readouterr()
    for part in ("alpha", "beta"):
        assert part not in printed.out + printed.err


def test_a_passphrase_with_a_newline_in_a_diagnostic_detail_is_not_printed(
    fake, capsys, publish_answering, multiline_passphrase
):
    """A value JSON escapes appears only decoded, and is redacted decoded."""
    configure_plan(
        fake,
        plan=0,
        plan_lines=[diagnostic_line("warning", "careful", f"echo {MULTILINE_PASSPHRASE} end")],
    )
    assert publish_answering() == 0
    printed = capsys.readouterr()
    for part in ("alpha", "beta"):
        assert part not in printed.out + printed.err


JSON_LINE_PASSPHRASE = "alpha\n{}\nbeta"


@pytest.mark.parametrize(
    ("plan_exit", "plan_text"),
    [
        pytest.param(1, f"panic: {JSON_LINE_PASSPHRASE} leaked", id="failed-plan"),
        pytest.param(0, f"truncated {JSON_LINE_PASSPHRASE} text", id="empty-plan-warning"),
    ],
)
def test_a_passphrase_with_a_json_looking_line_is_not_printed_wherever_its_lines_parse(
    fake, capsys, publish_answering, passphrase_file, plan_exit, plan_text
):
    """The `{}` line of the secret parses as JSON; the text is masked whole."""
    passphrase_file.write_text(JSON_LINE_PASSPHRASE + "\n")
    configure_plan(fake, plan=plan_exit, plan_lines=[plan_text])
    publish_answering()
    printed = capsys.readouterr()
    for leaked in ("alpha", "beta", "{}"):
        assert leaked not in printed.out + printed.err


@pytest.mark.parametrize(
    ("plan_exit", "plan_lines"),
    [
        pytest.param(0, REAL_EMPTY_PLAN_WITH_WARNING, id="empty-plan"),
        pytest.param(1, REAL_FAILED_PLAN, id="failed-plan"),
    ],
)
def test_masking_a_secret_never_turns_a_stream_object_into_printed_text(
    fake, capsys, publish_answering, passphrase_file, plan_exit, plan_lines
):
    """A passphrase `false` is in the stream's booleans."""
    passphrase_file.write_text("false\n")
    configure_plan(fake, plan=plan_exit, plan_lines=plan_lines)
    publish_answering()
    printed = capsys.readouterr()
    for field in ("snippet", "difference", "after_unknown", '"type"'):
        assert field not in printed.out + printed.err


def test_a_secret_spanning_a_stream_object_and_crash_lines_is_masked_in_each_part(
    fake, capsys, publish_answering, passphrase_file
):
    """The object line stays a diagnostic, the crash lines around it mask."""
    object_line = diagnostic_line("warning", "careful")
    secret = f"alpha\n{object_line}\nbeta"
    passphrase_file.write_text(secret + "\n")
    configure_plan(fake, plan=0, plan_lines=["alpha", object_line, "beta"])
    assert publish_answering() == 0
    printed = capsys.readouterr()
    for part in ("alpha", "beta"):
        assert part not in printed.out + printed.err
    assert "careful" in printed.out


@pytest.mark.parametrize("plan_exit", [0, 2])
def test_a_diagnostic_without_a_summary_is_refused_before_the_yes(
    plans, fake, capsys, publish_answering, plan_exit
):
    """Nothing is shown as the plan, nothing is applied, and the exit is 2."""
    configure_plan(fake, plan=plan_exit, plan_lines=[json.dumps(diagnostic(summary=None))])
    assert publish_answering() == 2
    out = capsys.readouterr().out
    assert said(out) == (
        "✗ cloudflare not planned tofu's plan stream has a diagnostic without severity or summary"
    )
    assert commands_of(fake) == ["version", "init", "plan"]
    assert list(plans.glob("*.tfplan")) == []


@pytest.mark.parametrize("environment", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_the_rows_are_the_same_words_without_colour(fake, pty_apply, environment):
    """No ANSI where the environment asks for none, from tofu or from hal."""
    configure_plan(fake, plan=0, plan_lines=[diagnostic_line("warning", "careful", "why")])
    apply = pty_apply(environment=environment)
    assert apply.finish() == 0
    assert "\x1b" not in apply.output
    assert said(apply.output).startswith("! cloudflare warning careful why ✓ cloudflare current")


def test_an_interrupted_plan_prints_the_interrupt_line_and_none_of_the_stream(
    plans, fake, pty_apply
):
    """The stream tofu wrote while stopping is not shown."""
    configure_plan(fake, hang_in="plan", plan_lines=REAL_EMPTY_PLAN_WITH_WARNING)
    apply = pty_apply()
    with fake.watch_hang() as wait_until_hanging:
        wait_until_hanging()
        apply.signal(signal.SIGINT)
        assert apply.finish() == 1
    assert "interrupted during preparing the plan" in said(apply.output)
    assert '"type"' not in apply.output
    assert list(plans.glob("*.tfplan")) == []
