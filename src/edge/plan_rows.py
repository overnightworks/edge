"""The planned run as rows: each part of a `Plan` is mapped from its typed fields to the rows
`hal_output` prints, in the order hal works. Nothing here parses a string or prints."""

from enum import StrEnum

from edge import switches
from edge.apps import AppKind, AppPlan, AppsPlan, OffPlan, SkippedNote, StaysStoppedNote
from edge.plan import Plan
from hal_output import Glyph, Group, Row

_SEPARATOR = " · "


class PlanEvent(StrEnum):
    SWITCH_ON = "switch on"
    START_AGAIN = "start again"
    UPDATE = "update"
    SWITCH_OFF = "switch off"
    STAYS_STOPPED = "stays stopped"
    TAG_SKIPPED = "tag skipped"


class PlanGroup(StrEnum):
    APPS = "apps"


def plan_groups(plan: Plan) -> tuple[Group, ...]:
    return (Group(PlanGroup.APPS, _app_rows(plan.app_plans)),)


def _app_rows(apps: AppsPlan) -> tuple[Row, ...]:
    return (
        *(_kept_row(note) for note in apps.kept),
        *(_off_row(off) for off in apps.offs),
        *(_app_row(plan) for plan in apps.plans),
    )


def _kept_row(note: StaysStoppedNote | SkippedNote) -> Row:
    if isinstance(note, SkippedNote):
        fact = f"{note.skipped.tag}{_SEPARATOR}{note.skipped.reason}"
        return Row(Glyph.UNCHANGED, note.key, PlanEvent.TAG_SKIPPED, fact)
    fact = (
        "barrier stands"
        if note.since is None
        else f"switched off since {switches.describe_since(note.since)}"
    )
    return Row(Glyph.UNCHANGED, note.key, PlanEvent.STAYS_STOPPED, fact)


def _off_row(off: OffPlan) -> Row:
    fact = f"volumes kept{_SEPARATOR}~/srv/{off.app.key} kept"
    return Row(Glyph.REMOVE, off.app.key, PlanEvent.SWITCH_OFF, fact)


def _app_row(plan: AppPlan) -> Row:
    key, tag = plan.app.key, plan.release.tag
    if plan.kind is AppKind.ON:
        return Row(Glyph.ADD, key, PlanEvent.SWITCH_ON, f"{tag}{_SEPARATOR}into ~/srv/{key}")
    if plan.kind is AppKind.START_AGAIN:
        return Row(Glyph.ADD, key, PlanEvent.START_AGAIN, f"{tag}{_SEPARATOR}recorded")
    fact = f"{plan.recorded_tag} → {tag}"
    if plan.unfinished:
        fact += f"{_SEPARATOR}update to {plan.unfinished} did not finish"
    return Row(Glyph.CHANGE, key, PlanEvent.UPDATE, fact)
