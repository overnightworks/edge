"""The rows `hal apply` prints for its app plan: what each kind of app entry says, from typed
fields (#296 S5-1)."""

from types import SimpleNamespace
from typing import cast

import pytest

from edge import releases
from edge.apps import (
    AppKind,
    AppPlan,
    AppRuntime,
    AppsPlan,
    OffPlan,
    SkippedNote,
    StaysStoppedNote,
)
from edge.catalog import App
from edge.plan import Plan
from edge.plan_rows import PlanEvent, PlanGroup, plan_groups
from hal_output import Glyph, Row

APP = App("hopin", True, "master")
OLD, NEW = "v2026.10.26.1", "v2026.10.27.1"


def app_plan(kind: AppKind, **overrides: object) -> AppPlan:
    fields: dict[str, object] = {
        "app": APP,
        "release": releases.parse(NEW),
        "sha": "a" * 40,
        "services": ("hopin",),
        "url": "https://github.com/org/hopin.git",
        "ingress": None,
        "kind": kind,
        "recorded_tag": None,
        "unfinished": None,
        "containers": ("hopin running (healthy)",),
    }
    return AppPlan(**{**fields, **overrides})  # type: ignore[arg-type]


def rows_of(**parts: object) -> tuple[Row, ...]:
    apps = AppsPlan(cast(AppRuntime, None), **parts)  # type: ignore[arg-type]
    [group] = plan_groups(cast(Plan, SimpleNamespace(app_plans=apps)))
    assert group.heading is PlanGroup.APPS
    return group.rows


@pytest.mark.parametrize(
    ("plans", "expected"),
    [
        pytest.param(
            (app_plan(AppKind.ON),),
            Row(Glyph.ADD, "hopin", PlanEvent.SWITCH_ON, f"{NEW} · into ~/srv/hopin"),
            id="switch on",
        ),
        pytest.param(
            (app_plan(AppKind.START_AGAIN, recorded_tag=NEW),),
            Row(Glyph.ADD, "hopin", PlanEvent.START_AGAIN, f"{NEW} · recorded"),
            id="start again",
        ),
        pytest.param(
            (app_plan(AppKind.UPDATE, recorded_tag=OLD),),
            Row(Glyph.CHANGE, "hopin", PlanEvent.UPDATE, f"{OLD} → {NEW}"),
            id="update",
        ),
        pytest.param(
            (app_plan(AppKind.UPDATE, recorded_tag=OLD, unfinished=NEW),),
            Row(
                Glyph.CHANGE,
                "hopin",
                PlanEvent.UPDATE,
                f"{OLD} → {NEW} · update to {NEW} did not finish",
            ),
            id="update after an unfinished update",
        ),
    ],
)
def test_an_app_plan_is_one_row_with_the_glyph_of_its_kind(
    plans: tuple[AppPlan, ...], expected: Row
) -> None:
    assert rows_of(plans=plans) == (expected,)


def test_an_app_switched_off_while_its_containers_exist_is_a_remove_row() -> None:
    off = OffPlan(APP, ("hopin running (healthy)",))

    assert rows_of(offs=(off,)) == (
        Row(Glyph.REMOVE, "hopin", PlanEvent.SWITCH_OFF, "volumes kept · ~/srv/hopin kept"),
    )


@pytest.mark.parametrize(
    ("since", "fact"),
    [
        ("2026-10-18T08:00:00+02:00", "switched off since 18.10. 08:00"),
        (None, "barrier stands"),
    ],
    ids=["since a switch", "with a barrier"],
)
def test_an_app_that_stays_stopped_says_since_when_or_that_its_barrier_stands(
    since: str | None, fact: str
) -> None:
    kept = (StaysStoppedNote("hopin", since),)

    assert rows_of(kept=kept) == (Row(Glyph.UNCHANGED, "hopin", PlanEvent.STAYS_STOPPED, fact),)


def test_a_skipped_tag_is_a_row_with_the_tag_and_its_reason() -> None:
    kept = (SkippedNote("hopin", releases.SkippedTag("v2027.1.5.1", "not vYYYY.MM.DD.N")),)

    assert rows_of(kept=kept) == (
        Row(Glyph.UNCHANGED, "hopin", PlanEvent.TAG_SKIPPED, "v2027.1.5.1 · not vYYYY.MM.DD.N"),
    )


def test_the_rows_stand_in_the_order_the_work_runs_notes_then_switch_offs_then_the_rest() -> None:
    rows = rows_of(
        plans=(app_plan(AppKind.ON),),
        offs=(OffPlan(App("legal", False, "master"), ()),),
        kept=(StaysStoppedNote("other", None),),
    )

    assert [row.event for row in rows] == [
        PlanEvent.STAYS_STOPPED,
        PlanEvent.SWITCH_OFF,
        PlanEvent.SWITCH_ON,
    ]
