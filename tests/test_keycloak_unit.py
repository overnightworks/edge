"""Keycloak as a catalog unit (#64): the repository's real fleet.yaml and compose.yaml."""

from pathlib import Path

import pytest
import yaml

from edge import apply, catalog, compose

REPOSITORY = Path(__file__).resolve().parents[1]
KEYCLOAK_SERVICES = ("keycloak-db", "keycloak")


def compose_document() -> dict:
    return yaml.safe_load((REPOSITORY / "compose.yaml").read_text())


def resolved_like_compose(document: dict) -> dict:
    """compose.yaml with the short volume syntax spelled out, as `docker compose config` does."""
    services = {}
    for name, service in document["services"].items():
        mounts = [
            {"type": "volume", "source": mount.split(":")[0]}
            for mount in service.get("volumes", [])
            if isinstance(mount, str) and mount.split(":")[0] in document.get("volumes", {})
        ]
        services[name] = {**service, "volumes": mounts}
    return {"services": services}


@pytest.fixture(scope="module")
def keycloak_unit() -> catalog.Unit:
    profiles = compose.profile_services(compose_document())
    units = catalog.load(REPOSITORY, profiles)
    return next(unit for unit in units if unit.key == "keycloak")


def test_the_catalog_ships_keycloak_off_with_go_live_as_its_setup(keycloak_unit):
    assert (keycloak_unit.enabled, keycloak_unit.setup) == (False, "keycloak/go-live")


def test_the_keycloak_profile_is_its_database_and_keycloak_and_not_the_config_cli():
    profiles = compose.profile_services(compose_document())

    assert profiles["keycloak"] == KEYCLOAK_SERVICES


def test_switching_keycloak_off_names_its_database_volume_as_staying(keycloak_unit):
    document = resolved_like_compose(compose_document())
    profiles = compose.profile_services(document)
    running = {name: {"running"} for name in KEYCLOAK_SERVICES}

    plans = apply.plan_switches([keycloak_unit], profiles, document, running, {})

    assert [(plan.action, plan.services, plan.volumes) for plan in plans] == [
        (apply.Action.OFF, KEYCLOAK_SERVICES, ("keycloak-db",))
    ]


def test_keycloak_that_is_off_and_gone_needs_no_change(keycloak_unit):
    document = resolved_like_compose(compose_document())
    profiles = compose.profile_services(document)

    assert apply.plan_switches([keycloak_unit], profiles, document, {}, {}) == []
