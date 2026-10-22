"""Keycloak as a catalog unit (#64): the repository's real fleet.yaml and compose.yaml."""

import copy
import dataclasses
import json
import re
from pathlib import Path

import pytest
import yaml

from edge import catalog, compose, unit_plans

REPOSITORY = Path(__file__).resolve().parents[1]
KEYCLOAK_SERVICES = ("keycloak-db", "keycloak")
CLIENT_SECRET_REFERENCE = re.compile(r"\$\(env:(\w+)\)")


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


@pytest.fixture
def keycloak_unit_off(keycloak_unit) -> catalog.Unit:
    return dataclasses.replace(keycloak_unit, enabled=False)


def test_the_catalog_ships_keycloak_on_with_go_live_as_its_setup(keycloak_unit):
    """#118 rule 2: the go-live switches Keycloak on; go-live stays its setup."""
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

    plans = unit_plans.plan_switches([keycloak_unit_off], profiles, document, running, {})

    assert [(plan.action, plan.services, plan.volumes) for plan in plans] == [
        (unit_plans.Action.OFF, KEYCLOAK_SERVICES, ("keycloak-db",))
    ]


def test_keycloak_that_is_off_and_gone_needs_no_change(keycloak_unit_off):
    """#64 rules 1 and 5: Keycloak that is off and not running needs no change."""
    document = resolved_like_compose(compose_document())
    profiles = compose.profile_services(document)

    assert unit_plans.plan_switches([keycloak_unit_off], profiles, document, {}, {}) == []


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

    assert (mounted_on, theme_properties(properties)["parent"]) == (["keycloak"], parent)


def theme_properties(path: Path) -> dict[str, str]:
    lines = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    return dict(line.split("=", 1) for line in lines)


def test_the_account_console_offers_no_groups_page():
    """#205 rule 4: the hallucinai account theme's navigation has no groups entry."""
    content = REPOSITORY / "keycloak/themes/hallucinai/account/resources/content.json"
    labels = [entry["label"] for entry in json.loads(content.read_text())]

    assert "groups" not in labels
    assert "applications" in labels


def test_the_apps_are_listed_in_the_account_console_only_for_people_who_used_them():
    """#205 rule 5: no app client is shown to everyone; Keycloak lists the apps a person used."""
    realm = yaml.safe_load((REPOSITORY / "keycloak/realm.yaml").read_text())
    apps = [client for client in realm["clients"] if client.get("standardFlowEnabled")]

    assert apps
    assert [client["clientId"] for client in apps if client["alwaysDisplayInConsole"]] == []


def test_the_theme_files_reach_no_host_but_hallucinai_de():
    """#205: fonts and favicon come from the theme itself, so a page asks no third party."""
    theme = REPOSITORY / "keycloak/themes/hallucinai"
    texts = [
        path.read_text()
        for path in theme.rglob("*")
        if path.suffix in {".css", ".ftl", ".js", ".properties"}
    ]
    hosts = {host for text in texts for host in re.findall(r"https?://([^/\"'\s)]+)", text)}

    assert hosts <= {"hallucinai.de"}


def test_the_bare_auth_address_redirects_to_the_account_page_and_nothing_else_opens():
    """One backend-less router for exactly `/` on the public entrypoint; other auth rules stay."""
    labels = compose_document()["services"]["keycloak"]["labels"]
    redirect = "traefik.http.middlewares.auth-root-to-account.redirectregex"

    assert {
        "auth-root rule": labels["traefik.http.routers.auth-root.rule"],
        "auth-root entrypoint": labels["traefik.http.routers.auth-root.entrypoints"],
        "auth-root middleware": labels["traefik.http.routers.auth-root.middlewares"],
        "auth-root service": labels["traefik.http.routers.auth-root.service"],
        "redirect regex": labels[f"{redirect}.regex"],
        "redirect target": labels[f"{redirect}.replacement"],
        "redirect is temporary": labels[f"{redirect}.permanent"],
        "auth rule": labels["traefik.http.routers.auth.rule"].replace("\n", " "),
        "auth-admin rule": labels["traefik.http.routers.auth-admin.rule"],
    } == {
        "auth-root rule": "Host(`auth.${EDGE_DOMAIN}`) && Path(`/`)",
        "auth-root entrypoint": "edge",
        "auth-root middleware": "auth-root-to-account",
        "auth-root service": "noop@internal",
        "redirect regex": "^.*$$",
        "redirect target": "https://auth.${EDGE_DOMAIN}/realms/hallucinai/account",
        "redirect is temporary": "false",
        "auth rule": (
            "Host(`auth.${EDGE_DOMAIN}`) && (PathPrefix(`/realms/hallucinai/`) "
            "|| PathPrefix(`/resources/hallucinai/`) || PathRegexp(`^/resources/[a-z0-9]{5}/`))"
        ),
        "auth-admin rule": "Host(`localhost`)",
    }
