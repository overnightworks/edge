"""Keycloak as a catalog unit (#64): the repository's real fleet.yaml and compose.yaml."""

import copy
import dataclasses
import re
from pathlib import Path

import pytest
import yaml

from edge import apply, catalog, compose

REPOSITORY = Path(__file__).resolve().parents[1]
KEYCLOAK_SERVICES = ("keycloak-db", "keycloak")
CLIENT_SECRET_REFERENCE = re.compile(r"\$\(env:(\w+)\)")


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


def client_secret_problems(realm: dict, compose_file: dict, overlay: dict) -> list[str]:
    """What is wrong between a client secret in realm.yaml and the compose secret feeding it."""
    mounted = {
        secret["target"]: secret["source"]
        for secret in compose_file["services"]["keycloak-config-cli"]["secrets"]
        if isinstance(secret, dict)
    }
    problems = []
    for client in realm["clients"]:
        reference = CLIENT_SECRET_REFERENCE.fullmatch(client.get("secret", ""))
        if reference is None:
            continue
        variable, name = reference.group(1), client["clientId"]
        source = mounted.get(variable)
        expected_file = f"${{HOME}}/.secrets/keycloak/clients/{name}"
        if source is None:
            problems.append(f"{name}: keycloak-config-cli has no secret with target {variable}")
        elif compose_file["secrets"][source]["file"] != expected_file:
            problems.append(f"{name}: secret {source} is not the file {expected_file}")
        elif source not in overlay["secrets"]:
            problems.append(f"{name}: the probe overlay does not override secret {source}")
        elif overlay["secrets"][source].get("file") != f"${{PROBE_SECRETS_DIR}}/clients/{name}":
            problems.append(f"{name}: the probe overlay does not mount its scratch file")
    return problems


def drop_mount(realm: dict, compose_file: dict, overlay: dict) -> None:
    secrets = compose_file["services"]["keycloak-config-cli"]["secrets"]
    secrets.remove(next(s for s in secrets if s["target"] == "SONGMAKER_CLIENT_SECRET"))


def drop_override(realm: dict, compose_file: dict, overlay: dict) -> None:
    del overlay["secrets"]["keycloak_client_songmaker"]


def override_with_production_path(realm: dict, compose_file: dict, overlay: dict) -> None:
    overlay["secrets"]["keycloak_client_songmaker"]["file"] = (
        "${HOME}/.secrets/keycloak/clients/songmaker"
    )


def override_without_file(realm: dict, compose_file: dict, overlay: dict) -> None:
    overlay["secrets"]["keycloak_client_songmaker"] = {}


def move_file(realm: dict, compose_file: dict, overlay: dict) -> None:
    compose_file["secrets"]["keycloak_client_songmaker"]["file"] = "${HOME}/.secrets/elsewhere"


def test_every_client_secret_in_realm_yaml_has_a_mount_a_file_and_a_probe_override():
    """#108 T11 (rule 5): a client secret reaches keycloak-config-cli from its own file, and
    the probe never mounts the operator's file."""
    realm = yaml.safe_load((REPOSITORY / "keycloak/realm.yaml").read_text())
    overlay = yaml.safe_load((REPOSITORY / "probe/keycloak/compose.probe.yaml").read_text())

    assert client_secret_problems(realm, compose_document(), overlay) == []
    assert any(CLIENT_SECRET_REFERENCE.fullmatch(c.get("secret", "")) for c in realm["clients"])


@pytest.mark.parametrize(
    "break_wiring",
    [drop_mount, drop_override, override_with_production_path, override_without_file, move_file],
)
def test_a_client_secret_without_its_mount_file_or_probe_override_is_named(break_wiring):
    """#108 T11 (rule 5): the cross-check goes red when a client secret's wiring is incomplete."""
    realm = yaml.safe_load((REPOSITORY / "keycloak/realm.yaml").read_text())
    overlay = yaml.safe_load((REPOSITORY / "probe/keycloak/compose.probe.yaml").read_text())
    compose_file = copy.deepcopy(compose_document())
    break_wiring(realm, compose_file, overlay)

    assert len(client_secret_problems(realm, compose_file, overlay)) == 1


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
