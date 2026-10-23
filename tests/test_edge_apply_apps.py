"""`edge apply --closed` with apps (#255 P1, A7, A10, A20, A22): one plan and one `yes`, the app
step after the units and the realm, on the scripted boundaries of `test_edge_apply` and the real
origin and scripted Docker of `app_support`."""

import io
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from app_support import FLEET, KEY, ScriptedDocker, origin_at, runtime_of, switch_lock_is_held
from test_edge_apply import (
    NOW,
    ZONE,
    at_terminal,  # noqa: F401 the fixture, requested by name below
    home,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    realm_backup,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    switch,  # noqa: F401 the fixture, requested by name below
    write_fleet,
)

from edge import barrier, checkout, compose, emergency, switches

APP = "github: {{org: overnightworks}}\nservices:\n  hopin:\n    repo: {repo}\n    run: {run}\n"


INVALID_APPS = [
    (APP.format(repo="{}", run="{enabled: true}"), "services.hopin.repo.default_branch"),
    (
        APP.format(repo="{default_branch: 'a b'}", run="{enabled: true}"),
        "services.hopin.repo.default_branch",
    ),
    (
        APP.format(repo="{default_branch: main, visibility: public}", run="{enabled: true}"),
        "services.hopin.repo.visibility",
    ),
    (
        APP.format(repo="{default_branch: main}", run="{enabled: true, setup: step-ca/init}"),
        "services.hopin.run.setup",
    ),
    (APP.format(repo="{default_branch: main}", run="{}"), "services.hopin.run.enabled"),
    (APP.format(repo="[main]", run="{enabled: true}"), "services.hopin.repo: must be a map"),
    (
        APP.format(repo="{default_branch: main}", run="[enabled]"),
        "services.hopin.run: must be a map",
    ),
    (
        APP.format(repo="{default_branch: main}", run="{enabled: true}").replace("hopin", "Hop.in"),
        "services.Hop.in: an app's key must be a compose project name",
    ),
    (
        APP.format(repo="{default_branch: main}", run="{enabled: true}").replace(
            "hopin", "step-ca"
        ),
        "services.step-ca: an app's key is no compose profile or project of edge",
    ),
    (
        APP.format(repo="{default_branch: main}", run="{enabled: true}").replace("hopin", "edge"),
        "services.edge: an app's key is no compose profile or project of edge",
    ),
    (
        APP.format(repo="{default_branch: main}", run="{enabled: true}").replace(
            "github: {org: overnightworks}\n", ""
        ),
        "github.org",
    ),
]


@dataclass
class AppWorld:
    """An app `hopin` in the catalog of the live checkout, a real origin it is checked out from and
    a scripted Docker; the catalog holds no unit, so only the app has anything to change."""

    run: object
    repository: Path
    scripted: object
    origin: object
    docker: ScriptedDocker
    runtime: object
    state: Path
    srv: Path

    def apply(self, answer: str = "yes") -> tuple[int, str, str]:
        return self.run(answer, app_runtime=self.runtime)


@pytest.fixture
def app_world(request, tmp_path):
    needs = {name: request.getfixturevalue(name) for name in ("repository", "state", "home")}
    tree, state_directory, operator_home = needs.values()
    write_fleet(tree, f"cloudflare:\n  zone: {ZONE}\n{FLEET}")
    srv = operator_home / "srv" / KEY
    docker = ScriptedDocker(state_directory, srv)
    return AppWorld(
        request.getfixturevalue("switch"), tree, request.getfixturevalue("scripted"),
        origin_at(tmp_path, operator_home), docker,
        runtime_of(operator_home, state_directory, docker), state_directory, srv,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("fleet", "named"),
    INVALID_APPS,
)
def test_an_invalid_app_entry_is_cannot_plan_with_one_line_naming_the_key(app_world, fleet, named):
    """A20: each app counterexample is exit 2 with a line that names the key."""
    write_fleet(app_world.repository, fleet)

    exit_code, _, err = app_world.run()

    assert exit_code == 2
    assert named in err
    assert app_world.scripted.changing_verbs() == []


def test_an_app_key_is_held_against_the_name_of_the_checkouts_own_compose_project(app_world):
    app_world.scripted.config["name"] = "mainline"
    fleet = f"cloudflare:\n  zone: {ZONE}\n{FLEET}".replace("hopin", "mainline")
    write_fleet(app_world.repository, fleet)

    exit_code, _, err = app_world.run()

    assert exit_code == 2
    assert "services.mainline: an app's key is no compose profile or project of edge" in err


