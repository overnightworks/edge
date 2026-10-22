"""`edge apply --closed` with apps (#255 P1, A7, A10, A20, A22): one plan and one `yes`, the app
step after the units and the realm, on the scripted boundaries of `test_edge_apply` and the real
origin and scripted Docker of `app_support`."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from app_support import FLEET, KEY, ScriptedDocker, origin_at, runtime_of
from test_edge_apply import (
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

from edge import checkout

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
    assert app_world.docker.verbs == []
