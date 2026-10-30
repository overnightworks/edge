"""The rows the Cloudflare publish prints for its plan: current, a warning, or not planned.

Every text is redacted of the token values here; `hal.output` escapes it when it writes the row.
"""

from dataclasses import replace
from enum import StrEnum

from hal.output import Glyph, Row
from hal.stack.external import secrets, tofu

SUBJECT = "cloudflare"


class PublishEvent(StrEnum):
    CURRENT = "current"
    WARNING = "warning"
    NOT_PLANNED = "not planned"


def current_row() -> Row:
    return Row(Glyph.DONE, SUBJECT, PublishEvent.CURRENT, "nothing to publish")


def warning_rows(stream: tofu.PlanStream, token_values: dict[str, str]) -> tuple[Row, ...]:
    return tuple(
        Row(
            Glyph.NEEDS_YOU,
            SUBJECT,
            PublishEvent.WARNING,
            secrets.redact(each.summary, token_values),
            details=_lines(each.detail, token_values),
        )
        for each in stream.diagnostics
        if each.severity is tofu.Severity.WARNING
    )


def unparsed_rows(stream: tofu.PlanStream) -> tuple[Row, ...]:
    """One warning row for the lines tofu wrote outside its stream, which a plan that went on must
    not swallow; none when there are none."""
    if not stream.unparsed:
        return ()
    return (
        Row(
            Glyph.NEEDS_YOU,
            SUBJECT,
            PublishEvent.WARNING,
            "tofu printed text outside its stream",
            details=stream.unparsed,
        ),
    )


def failed_plan_rows(stream: tofu.PlanStream, token_values: dict[str, str]) -> tuple[Row, ...]:
    """A row for each error; the lines tofu wrote outside its stream stand under the last one,
    or under a row of their own when the stream names no error."""
    rows = [
        _not_planned(each.summary, _lines(each.detail, token_values), token_values)
        for each in stream.diagnostics
        if each.severity is tofu.Severity.ERROR
    ]
    unparsed = stream.unparsed
    if not rows:
        return (_not_planned("tofu ended without naming a reason", unparsed, token_values),)
    rows[-1] = replace(rows[-1], details=(*rows[-1].details, *unparsed))
    return tuple(rows)


def failed_show_row(output: str, token_values: dict[str, str]) -> Row:
    return _not_planned("tofu could not show the plan", _lines(output, token_values), token_values)


def malformed_stream_row() -> Row:
    return _not_planned("tofu's plan stream has a diagnostic without severity or summary", (), {})


def _not_planned(summary: str, details: tuple[str, ...], token_values: dict[str, str]) -> Row:
    return Row(
        Glyph.FAILED,
        SUBJECT,
        PublishEvent.NOT_PLANNED,
        secrets.redact(summary, token_values),
        details=details,
    )


def _lines(text: str, token_values: dict[str, str]) -> tuple[str, ...]:
    redacted = secrets.redact(text, token_values)
    return tuple(line.rstrip() for line in redacted.splitlines() if line.strip())
