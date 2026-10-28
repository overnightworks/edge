"""The rows `hal apply` prints for its app plan: what each kind of app entry says, from typed
fields (#296 S5-1)."""

from types import SimpleNamespace
from typing import Any, cast

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
from edge.core import Gate
from edge.plan import Plan
from edge.plan_rows import PlanEvent, PlanGroup, plan_groups
from edge.realm.reconcile import REALM_UNIT, RealmAction, RealmPlan, RealmRestart
from edge.unit_plans import ImageReference, KeptImage
from hal_output import Glyph, Group, Row

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
    }
    return AppPlan(**{**fields, **overrides})  # type: ignore[arg-type]


NO_REALM = RealmPlan()


def groups_of(
    images: tuple[KeptImage, ...] = (), realm: RealmPlan = NO_REALM, **parts: object
) -> tuple[Group, ...]:
    apps = AppsPlan(cast(AppRuntime, None), **parts)  # type: ignore[arg-type]
    plan = SimpleNamespace(app_plans=apps, images=list(images), realm=realm)
    return plan_groups(cast(Plan, plan))


def rows_of(**parts: object) -> tuple[Row, ...]:
    [units, apps] = groups_of(**parts)
    assert (units.heading, apps.heading) == (PlanGroup.UNITS, PlanGroup.APPS)
    return apps.rows


def unit_rows_of(**plan: Any) -> tuple[Row, ...]:
    return groups_of(**plan)[0].rows


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


def test_an_app_switched_off_is_a_remove_row() -> None:
    off = OffPlan(APP)

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
        offs=(OffPlan(App("legal", False, "master")),),
        kept=(StaysStoppedNote("other", None),),
    )

    assert [row.event for row in rows] == [
        PlanEvent.STAYS_STOPPED,
        PlanEvent.SWITCH_OFF,
        PlanEvent.SWITCH_ON,
    ]


DIGEST_A, DIGEST_B = "79c6" + "0" * 60, "a78d" + "0" * 60
KEYCLOAK = "quay.io/keycloak/keycloak:26.8.1"


def kept(running: ImageReference, configured: ImageReference) -> KeptImage:
    return KeptImage("keycloak", running, configured)


@pytest.mark.parametrize(
    ("image", "fact"),
    [
        pytest.param(
            kept(ImageReference(KEYCLOAK, DIGEST_A), ImageReference(KEYCLOAK, DIGEST_B)),
            f"{KEYCLOAK}@79c6… · compose.yaml @a78d…",
            id="same name",
        ),
        pytest.param(
            kept(
                ImageReference(KEYCLOAK, DIGEST_A),
                ImageReference(f"docker.io/{KEYCLOAK}", DIGEST_B),
            ),
            f"{KEYCLOAK}@79c6… · compose.yaml docker.io/{KEYCLOAK}@a78d…",
            id="differing names",
        ),
        pytest.param(
            kept(ImageReference(KEYCLOAK, DIGEST_A), ImageReference(KEYCLOAK, None)),
            f"{KEYCLOAK}@79c6… · compose.yaml {KEYCLOAK}",
            id="without a digest",
        ),
    ],
)
def test_a_kept_image_is_one_needs_you_row_with_both_versions(image: KeptImage, fact: str) -> None:
    assert unit_rows_of(images=(image,)) == (
        Row(Glyph.NEEDS_YOU, "keycloak", PlanEvent.OLD_IMAGE, fact),
    )


@pytest.mark.parametrize(
    ("realm", "expected"),
    [
        pytest.param(
            RealmPlan(RealmAction.RUN, why="keycloak/realm.yaml changed"),
            Row(
                Glyph.CHANGE,
                REALM_UNIT,
                PlanEvent.REALM_APPLY,
                "keycloak/*.yaml · keycloak/realm.yaml changed",
            ),
            id="run",
        ),
        pytest.param(
            RealmPlan(RealmAction.HELD, why="no realm apply is recorded", gate=Gate.OFF),
            Row(
                Glyph.UNCHANGED,
                REALM_UNIT,
                PlanEvent.REALM_HELD,
                "no realm apply is recorded · Keycloak is off",
            ),
            id="held",
        ),
        pytest.param(
            RealmPlan(RealmAction.NOT_CHECKED, restart=RealmRestart.DATABASE),
            Row(
                Glyph.UNCHANGED,
                REALM_UNIT,
                PlanEvent.REALM_LATER,
                "its database restarts now · next apply checks it",
            ),
            id="not checked",
        ),
        pytest.param(
            RealmPlan(RealmAction.UNREADABLE, error="the realm observer failed (x)"),
            Row(
                Glyph.NEEDS_YOU,
                REALM_UNIT,
                PlanEvent.REALM_SKIPPED,
                "the realm observer failed (x)",
            ),
            id="unreadable",
        ),
        pytest.param(RealmPlan(), None, id="nothing"),
    ],
)
def test_a_realm_plan_is_one_row_for_its_action_and_none_when_nothing_changes(
    realm: RealmPlan, expected: Row | None
) -> None:
    assert unit_rows_of(realm=realm) == (() if expected is None else (expected,))


def test_the_kept_images_come_before_the_realm_row_and_the_units_before_the_apps() -> None:
    image = kept(ImageReference(KEYCLOAK, DIGEST_A), ImageReference(KEYCLOAK, DIGEST_B))
    realm = RealmPlan(RealmAction.RUN, why="x")

    units, apps = groups_of(images=(image,), realm=realm, plans=(app_plan(AppKind.ON),))

    assert [row.event for row in units.rows] == [PlanEvent.OLD_IMAGE, PlanEvent.REALM_APPLY]
    assert apps.rows
