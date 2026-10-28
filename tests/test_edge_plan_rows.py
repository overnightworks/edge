"""The rows `hal apply` prints for its app plan and its unit plan: what each kind of entry says,
from typed fields, and the `find_row` helper the apply tests share."""

import re
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from edge import releases
from edge.appkit_step import TARGETS, AppkitPlan
from edge.apps import (
    AppKind,
    AppPlan,
    AppRuntime,
    AppsPlan,
    OffPlan,
    SkippedNote,
    StaysStoppedNote,
)
from edge.built_image import ENDS_RUNS, BuiltRecreate
from edge.catalog import App, Unit
from edge.core import (
    Cause,
    CauseKind,
    CoreAction,
    CorePlan,
    FollowerAction,
    FollowerPlan,
    Gate,
)
from edge.legal_step import BlockChange, Change, LegalPlan, PageChange
from edge.plan import Plan
from edge.plan_rows import LegalEvent, PlanEvent, PlanGroup, plan_groups
from edge.realm.reconcile import REALM_UNIT, RealmAction, RealmPlan, RealmRestart
from edge.unit_plans import Action, ImageReference, KeptImage, Recreate, UnitPlan
from hal_output import DETAIL_INDENT, EVENT_WIDTH, Glyph, Group, Row, counted, write_groups

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
NO_LEGAL = LegalPlan(Path(), Path(), {}, {})
REPOSITORY = Path("/repository")
NO_APPKIT = AppkitPlan("0.5.0", Path(), REPOSITORY, (), ())


def groups_of(
    images: tuple[KeptImage, ...] = (),
    realm: RealmPlan = NO_REALM,
    legal: LegalPlan = NO_LEGAL,
    appkit: AppkitPlan = NO_APPKIT,
    **parts: Any,
) -> tuple[Group, ...]:
    units = {
        key: parts.pop(key)
        for key in ("built", "recreates", "unit_plans", "core", "followers")
        if key in parts
    }
    apps = AppsPlan(cast(AppRuntime, None), **parts)  # type: ignore[arg-type]
    plan = SimpleNamespace(
        app_plans=apps,
        images=list(images),
        realm=realm,
        legal=legal,
        appkit=appkit,
        built=list(units.pop("built", ())),
        recreates=list(units.pop("recreates", ())),
        plans=list(units.pop("unit_plans", ())),
        core=list(units.pop("core", ())),
        followers=list(units.pop("followers", ())),
    )
    return plan_groups(cast(Plan, plan))


def rows_of(**parts: object) -> tuple[Row, ...]:
    [legal, core, units, apps] = groups_of(**parts)
    assert (legal.heading, core.heading, units.heading, apps.heading) == (
        None,
        PlanGroup.CORE,
        PlanGroup.UNITS,
        PlanGroup.APPS,
    )
    return apps.rows


def unit_rows_of(**plan: Any) -> tuple[Row, ...]:
    return groups_of(**plan)[2].rows


def plain(out: str) -> str:
    """The output without colour."""
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


def find_row(out: str, subject: str, event: StrEnum, fact: str = "") -> re.Match[str] | None:
    pattern = (
        rf"^  \S {re.escape(subject)} +(?:\d+ )?{re.escape(str(event))}s?\b.*{re.escape(fact)}"
    )
    return re.search(pattern, plain(out), re.MULTILINE)


def detail_lines(out: str) -> list[str]:
    """The lines indented under a row, without colour."""
    return [
        line[len(DETAIL_INDENT) :]
        for line in plain(out).splitlines()
        if line.startswith(DETAIL_INDENT)
    ]


def row_at(out: str, subject: str, event: StrEnum, fact: str = "") -> int:
    row = find_row(out, subject, event, fact)
    assert row, out
    return row.start()


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

    _, _, units, apps = groups_of(images=(image,), realm=realm, plans=(app_plan(AppKind.ON),))

    assert [row.event for row in units.rows] == [PlanEvent.OLD_IMAGE, PlanEvent.REALM_APPLY]
    assert apps.rows


STEP_CA = Unit("step-ca", True, None)
SERVICES = ("step-ca", "traefik-mcp")
SINCE = "2026-10-17T22:10:00"


def built(**overrides: Any) -> BuiltRecreate:
    fields: dict[str, Any] = {
        "unit": "agent-service",
        "service": "agent-service",
        "reference": "agent-service:local",
        "moved_from": (),
        "definition_changed": False,
        "rebuilt": False,
    }
    return BuiltRecreate(**{**fields, **overrides})


