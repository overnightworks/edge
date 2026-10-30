"""The rows of a finished step: what a typed result prints as, with no step run."""

import pytest

from hal.output import Glyph
from hal.stack import realm as realm_lines
from hal.stack.realm.reconcile import Leftovers, OnlyInKeycloak, RealmApplied, StepResult
from hal.stack.result_rows import result_rows

SNAPSHOT = "37c2a56056d17297895e58c1ff98493754064280352422f136596266498fe056"
REMOVAL = realm_lines.Change("hallucinai", realm_lines.Kind.REMOVAL, "group", "/console", members=0)
ADDITION = realm_lines.Change("hallucinai", realm_lines.Kind.ADDITION, "group", "/ops", members=2)
UNREAD = ("workflows", "defaultGroups", "userProfile")


def applied(*changes: realm_lines.Change, leftovers: Leftovers | None, seconds: float = 11.4):
    return StepResult(RealmApplied(SNAPSHOT, seconds, changes, leftovers))


def test_the_result_is_one_row_with_the_short_snapshot_the_count_and_the_time() -> None:
    leftovers = Leftovers((), (), True, UNREAD)

    (row,) = result_rows(applied(ADDITION, REMOVAL, leftovers=leftovers, seconds=71.9))

    assert (row.glyph, row.subject, str(row.event)) == (Glyph.DONE, "keycloak", "realm applied")
    assert row.fact == "2 changes · snapshot 37c2a560 proven · 1m11s"


def test_a_removal_is_reset_to_the_file_with_the_short_snapshot_under_the_row() -> None:
    (row,) = result_rows(applied(ADDITION, REMOVAL, leftovers=None))

    assert row.details == (
        "hallucinai: addition group /ops (2 members)",
        "hallucinai: removal group /console (0 members): reset to the file, "
        "pre-apply snapshot 37c2a560",
    )


@pytest.mark.parametrize(
    ("leftovers", "details"),
    [
        pytest.param(None, (), id="record not written: nothing listed"),
        pytest.param(
            Leftovers((), (), True, UNREAD),
            ("only in Keycloak: nothing · 3 kinds not read",),
            id="nothing",
        ),
        pytest.param(
            Leftovers(
                (OnlyInKeycloak("hallucinai", "group", "/console"),), ("elsewhere",), True, ("x",)
            ),
            (
                "only in Keycloak: hallucinai group /console",
                "only in Keycloak: elsewhere not listed, it was not observed",
                "only in Keycloak: 1 found · 1 kind not read",
            ),
            id="found and absent",
        ),
        pytest.param(
            Leftovers((), ("elsewhere",), True, UNREAD),
            (
                "only in Keycloak: elsewhere not listed, it was not observed",
                "only in Keycloak: 0 found · 3 kinds not read",
            ),
            id="absent only",
        ),
        pytest.param(
            Leftovers((), (), False, UNREAD),
            ("only in Keycloak: not listed, the realm files could not be read",),
            id="files unreadable",
        ),
        pytest.param(
            Leftovers((OnlyInKeycloak("hallucinai", "group", "/a\x1b[31m\nb"),), (), True, ()),
            (
                "only in Keycloak: hallucinai group /a\\x1b[31m\\nb",
                "only in Keycloak: 1 found · 0 kinds not read",
            ),
            id="escaped name",
        ),
    ],
)
def test_what_is_only_in_keycloak_is_one_detail_each_then_one_summary(leftovers, details) -> None:
    (row,) = result_rows(applied(leftovers=leftovers))

    assert row.details == details


def test_no_changes_is_said_as_such() -> None:
    (row,) = result_rows(applied(leftovers=None))

    assert row.fact.startswith("no changes · ")


def test_a_skipped_realm_is_one_needs_you_row_with_the_reason_and_no_detail() -> None:
    (row,) = result_rows(StepResult(skipped="the realm observer failed"))

    assert (row.glyph, row.subject, str(row.event)) == (
        Glyph.NEEDS_YOU,
        "keycloak",
        "realm skipped",
    )
    assert (row.fact, row.details) == ("the realm observer failed", ())


@pytest.mark.parametrize("result", [StepResult(), StepResult(failure="realm: broke")])
def test_a_step_that_applied_nothing_has_no_row(result: StepResult) -> None:
    assert result_rows(result) == ()
