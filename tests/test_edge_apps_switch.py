"""What `hal apply` does about an app that `hal stop` switched off (#255 P5, D2-D6, D10, B5, RV3):
the plan names it and fetches nothing, and a stop that comes in during the run wins wherever it
comes in, against a real origin and a scripted Docker."""

import dataclasses

import pytest
from app_support import KEY, NOW
from test_edge_apps import (
    FIRST,
    SECOND,
    World,
    deploy,
    docker,  # noqa: F401 the fixture, requested by name below
    home,  # noqa: F401 the fixture, requested by name below
    origin,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    runtime,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    world,  # noqa: F401 the fixture, requested by name below
)


def switch_off_meanwhile(world: World) -> None:  # noqa: F811
    """Another operator's `hal stop`: the marker, the first thing it writes."""
    world.marks.record(KEY, NOW)


STOPPED_LINE = f"{KEY} was switched off meanwhile (since 22.10. 12:00); not started"


def test_a_stopped_app_stays_stopped_and_is_neither_fetched_nor_built(world, origin):  # noqa: F811
    origin.release(FIRST)
    switch_off_meanwhile(world)
    world.runtime = dataclasses.replace(
        world.runtime, git=lambda *call: pytest.fail("git was asked")
    )

    plan = world.plan()

    assert plan.changes_nothing
    assert plan.describe() == [f"{KEY} stays stopped (switch since 22.10. 12:00)"]
    assert world.docker.verbs == []
    assert not world.srv.exists()


def test_a_barrier_left_without_a_marker_keeps_the_app_stopped(world, origin, repository):  # noqa: F811
    origin.release(FIRST)
    (repository / "dynamic").mkdir()
    (repository / "dynamic" / f"switch-{KEY}.yml").write_text("barrier\n")

    plan = world.plan()

    assert plan.changes_nothing
    assert plan.describe() == [f"{KEY} stays stopped (its barrier stands; no switch recorded)"]
    assert world.docker.verbs == []


def test_a_stop_during_the_build_runs_no_create_and_keeps_the_record(world, origin):  # noqa: F811
    deploy(world, origin, FIRST)
    origin.release(SECOND)
    plan = world.plan()
    world.docker.calls.clear()
    world.docker.during["build"] = lambda: switch_off_meanwhile(world)

    assert world.run(plan) == [STOPPED_LINE]

    assert "create" not in world.docker.verbs
    assert world.deployed()["tag"] == FIRST
    assert world.marks.mark(KEY) is not None


def test_a_stop_during_the_health_wait_ends_the_wait_without_a_record_and_removes_what_started(
    world,  # noqa: F811
    origin,  # noqa: F811
):
    world.docker.health_after_start = "starting"
    origin.release(FIRST)
    world.docker.during["start"] = lambda: world.docker.during.update(
        ps=lambda: switch_off_meanwhile(world)
    )

    assert world.run() == [STOPPED_LINE]

    assert world.deployed() is None
    assert world.docker.rows == []
    assert world.docker.locked_verbs() == {"create"}
    assert world.docker.verbs.count("start") == 1


def test_a_stop_after_the_health_wait_is_seen_under_the_lock_and_wins_over_the_record(
    world,  # noqa: F811
    origin,  # noqa: F811
):
    origin.release(FIRST)
    world.docker.during["start"] = lambda: switch_off_meanwhile(world)

    assert world.run() == [STOPPED_LINE]

    assert world.deployed() is None
    assert world.docker.rows == []


def test_a_barrier_that_appears_meanwhile_stops_the_run_like_a_marker(world, origin, repository):  # noqa: F811
    origin.release(FIRST)
    (repository / "dynamic").mkdir()

    def barrier_stands() -> None:
        (repository / "dynamic" / f"switch-{KEY}.yml").write_text("barrier\n")

    world.docker.during["build"] = barrier_stands

    assert world.run() == [f"{KEY} was switched off meanwhile; not started"]
    assert "create" not in world.docker.verbs


def test_a_stop_is_reported_in_place_of_a_start_that_failed(world, origin):  # noqa: F811
    origin.release(FIRST)
    world.docker.failing_verb = "start"
    world.docker.during["create"] = lambda: switch_off_meanwhile(world)

    [failure] = world.run()

    assert failure == STOPPED_LINE