def unit_plan(action: Action, **overrides: Any) -> UnitPlan:
    fields: dict[str, Any] = {"unit": STEP_CA, "services": SERVICES, "volumes": ("step-ca",)}
    return UnitPlan(action=action, **{**fields, **overrides})


@pytest.mark.parametrize(
    ("parts", "row"),
    [
        pytest.param(
            {"recreates": (Recreate("keycloak", "keycloak-db"),)},
            Row(Glyph.CHANGE, "keycloak-db", PlanEvent.RECREATE, "definition changed"),
            id="recreate",
        ),
        pytest.param(
            {"built": (built(definition_changed=True),)},
            Row(
                Glyph.CHANGE,
                "agent-service",
                PlanEvent.RECREATE,
                f"definition changed · {ENDS_RUNS}",
            ),
            id="built definition",
        ),
        pytest.param(
            {"built": (built(moved_from=("agent-service:old", "agent-service:older")),)},
            Row(
                Glyph.CHANGE,
                "agent-service",
                PlanEvent.RECREATE,
                "image agent-service:old → agent-service:local"
                " · image agent-service:older → agent-service:local"
                f" · {ENDS_RUNS}",
            ),
            id="built image moved",
        ),
        pytest.param(
            {"built": (built(rebuilt=True),)},
            Row(
                Glyph.CHANGE,
                "agent-service",
                PlanEvent.RECREATE,
                f"image agent-service:local rebuilt · {ENDS_RUNS}",
            ),
            id="built rebuilt",
        ),
        pytest.param(
            {"built": (built(moved_from=("a:1",), definition_changed=True, rebuilt=True),)},
            Row(
                Glyph.CHANGE,
                "agent-service",
                PlanEvent.RECREATE,
                "image a:1 → agent-service:local · definition changed"
                f" · image agent-service:local rebuilt · {ENDS_RUNS}",
            ),
            id="built combination",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.ON, volumes=()),)},
            Row(Glyph.ADD, "step-ca", PlanEvent.SWITCH_ON, "step-ca, traefik-mcp"),
            id="switch on",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.OFF),)},
            Row(
                Glyph.REMOVE,
                "step-ca",
                PlanEvent.SWITCH_OFF,
                "step-ca, traefik-mcp · volumes step-ca kept",
            ),
            id="switch off with volumes",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.OFF, switched_off_since=SINCE),)},
            Row(
                Glyph.REMOVE,
                "step-ca",
                PlanEvent.SWITCH_OFF,
                "step-ca, traefik-mcp · volumes step-ca kept · its switch since 17.10. 22:10 stays",
            ),
            id="switch off with a switch since",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.OFF, volumes=()),)},
            Row(Glyph.REMOVE, "step-ca", PlanEvent.SWITCH_OFF, "step-ca, traefik-mcp"),
            id="switch off without volumes",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.STAYS_STOPPED, switched_off_since=SINCE),)},
            Row(
                Glyph.UNCHANGED,
                "step-ca",
                PlanEvent.STAYS_STOPPED,
                "switched off since 17.10. 22:10",
            ),
            id="stays stopped with a switch since",
        ),
        pytest.param(
            {"unit_plans": (unit_plan(Action.STAYS_STOPPED),)},
            Row(Glyph.UNCHANGED, "step-ca", PlanEvent.STAYS_STOPPED, "barrier stands"),
            id="stays stopped with a barrier",
        ),
    ],
)
def test_a_unit_part_is_one_row_of_its_typed_fields(parts: dict[str, Any], row: Row) -> None:
    assert unit_rows_of(**parts) == (row,)


def test_the_unit_rows_stand_built_then_recreates_then_plans_then_images_then_the_realm() -> None:
    image = kept(ImageReference(KEYCLOAK, DIGEST_A), ImageReference(KEYCLOAK, DIGEST_B))

    _, _, units, apps = groups_of(
        built=(built(rebuilt=True),),
        recreates=(Recreate("keycloak", "keycloak-db"),),
        unit_plans=(unit_plan(Action.ON), unit_plan(Action.STAYS_STOPPED)),
        images=(image,),
        realm=RealmPlan(RealmAction.RUN, why="x"),
        plans=(app_plan(AppKind.ON),),
    )

    assert [(row.subject, row.event) for row in units.rows] == [
        ("agent-service", PlanEvent.RECREATE),
        ("keycloak-db", PlanEvent.RECREATE),
        ("step-ca", PlanEvent.SWITCH_ON),
        ("step-ca", PlanEvent.STAYS_STOPPED),
        ("keycloak", PlanEvent.OLD_IMAGE),
        (REALM_UNIT, PlanEvent.REALM_APPLY),
    ]
    assert apps.rows


