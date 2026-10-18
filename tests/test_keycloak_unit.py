"""Keycloak as a catalog unit (#64): the repository's real fleet.yaml and compose.yaml."""

import dataclasses
from pathlib import Path

import pytest
import yaml

from edge import apply, catalog, compose

REPOSITORY = Path(__file__).resolve().parents[1]
KEYCLOAK_SERVICES = ("keycloak-db", "keycloak")


def compose_document() -> dict:
    return yaml.safe_load((REPOSITORY / "compose.yaml").read_text())


def resolved_like_compose(document: dict) -> dict:
    """compose.yaml with short volumes and depends_on spelled out, like `docker compose config`."""
    services = {}
    for name, service in document["services"].items():
        mounts = [
            {"type": "volume", "source": mount.split(":")[0]}
            for mount in service.get("volumes", [])
            if isinstance(mount, str) and mount.split(":")[0] in document.get("volumes", {})
        ]
        services[name] = {**service, "volumes": mounts}
        if isinstance(service.get("depends_on"), list):
            services[name]["depends_on"] = {
                dependency: {"condition": "service_started", "required": True}
                for dependency in service["depends_on"]
            }
    return {"services": services}


@pytest.fixture(scope="module")
def keycloak_unit() -> catalog.Unit:
    profiles = compose.profile_services(compose_document())
    units = catalog.load(REPOSITORY, profiles)
    return next(unit for unit in units if unit.key == "keycloak")


@pytest.fixture
def keycloak_unit_off(keycloak_unit) -> catalog.Unit:
    return dataclasses.replace(keycloak_unit, enabled=False)


def test_the_catalog_switches_keycloak_on_with_go_live_as_its_setup(keycloak_unit):
    """#118 rule 1: the real catalog lists keycloak as on, with go-live as its setup."""
    assert (keycloak_unit.enabled, keycloak_unit.setup) == (True, "keycloak/go-live")


def test_the_keycloak_profile_is_its_database_and_keycloak_and_not_the_config_cli():
    """#64 rule 1: the keycloak profile resolves to its database and Keycloak, not the CLI."""
    profiles = compose.profile_services(compose_document())

    assert profiles["keycloak"] == KEYCLOAK_SERVICES


def test_switching_keycloak_off_names_its_database_volume_as_staying(keycloak_unit_off):
    """#64 rule 4: switching Keycloak off stops both services and keeps its database volume."""
    document = resolved_like_compose(compose_document())
    profiles = compose.profile_services(document)
    running = {name: {"running"} for name in KEYCLOAK_SERVICES}

    plans = apply.plan_switches([keycloak_unit_off], profiles, document, running, {})

    assert [(plan.action, plan.services, plan.volumes) for plan in plans] == [
        (apply.Action.OFF, KEYCLOAK_SERVICES, ("keycloak-db",))
    ]


def test_keycloak_that_is_off_and_gone_needs_no_change(keycloak_unit_off):
    """#64 rules 1 and 5: Keycloak that is off and not running needs no change."""
    document = resolved_like_compose(compose_document())
    profiles = compose.profile_services(document)

    assert apply.plan_switches([keycloak_unit_off], profiles, document, {}, {}) == []


@pytest.mark.parametrize(
    ("realm_setting", "area", "parent"),
    [("loginTheme", "login", "keycloak.v2"), ("accountTheme", "account", "keycloak.v3")],
)
def test_each_realm_theme_is_mounted_read_only_and_names_its_parent(realm_setting, area, parent):
    """#105 rule 1, cell T2: a realm theme is mounted on keycloak only and names its parent."""
    realm = yaml.safe_load((REPOSITORY / "keycloak" / "realm.yaml").read_text())
    theme = realm[realm_setting]
    document = compose_document()
    mount = f"./keycloak/themes/{theme}:/opt/keycloak/themes/{theme}:ro"
    mounted_on = [
        name
        for name, service in document["services"].items()
        if mount in service.get("volumes", [])
    ]
    properties = REPOSITORY / "keycloak" / "themes" / theme / area / "theme.properties"

    assert (mounted_on, properties.read_text().strip()) == (["keycloak"], f"parent={parent}")