def test_an_app_is_planned_and_started_after_one_yes_and_the_next_apply_changes_nothing(app_world):
    sha = app_world.origin.release("v2026.10.22.1")

    exit_code, out, err = app_world.apply()

    assert exit_code == 0, err
    assert f"hopin: on at v2026.10.22.1 ({sha}) from origin master, into ~/srv/hopin" in out
    assert "edge apply --closed: hopin is on at v2026.10.22.1" in out
    again = app_world.apply()
    assert again[0] == 0
    assert "nothing to change" in again[1]
    assert app_world.docker.verbs.count("create") == 1


def test_declining_the_plan_leaves_the_checkout_the_containers_and_the_record_alone(app_world):
    app_world.origin.release("v2026.10.22.1")

    exit_code, _, err = app_world.apply("no")

    assert exit_code == 1
    assert "aborted, nothing changed" in err
    assert set(app_world.docker.verbs) == {"ps"}
    assert not (app_world.state / "deployed").exists()
    assert not checkout.Git(app_world.srv).has_commit()


def test_an_app_that_is_not_healthy_ends_partial_with_exit_1_and_no_record(app_world):
    app_world.origin.release("v2026.10.22.1")
    app_world.docker.health_after_start = "unhealthy"

    exit_code, _, err = app_world.apply()

    assert exit_code == 1
    assert "partial, hopin is not healthy at v2026.10.22.1 after 300s" in err
    assert not (app_world.state / "deployed").exists()


def test_a_ctrl_c_during_the_create_names_the_app_and_says_the_containers_may_be_partly_replaced(
    app_world,
):
    app_world.origin.release("v2026.10.22.1")
    app_world.docker.interrupted_verb = "create"

    exit_code, _, err = app_world.apply()

    assert exit_code == 1
    assert "interrupted while changing hopin (create the containers at v2026.10.22.1" in err
    assert "partly replaced" in err
    assert not (app_world.state / "deployed").exists()


def test_an_app_without_a_release_is_cannot_plan_and_nothing_is_asked(app_world):
    exit_code, out, err = app_world.apply()

    assert exit_code == 2
    assert "cannot plan, services.hopin: no vYYYY.MM.DD.N release on origin master" in err
    assert "the plan" not in out
    assert app_world.docker.verbs == []


def test_a_disabled_app_is_neither_fetched_nor_run(app_world):
    write_fleet(
        app_world.repository, f"cloudflare:\n  zone: {ZONE}\n" + FLEET.replace("true", "false")
    )

    exit_code, out, _ = app_world.apply()

    assert exit_code == 0
    assert "nothing to change" in out
    assert app_world.docker.verbs == ["ps"]


RELEASE = "v2026.10.22.1"


@dataclass
class Hal:
    """`hal stop hopin` and `hal start hopin` as the operator runs them, on the same boundaries."""

    world: AppWorld
    at_terminal: object
    capsys: object
    verifier: object
    monkeypatch: pytest.MonkeyPatch

    def stop(self, key: str = KEY) -> tuple[int, str, str]:
        """With no terminal: stdout is a plain buffer."""
        self.monkeypatch.setattr(sys, "stdout", io.StringIO())
        self.monkeypatch.setattr(sys, "stdin", io.StringIO())
        exit_code = emergency.stop_unit(
            switches.Switches(self.world.state), key, NOW, self.engine(), self.world.repository,
            self.verifier, self.world.runtime,
        )  # fmt: skip
        return exit_code, sys.stdout.getvalue(), self.capsys.readouterr().err

    def start(self, answer: str = "yes", while_prompt_waits=None) -> tuple[int, str, str]:
        self.at_terminal(answer, while_prompt_waits)
        exit_code = emergency.start_unit(
            switches.Switches(self.world.state), KEY, self.engine(), self.world.repository,
            self.verifier, runtime=self.world.runtime,
        )  # fmt: skip
        return exit_code, sys.stdout.getvalue(), self.capsys.readouterr().err

    def engine(self) -> compose.Compose:
        return compose.Compose(self.world.scripted.run, self.world.repository)


@pytest.fixture
def hal(app_world, at_terminal, capsys, ingress, monkeypatch):  # noqa: F811
    return Hal(app_world, at_terminal, capsys, ingress.verifier, monkeypatch)


def deployed(app_world: AppWorld) -> None:
    app_world.origin.release(RELEASE)
    assert app_world.apply()[0] == 0


def test_hal_stop_writes_the_marker_and_stops_the_containers_without_a_terminal(app_world, hal):
    deployed(app_world)

    exit_code, out, _ = hal.stop()

    assert exit_code == 0
    assert "hopin is switched off (containers of hopin stopped; volumes stay)" in out
    assert switches.Switches(app_world.state).mark(KEY) is not None
    assert {row["State"] for row in app_world.docker.rows} == {"exited"}