def core_rows_of(**parts: Any) -> tuple[Row, ...]:
    return groups_of(**parts)[1].rows


def core_plan(action: CoreAction, *why: Cause) -> CorePlan:
    return CorePlan("socket-proxy", action, why, {}, False)


def follower_plan(action: FollowerAction, gate: Gate, *why: Cause) -> FollowerPlan:
    return FollowerPlan("traefik-mcp", "step-ca", action, why, {}, gate)


DEFINITION = Cause(CauseKind.DEFINITION_CHANGED)
FOLLOWER_FILE = Cause(CauseKind.FILE_UNRECORDED, "traefik-mcp.yml")


@pytest.mark.parametrize(
    ("cause", "fact"),
    [
        pytest.param(DEFINITION, "definition changed", id="definition changed"),
        pytest.param(
            Cause(CauseKind.FILE_CHANGED, "traefik.yml"), "traefik.yml changed", id="file"
        ),
        pytest.param(
            Cause(CauseKind.FILE_UNRECORDED, "traefik.yml"),
            "traefik.yml has no applied record",
            id="file unrecorded",
        ),
        pytest.param(
            Cause(CauseKind.UNFINISHED), "the last recreate did not finish", id="unfinished"
        ),
        pytest.param(
            Cause(CauseKind.RECORD_UNTRUSTED, "/x/applied.json is malformed: it is not JSON"),
            "/x/applied.json is malformed: it is not JSON",
            id="record untrusted",
        ),
        pytest.param(
            Cause(CauseKind.RECORD_NAMES_OTHERS, "old.yml, older.yml"),
            "applied.json names old.yml, older.yml, which socket-proxy does not mount",
            id="record names others",
        ),
        pytest.param(
            Cause(CauseKind.PULLED_IN, "socket-proxy"), "socket-proxy is recreated", id="pulled in"
        ),
    ],
)
def test_every_cause_of_a_recreated_core_service_is_one_fact(cause: Cause, fact: str) -> None:
    plan = core_plan(CoreAction.RECREATE, cause)

    assert core_rows_of(core=(plan,)) == (
        Row(Glyph.CHANGE, "socket-proxy", PlanEvent.RECREATE, fact),
    )


def test_several_causes_are_one_row_in_the_order_they_are_planned() -> None:
    plan = CorePlan(
        "traefik",
        CoreAction.RECREATE,
        (DEFINITION, Cause(CauseKind.FILE_CHANGED, "traefik.yml")),
        {},
        False,
    )

    assert core_rows_of(core=(plan,)) == (
        Row(
            Glyph.CHANGE, "traefik", PlanEvent.RECREATE, "definition changed · traefik.yml changed"
        ),
    )


def test_a_core_service_that_is_not_running_is_a_row_that_says_it_is_not_started() -> None:
    plan = core_plan(CoreAction.NOT_STARTED, DEFINITION)

    assert core_rows_of(core=(plan,)) == (
        Row(
            Glyph.UNCHANGED,
            "socket-proxy",
            PlanEvent.NOT_STARTED,
            "definition changed · not running",
        ),
    )


@pytest.mark.parametrize(
    ("action", "gate", "row"),
    [
        pytest.param(
            FollowerAction.RECREATE,
            Gate.ON,
            Row(
                Glyph.CHANGE,
                "traefik-mcp",
                PlanEvent.RECREATE,
                "traefik-mcp.yml has no applied record",
            ),
            id="recreate",
        ),
        pytest.param(
            FollowerAction.STARTS_WITH_UNIT,
            Gate.ON,
            Row(
                Glyph.CHANGE,
                "traefik-mcp",
                PlanEvent.STARTS_LATER,
                "traefik-mcp.yml has no applied record · with step-ca",
            ),
            id="starts with its unit",
        ),
        pytest.param(
            FollowerAction.HELD,
            Gate.OFF,
            Row(
                Glyph.UNCHANGED,
                "traefik-mcp",
                PlanEvent.NOT_STARTED,
                "traefik-mcp.yml has no applied record · step-ca is off",
            ),
            id="held, unit off",
        ),
        pytest.param(
            FollowerAction.HELD,
            Gate.SWITCHED_OFF,
            Row(
                Glyph.UNCHANGED,
                "traefik-mcp",
                PlanEvent.NOT_STARTED,
                "traefik-mcp.yml has no applied record · step-ca is switched off",
            ),
            id="held, unit switched off",
        ),
    ],
)
def test_a_follower_is_one_row_and_a_waiting_one_ends_with_the_state_of_its_unit(
    action: FollowerAction, gate: Gate, row: Row
) -> None:
    assert core_rows_of(followers=(follower_plan(action, gate, FOLLOWER_FILE),)) == (row,)


