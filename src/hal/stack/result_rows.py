"""The finished run as rows: each step's typed result is mapped to the rows `hal.output` prints.
Nothing here parses a string or prints; `plan_rows` is the planned run's twin."""

from enum import StrEnum

from hal.output import Glyph, Noun, Row, counted, format_duration, printable
from hal.stack import realm as realm_lines
from hal.stack.plan_rows import ChangeNoun, PlanEvent
from hal.stack.realm import Kind
from hal.stack.realm.reconcile import REALM_UNIT, Leftovers, RealmApplied, StepResult

# restic's short ID, which `hal restore --snapshot` takes as a prefix (`hal.backup.runner`'s
# MIN_SNAPSHOT_PREFIX); `hal.stack` may not import `hal.backup`, so the length is repeated.
SNAPSHOT_SHORT_ID = 8
_SEPARATOR = " · "
_ONLY_IN_KEYCLOAK = "only in Keycloak: "


class ResultEvent(StrEnum):
    REALM_APPLIED = "realm applied"


class UnreadNoun(Noun):
    KIND = "kind not read"


def result_rows(result: StepResult) -> tuple[Row, ...]:
    """The rows of one step's result: none when the step applied nothing."""
    if result.skipped is not None:
        return (Row(Glyph.NEEDS_YOU, REALM_UNIT, PlanEvent.REALM_SKIPPED, result.skipped),)
    return (_realm_row(result.applied),) if result.applied else ()


def _realm_row(applied: RealmApplied) -> Row:
    short = applied.snapshot[:SNAPSHOT_SHORT_ID]
    changes = counted(len(applied.changes), ChangeNoun.CHANGE) if applied.changes else "no changes"
    facts = (changes, f"snapshot {short} proven", format_duration(applied.seconds))
    details = (
        *(_change_detail(change, short) for change in applied.changes),
        *(_leftovers_details(applied.leftovers) if applied.leftovers else ()),
    )
    return Row(Glyph.DONE, REALM_UNIT, ResultEvent.REALM_APPLIED, _SEPARATOR.join(facts),
               details=details)  # fmt: skip


def _change_detail(change: realm_lines.Change, short_snapshot: str) -> str:
    """A removal is config-cli resetting a console change to the file."""
    shown = realm_lines.line(change)
    if change.kind is Kind.REMOVAL:
        shown += f": reset to the file, pre-apply snapshot {short_snapshot}"
    return shown


def _leftovers_details(leftovers: Leftovers) -> tuple[str, ...]:
    if not leftovers.files_read:
        return (f"{_ONLY_IN_KEYCLOAK}not listed, the realm files could not be read",)
    found = tuple(
        f"{_ONLY_IN_KEYCLOAK}{printable(item.realm)} {item.kind} {printable(item.identity)}"
        for item in leftovers.found
    )
    absent = tuple(
        f"{_ONLY_IN_KEYCLOAK}{printable(realm)} not listed, it was not observed"
        for realm in leftovers.absent
    )
    seen = f"{len(leftovers.found)} found" if leftovers.found or leftovers.absent else "nothing"
    unread = counted(len(leftovers.unread), UnreadNoun.KIND)
    summary = f"{_ONLY_IN_KEYCLOAK}{seen}{_SEPARATOR}{unread}"
    return (*found, *absent, summary)