def test_hal_stop_of_an_app_records_the_marker_before_it_plans_anything(app_world, hal):
    deployed(app_world)
    marks = switches.Switches(app_world.state)
    planned_with_marker: list[bool] = []
    scripted_run = app_world.scripted.run

    def planner(*call):
        planned_with_marker.append(marks.mark(KEY) is not None)
        return scripted_run(*call)

    app_world.scripted.run = planner
    app_world.docker.during["ps"] = lambda: planned_with_marker.append(marks.mark(KEY) is not None)

    assert hal.stop()[0] == 0

    assert planned_with_marker and all(planned_with_marker)


UNSAFE_APP = "services:\n  '{key}': {{repo: {{default_branch: master}}, run: {{enabled: true}}}}\n"


@pytest.mark.parametrize(
    ("services", "key"),
    [
        ("services: null\n", KEY),
        ("services: [hopin]\n", KEY),
        (UNSAFE_APP, f"../{switches.GLOBAL_STOP_FILE}"),
        (UNSAFE_APP, "{outside}"),
    ],
)
def test_hal_stop_on_a_malformed_catalog_or_an_unsafe_app_key_cannot_plan_and_writes_nothing(
    app_world, hal, tmp_path, services, key
):
    outside = tmp_path / "outside"
    key = key.format(outside=outside)
    write_fleet(
        app_world.repository,
        f"cloudflare:\n  zone: {ZONE}\ngithub: {{org: overnightworks}}\n"
        + services.format(key=key),
    )

    exit_code, _, err = hal.stop(key)

    assert exit_code == emergency.ApplyExit.CANNOT_PLAN
    assert "edge stop: cannot plan" in err
    assert [path for path in app_world.state.rglob("*") if path.is_file()] == []
    assert not outside.exists()


def test_hal_stop_of_a_disabled_app_with_nothing_running_says_so(app_world, hal):
    write_fleet(
        app_world.repository, f"cloudflare:\n  zone: {ZONE}\n" + FLEET.replace("true", "false")
    )

    exit_code, out, _ = hal.stop()

    assert exit_code == 0
    assert "hopin is disabled in the catalog and nothing of it runs" in out
    assert switches.Switches(app_world.state).mark(KEY) is None


def test_hal_start_starts_the_stopped_containers_ends_the_switch_and_says_so(app_world, hal):
    deployed(app_world)
    hal.stop()

    exit_code, out, _ = hal.start()

    assert exit_code == 0
    assert "edge start: hopin is on" in out
    assert switches.Switches(app_world.state).mark(KEY) is None
    assert {row["Health"] for row in app_world.docker.rows} == {"healthy"}


def test_hal_start_is_refused_while_the_app_is_disabled_and_changes_nothing(app_world, hal):
    deployed(app_world)
    hal.stop()
    write_fleet(
        app_world.repository, f"cloudflare:\n  zone: {ZONE}\n" + FLEET.replace("true", "false")
    )

    exit_code, _, err = hal.start()

    assert exit_code == 1
    assert "refused, hopin is disabled in the catalog" in err
    assert switches.Switches(app_world.state).mark(KEY) is not None
    assert {row["State"] for row in app_world.docker.rows} == {"exited"}


def test_hal_start_of_an_app_whose_containers_are_gone_ends_the_switch_for_apply_to_start(
    app_world, hal
):
    deployed(app_world)
    hal.stop()
    app_world.docker.rows.clear()

    exit_code, out, _ = hal.start()

    assert exit_code == 0
    assert switches.Switches(app_world.state).mark(KEY) is None
    assert "no container of it exists" in out


def test_a_stop_while_hal_start_waits_for_health_wins_and_stops_the_containers_again(
    app_world, hal
):
    deployed(app_world)
    hal.stop()
    app_world.docker.health_after_start = "starting"
    app_world.docker.during["start"] = lambda: app_world.docker.during.update(
        ps=lambda: switches.Switches(app_world.state).record(KEY, NOW)
    )

    exit_code, _, err = hal.start()

    assert exit_code == 1
    assert "hopin was switched off meanwhile" in err
    assert switches.Switches(app_world.state).mark(KEY) is not None
    assert {row["State"] for row in app_world.docker.rows} == {"exited"}