def test_the_core_group_holds_core_plans_then_followers_and_stands_before_units_and_apps() -> None:
    _, core, units, apps = groups_of(
        core=(core_plan(CoreAction.RECREATE, DEFINITION),),
        followers=(follower_plan(FollowerAction.RECREATE, Gate.ON, DEFINITION),),
        unit_plans=(unit_plan(Action.ON),),
        plans=(app_plan(AppKind.ON),),
    )

    assert [group.heading for group in (core, units, apps)] == list(PlanGroup)
    assert [row.subject for row in core.rows] == ["socket-proxy", "traefik-mcp"]


def legal_plan(
    changed: dict[str, PageChange], blocks: dict[str, tuple[BlockChange, ...]] | None = None
) -> LegalPlan:
    return LegalPlan(Path(), Path(), {page: "" for page in changed}, changed, blocks or {})


SECTION = "legal/sections/agent-service/datenschutz.html"
FRAME = "legal/frame/datenschutz.html"


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        pytest.param(
            legal_plan({"datenschutz": PageChange.NEW}),
            [Row(Glyph.ADD, "legal", LegalEvent.NEW_PAGE, "datenschutz", 1)],
            id="one new page",
        ),
        pytest.param(
            legal_plan({"datenschutz": PageChange.NEW, "nutzungsbedingungen": PageChange.NEW}),
            [Row(Glyph.ADD, "legal", LegalEvent.NEW_PAGE, "datenschutz, nutzungsbedingungen", 2)],
            id="several new pages",
        ),
        pytest.param(
            legal_plan(
                {"datenschutz": PageChange.CHANGED},
                {
                    "datenschutz": (
                        BlockChange(FRAME, Change.CHANGED),
                        BlockChange(SECTION, Change.CHANGED, "agent-dienst"),
                        BlockChange(SECTION, Change.LEFT, "alt"),
                    )
                },
            ),
            [
                Row(
                    Glyph.CHANGE,
                    "legal",
                    LegalEvent.EDITED_PAGE,
                    "datenschutz",
                    1,
                    (f"{FRAME} changed", f"{SECTION}#agent-dienst changed", f"{SECTION}#alt left"),
                )
            ],
            id="an edited page with frame, changed and left lines",
        ),
        pytest.param(
            legal_plan({"datenschutz": PageChange.UNRECORDED, "impressum": PageChange.UNRECORDED}),
            [Row(Glyph.CHANGE, "legal", LegalEvent.NEW_RECORD, "datenschutz, impressum", 2)],
            id="records only",
        ),
        pytest.param(
            legal_plan(
                {
                    "a": PageChange.UNRECORDED,
                    "b": PageChange.CHANGED,
                    "c": PageChange.NEW,
                },
                {"b": (BlockChange(SECTION, Change.NEW, "x"),)},
            ),
            [
                Row(Glyph.ADD, "legal", LegalEvent.NEW_PAGE, "c", 1),
                Row(
                    Glyph.CHANGE,
                    "legal",
                    LegalEvent.EDITED_PAGE,
                    "b",
                    1,
                    (f"{SECTION}#x new",),
                ),
                Row(Glyph.CHANGE, "legal", LegalEvent.NEW_RECORD, "a", 1),
            ],
            id="the kinds stand new, changed, record",
        ),
        pytest.param(legal_plan({}), [], id="nothing changes"),
    ],
)
def test_the_legal_pages_are_one_row_per_kind_with_a_count(
    plan: LegalPlan, expected: list[Row]
) -> None:
    assert list(groups_of(legal=plan)[0].rows) == expected


