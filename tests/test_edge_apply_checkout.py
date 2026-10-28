"""`edge apply --closed` prints the live checkout against origin's `main` as one row (#612), in a
real git repository on the scripted boundaries of `test_edge_apply`."""

import pytest
from git_support import advance, checkout_state, subjects
from test_edge_apply import (
    DISABLED,
    applied_core,  # noqa: F401 the fixture, requested by name below
    at_terminal,  # noqa: F401 the fixture, requested by name below
    core_files,  # noqa: F401 the fixture, requested by name below
    home,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    origin,  # noqa: F401 the fixture, requested by name below
    real_main,  # noqa: F401 the fixture, requested by name below
    realm_backup,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    short,
    state,  # noqa: F401 the fixture, requested by name below
    switch,  # noqa: F401 the fixture, requested by name below
)
from test_edge_plan_rows import detail_lines, find_row, plain

from edge import checkout
from edge.plan_rows import CHECKOUT, CheckoutEvent, PlanEvent

OLD_TEXT = (
    "fetched main from",
    "live checkout current (",
    "wait on origin",
    "their files, by what",
)


def test_an_equal_main_is_one_current_row_before_the_plan(switch, real_main, origin):  # noqa: F811
    exit_code, out, _ = switch(git=checkout.run_git)

    assert exit_code == 0
    assert find_row(out, CHECKOUT, CheckoutEvent.CURRENT, f"{short(real_main)} · origin/main")
    assert plain(out).index("  ✓ checkout") < plain(out).index("the plan")
    assert not [text for text in OLD_TEXT if text in out]


@pytest.mark.parametrize("waiting", [1, 3])
def test_waiting_commits_are_one_row_with_the_merge_command_and_head_is_planned(
    switch, scripted, real_main, origin, waiting  # noqa: F811
):  # fmt: skip
    """The plan applies HEAD, whose fleet.yaml still enables step-ca; HEAD, the index and the
    working tree stay byte-identical."""
    tip = advance(
        origin,
        *subjects(*(f"waiting {n}" for n in range(waiting))),
        (b"f", {b"fleet.yaml": DISABLED.encode()}),
    )
    before = checkout_state(real_main)

    exit_code, out, _ = switch("no", git=checkout.run_git)

    assert find_row(
        out, CHECKOUT, CheckoutEvent.BEHIND, f"plan applies {short(real_main)} without them"
    )
    assert f"{waiting + 1} behind" in plain(out)
    assert detail_lines(out)[0] == f"git -C ~/git/edge merge --ff-only {tip}"
    assert find_row(out, "step-ca", PlanEvent.SWITCH_ON)
    assert not [text for text in OLD_TEXT if text in out]
    assert exit_code == 1
    assert checkout_state(real_main) == before
