"""The rows `hal apply` prints for the legal pages and the appkit files: what each kind of change
says, from typed fields."""

from pathlib import Path

import pytest
from test_edge_plan_rows import (
    REPOSITORY,
    app_plan,
    groups_of,
    plain,
)

from edge.appkit_step import TARGETS, AppkitPlan
from edge.apps import AppKind
from edge.legal_step import BlockChange, Change, LegalPlan, PageChange
from edge.plan_rows import LegalEvent, PlanEvent, PlanGroup
from hal_output import EVENT_WIDTH, Glyph, Row, counted, write_groups


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