@pytest.mark.parametrize("event", list(LegalEvent))
@pytest.mark.parametrize("count", [1, 2, 3])
def test_every_legal_event_with_a_count_fits_the_event_column(
    event: LegalEvent, count: int
) -> None:
    assert len(counted(count, event)) <= EVENT_WIDTH


def test_the_legal_rows_stand_first_in_a_group_without_a_heading() -> None:
    groups = groups_of(legal=legal_plan({"datenschutz": PageChange.NEW}))

    assert [group.heading for group in groups] == [None, *PlanGroup]


def appkit_plan(changed_labels: tuple[str, ...] = (), stale: tuple[str, ...] = ()) -> AppkitPlan:
    changed = tuple(
        target.served(REPOSITORY, "0.5.0") for target in TARGETS if target.label in changed_labels
    )
    return AppkitPlan("0.5.0", Path(), REPOSITORY, changed, tuple(Path(path) for path in stale))


ALL_LABELS = tuple(target.label for target in TARGETS)


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        pytest.param(appkit_plan(), [], id="unchanged prints nothing"),
        pytest.param(
            appkit_plan(ALL_LABELS),
            [Row(Glyph.ADD, "appkit", PlanEvent.NEW_FILES, "0.5.0")],
            id="every target changed",
        ),
        pytest.param(
            appkit_plan(("login theme",)),
            [Row(Glyph.ADD, "appkit", PlanEvent.NEW_FILES, "0.5.0 · only login theme")],
            id="one target changed",
        ),
        pytest.param(
            appkit_plan(("login theme", "account theme")),
            [
                Row(
                    Glyph.ADD,
                    "appkit",
                    PlanEvent.NEW_FILES,
                    "0.5.0 · only login theme, account theme",
                )
            ],
            id="two targets changed",
        ),
        pytest.param(
            appkit_plan(stale=("legal/assets/appkit-0.4.0",)),
            [Row(Glyph.REMOVE, "appkit", PlanEvent.OLD_FILES, "appkit-0.4.0")],
            id="stale only, one name",
        ),
        pytest.param(
            appkit_plan(
                stale=(
                    "legal/assets/appkit-0.4.0",
                    "keycloak/themes/hallucinai/login/resources/appkit-0.3.0",
                    "keycloak/themes/hallucinai/account/resources/appkit-0.4.0",
                )
            ),
            [Row(Glyph.REMOVE, "appkit", PlanEvent.OLD_FILES, "appkit-0.3.0, appkit-0.4.0")],
            id="stale only, two names sorted and unique",
        ),
        pytest.param(
            appkit_plan(ALL_LABELS, stale=("legal/assets/appkit-0.4.0",)),
            [Row(Glyph.ADD, "appkit", PlanEvent.NEW_FILES, "0.5.0 · removes appkit-0.4.0")],
            id="changed plus stale",
        ),
        pytest.param(
            appkit_plan(("account theme",), stale=("legal/assets/appkit-0.4.0",)),
            [
                Row(
                    Glyph.ADD,
                    "appkit",
                    PlanEvent.NEW_FILES,
                    "0.5.0 · only account theme · removes appkit-0.4.0",
                )
            ],
            id="partial plus stale",
        ),
    ],
)
def test_the_appkit_step_is_one_row_naming_what_differs(
    plan: AppkitPlan, expected: list[Row]
) -> None:
    assert list(groups_of(appkit=plan)[0].rows) == expected


def test_the_appkit_row_stands_before_the_legal_rows_in_the_group_without_a_heading() -> None:
    groups = groups_of(
        appkit=appkit_plan(ALL_LABELS), legal=legal_plan({"datenschutz": PageChange.NEW})
    )

    assert [row.subject for row in groups[0].rows] == ["appkit", "legal"]


def test_the_appkit_row_counts_toward_the_heading_threshold(capsys: pytest.CaptureFixture) -> None:
    pages = {"datenschutz": PageChange.NEW}
    plan_without = groups_of(legal=legal_plan(pages), plans=(app_plan(AppKind.ON),) * 4)
    plan_with = groups_of(
        appkit=appkit_plan(ALL_LABELS),
        legal=legal_plan(pages),
        plans=(app_plan(AppKind.ON),) * 4,
    )

    write_groups(plan_without)
    without = capsys.readouterr().out
    write_groups(plan_with)
    with_appkit = capsys.readouterr().out

    assert "apps" not in plain(without).splitlines()
    assert "apps" in plain(with_appkit).splitlines()