def test_a_stop_while_apply_waits_at_the_yes_prompt_checks_out_and_builds_nothing(app_world):
    app_world.origin.release(RELEASE)
    marks = switches.Switches(app_world.state)

    exit_code, _, err = app_world.run(
        "yes", lambda: marks.record(KEY, NOW), app_runtime=app_world.runtime
    )

    assert exit_code == 1
    assert "hopin was switched off meanwhile (since 18.10. 08:00); not started" in err
    assert "build" not in app_world.docker.verbs
    assert [path.name for path in app_world.srv.iterdir()] == [".git"]
    assert marks.mark(KEY) is not None


def test_a_marked_app_with_a_newer_tag_waiting_stays_stopped_and_its_marker_stays(app_world):
    app_world.origin.release(RELEASE)
    marks = switches.Switches(app_world.state)
    marks.record(KEY, NOW)
    marker = (app_world.state / switches.STOPPED_DIRECTORY / KEY).read_bytes()

    exit_code, out, _ = app_world.apply()

    assert exit_code == 0
    assert "hopin stays stopped (switch since 18.10. 08:00)" in out
    assert "nothing to change" in out
    assert (app_world.state / switches.STOPPED_DIRECTORY / KEY).read_bytes() == marker
    assert app_world.docker.verbs == []
    assert not app_world.srv.exists()


def test_a_stop_that_comes_in_while_apply_holds_the_lock_waits_for_it_then_stops_what_started(
    app_world, hal
):
    app_world.origin.release(RELEASE)
    marks = switches.Switches(app_world.state)
    stopped: list[int] = []

    def stop_the_app() -> None:
        stopped.append(
            emergency.stop_unit(
                marks, KEY, NOW, hal.engine(), app_world.repository, hal.verifier, app_world.runtime
            )
        )

    stop = threading.Thread(target=stop_the_app)
    waited: list[bool] = []

    def hal_stop_while_start_holds_the_lock() -> None:
        assert switch_lock_is_held(app_world.state)
        stop.start()
        while marks.mark(KEY) is None:  # the stop records first, then waits for the lock
            stop.join(0.01)
        waited.append(stop.is_alive() and not stopped)

    app_world.docker.during["start"] = hal_stop_while_start_holds_the_lock

    exit_code, _, err = app_world.apply()
    stop.join()

    assert waited == [True]
    assert stopped == [0]
    assert exit_code == 1
    assert "hopin was switched off meanwhile (since 18.10. 08:00); not started" in err
    assert "`hal start hopin`" in err
    assert "`edge apply` again" not in err
    assert not any(row["State"] == "running" for row in app_world.docker.rows)
    assert not (app_world.state / "deployed").exists()
    assert marks.mark(KEY) is not None


HOSTED_FLEET = f"cloudflare:\n  zone: {ZONE}\n" + FLEET.replace(
    "run: {enabled: true}",
    "run: {enabled: true}\n    hosts: [{name: hopin, exposure: public, reason: a page, ready: /}]",
)


def test_hal_stop_proves_the_barrier_of_the_apps_catalog_hosts_before_it_stops_the_containers(
    app_world, hal
):
    tree = app_world.repository
    domain = app_world.scripted.config["services"]["traefik"]["environment"]["EDGE_DOMAIN"]
    write_fleet(tree, HOSTED_FLEET)
    app_world.docker.put(KEY, "running", "healthy")
    states_when_proven: list[set[str]] = []
    asked: list[str] = []

    def fetch(_address: str, host: str, _seconds: float, _clock) -> barrier.Answer:
        asked.append(host)
        states_when_proven.append({row["State"] for row in app_world.docker.rows})
        return barrier.Answer(404, b"404 page not found\n", KEY)

    hal.verifier = barrier.Verifier(fetch, lambda: 0.0, lambda _seconds: None)

    exit_code, out, _ = hal.stop()

    assert exit_code == 0, out
    assert asked == [f"hopin.{domain}"]
    assert states_when_proven == [{"running"}]
    assert (tree / "dynamic" / f"switch-{KEY}.yml").exists()
    assert {row["State"] for row in app_world.docker.rows} == {"exited"}
    assert switches.Switches(app_world.state).mark(KEY) is not None


def test_a_stop_while_hal_start_waits_at_the_prompt_starts_no_container(app_world, hal):
    deployed(app_world)
    hal.stop()
    marks = switches.Switches(app_world.state)
    app_world.docker.calls.clear()

    exit_code, _, err = hal.start(while_prompt_waits=lambda: marks.record(KEY, NOW))

    assert exit_code == 1
    assert "hopin was switched off meanwhile" in err
    assert "start" not in app_world.docker.verbs
    assert {row["State"] for row in app_world.docker.rows} == {"exited"}
