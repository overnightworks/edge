"""Keycloak as a catalog unit (#64): the repository's real fleet.yaml and compose.yaml."""

import copy
import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

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


def test_keycloaks_gzip_cache_is_a_tmpfs_owned_by_its_user_so_a_restart_empties_it():
    """A bare tmpfs comes back root-owned after a restart and Keycloak then answers 404 for gzip."""
    tmpfs = compose_document()["services"]["keycloak"]["tmpfs"]
    target, _, options = tmpfs[0].partition(":")
    options = options.split(",")

    assert (target, "uid=1000" in options, "mode=0700" in options) == (
        "/opt/keycloak/data/tmp",
        True,
        True,
    )


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


@pytest.mark.parametrize("app", ["songmaker", "hopin"])
def test_an_app_back_channel_is_an_internal_isolated_network_only_keycloak_joins(app):
    """#293, #254: the app joins the fixed name as an external network; only keycloak is on it."""
    document = compose_document()
    network = document["networks"][f"backchannel-{app}"]
    members = [
        name
        for name, service in document["services"].items()
        if f"backchannel-{app}" in service.get("networks", [])
    ]

    assert network["name"] == f"hal_backchannel_{app}"
    assert network["internal"] is True
    assert network["driver_opts"] == document["x-isolated-bridge"]
    assert members == ["keycloak"]


def test_no_service_but_keycloak_joins_any_back_channel_network():
    """#382 cell 2 (#254 invariant): Keycloak is the only edge member of a back-channel network."""
    document = compose_document()
    back_channels = {name for name in document["networks"] if name.startswith("backchannel-")}

    joiners = {
        name
        for name, service in document["services"].items()
        if back_channels & set(service.get("networks", []))
    }

    assert joiners == {"keycloak"}


def test_keycloak_trusts_forwarded_from_its_own_network_only():
    """#382 cell 3: Keycloak trusts forwarded headers from its own network only."""
    document = compose_document()
    keycloak = document["services"]["keycloak"]

    assert (
        keycloak["environment"]["KC_PROXY_TRUSTED_ADDRESSES"]
        == document["networks"]["keycloak"]["ipam"]["config"][0]["subnet"]
    )


def test_the_probe_overlay_renames_every_fixed_back_channel_network_keycloak_joins():
    """#382: a name left fixed would make the probe create the live `hal_backchannel_*` network."""
    document = compose_document()
    overlay = yaml.safe_load((REPOSITORY / "probe/keycloak/compose.probe.yaml").read_text())
    joined = [
        name
        for name in document["services"]["keycloak"]["networks"]
        if name.startswith("backchannel-")
    ]

    assert joined
    for name in joined:
        app = name.removeprefix("backchannel-")
        assert overlay["networks"][name]["name"] == f"${{COMPOSE_PROJECT_NAME}}_backchannel_{app}"


def test_traefik_reaches_keycloak_through_the_keycloak_network_whatever_the_project_is_called():
    """#293: with a second network on the service, Traefik would otherwise pick either address."""
    labels = compose_document()["services"]["keycloak"]["labels"]

    assert labels["traefik.docker.network"] == "${COMPOSE_PROJECT_NAME}_keycloak"


def realm_clients() -> dict[str, dict]:
    realm = yaml.safe_load((REPOSITORY / "keycloak/realm.yaml").read_text())
    return {client["clientId"]: client for client in realm["clients"]}


@pytest.mark.parametrize("app", ["songmaker", "hopin"])
def test_an_app_is_logged_out_by_a_back_channel_call_that_needs_no_session_id(app):
    """#293: webauth ends every session of the subject and ignores `sid`; hopin has the shape."""
    attributes = realm_clients()[app]["attributes"]

    assert (
        attributes["backchannel.logout.url"]
        == f"http://{app}.hallucinai.de:8080/auth/backchannel-logout"
    )
    assert attributes["backchannel.logout.session.required"] == "false"


def test_only_songmaker_and_hopin_have_a_back_channel_logout_address():
    """#382 cell 6: only songmaker and HopIn have a back-channel logout address."""
    with_address = [
        name
        for name, client in realm_clients().items()
        if "backchannel.logout.url" in client.get("attributes", {})
    ]

    assert sorted(with_address) == ["hopin", "songmaker"]


def test_only_hopin_gets_no_refresh_token_because_it_uses_none():
    """O1 of #254 (24.10.2026): an app gets a refresh token only if it uses one."""
    clients = realm_clients()

    assert clients["hopin"]["attributes"]["use.refresh.tokens"] == "false"
    assert "use.refresh.tokens" not in clients["songmaker"]["attributes"]
    assert "use.refresh.tokens" not in clients["presentator"].get("attributes", {})


def test_only_songmaker_allows_the_device_grant():
    """#297: the live smoke signs in by device grant; a stranger needs songmaker's secret."""
    realm = yaml.safe_load((REPOSITORY / "keycloak/realm.yaml").read_text())

    allowing = [
        client["clientId"]
        for client in realm["clients"]
        if client.get("attributes", {}).get("oauth2.device.authorization.grant.enabled") == "true"
    ]

    assert allowing == ["songmaker"]


@pytest.mark.parametrize(
    ("client_id", "answers"),
    [
        ("songmaker", {"with": 200, "without": 401}),
        ("hopin", {"with": 400, "without": 400}),
    ],
)
def test_the_device_grant_cell_accepts_only_songmaker_with_its_secret(
    monkeypatch, client_id, answers
):
    """#297: the cell reports a held cell for the ruled answers and no other."""
    monkeypatch.syspath_prepend(str(REPOSITORY / "probe" / "keycloak"))
    import device_grant_cell

    def answer(status):
        body = {"device_code": "d", "user_code": "u"} if status == 200 else {"error": "x"}
        return SimpleNamespace(status=status, text=json.dumps(body), json=lambda: body)

    class FakeProbe:
        def __init__(self, status_for):
            self.status_for = status_for
            self.held_lines = []

        def through_tunnel(self, _method, _path, form):
            return answer(self.status_for(form.get("client_secret")))

        def held(self, proof, detail):
            self.held_lines.append((proof, detail))

    def expecting(failures):
        return lambda held, message: held or failures.append(message)

    failures = []
    ruled = FakeProbe(lambda secret: answers["with" if secret == "s3" else "without"])
    device_grant_cell.prove(ruled, client_id, "s3", expecting(failures))
    assert failures == []
    assert [proof for proof, _ in ruled.held_lines] == ["DG1"]

    device_grant_cell.prove(FakeProbe(lambda _secret: 200), client_id, "s3", expecting(failures))
    assert failures


def test_the_device_grant_cell_names_no_part_of_an_answer_that_lacks_the_user_code(monkeypatch):
    """#297: a device_code in a malformed answer must not reach the probe's output."""
    monkeypatch.syspath_prepend(str(REPOSITORY / "probe" / "keycloak"))
    import device_grant_cell

    body = {"device_code": "SECRET-DEVICE-CODE"}
    started = SimpleNamespace(status=200, text=json.dumps(body), json=lambda: body)

    class FakeProbe:
        def through_tunnel(self, _method, _path, form):
            return started

        def held(self, proof, detail):
            pass

    messages = []
    device_grant_cell.prove(
        FakeProbe(), "songmaker", "s3", lambda held, message: held or messages.append(message)
    )

    assert (
        messages[0]
        == "a device request for songmaker with its secret answered 200 without user_code"
    )
    assert "SECRET-DEVICE-CODE" not in "".join(messages)


def test_the_back_channel_cell_keeps_the_client_secret_out_of_the_exec_arguments(monkeypatch):
    """#293: process arguments are visible to the host; the secret goes through stdin."""
    monkeypatch.syspath_prepend(str(REPOSITORY / "probe" / "keycloak"))
    import backchannel_cell

    secret = "probe-client-secret-value"
    calls = []

    class FakeStack:
        def compose(self, *arguments, stdin=None):
            calls.append((arguments, stdin))
            seen = {"token": "400", "address": "10.0.0.2", "admin": "404", "management": "-"}
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(seen))

    class FakeProbe:
        stack = FakeStack()

        def keep_secret(self, _name, value):
            return value

    class FakeAdmin:
        def create_user(self, *_arguments):
            return "user-id"

        def get(self, _path):
            return [{"type": "LOGIN_ERROR", "ipAddress": "10.0.0.2"}]

    backchannel_cell.prove(FakeProbe(), FakeAdmin(), secret, lambda _held, _message: None)

    ((arguments, stdin),) = calls
    assert secret not in " ".join(arguments)
    assert json.loads(stdin)["client_secret"] == secret


# An SVG's xmlns names a namespace; no browser fetches it.
XML_NAMESPACE_HOST = "www.w3.org"


def test_the_theme_sources_name_no_host_but_hallucinai_de():
    """#205: a source check of the theme, not proof of browser requests; the driven page is that."""
    theme = REPOSITORY / "keycloak/themes/hallucinai"
    texts = [
        path.read_text()
        for path in theme.rglob("*")
        if path.suffix in {".css", ".ftl", ".js", ".json", ".properties", ".svg"}
    ]
    hosts = {host for text in texts for host in re.findall(r"(?:https?:)?//([^/\"'\s)]+)", text)}

    assert hosts <= {"hallucinai.de", XML_NAMESPACE_HOST}


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


HOPIN_RULED = {
    "backchannel.logout.url": "http://hopin.hallucinai.de:8080/auth/backchannel-logout",
    "backchannel.logout.session.required": "false",
    "use.refresh.tokens": "false",
}
SONGMAKER_RULED = {
    "backchannel.logout.url": "http://songmaker.hallucinai.de:8080/auth/backchannel-logout",
    "backchannel.logout.session.required": "false",
}


@pytest.mark.parametrize(
    ("client_id", "attributes", "wrong"),
    [
        pytest.param("hopin", HOPIN_RULED, set(), id="hopin as ruled"),
        pytest.param(
            "hopin",
            HOPIN_RULED | {"backchannel.logout.url": "http://x"},
            {"backchannel.logout.url"},
            id="hopin wrong url",
        ),
        pytest.param("hopin", {}, set(HOPIN_RULED), id="hopin missing"),
        pytest.param(
            "hopin",
            HOPIN_RULED | {"backchannel.logout.session.required": "true"},
            {"backchannel.logout.session.required"},
            id="hopin session required",
        ),
        pytest.param(
            "hopin",
            HOPIN_RULED | {"use.refresh.tokens": "true"},
            {"use.refresh.tokens"},
            id="hopin refresh tokens",
        ),
        pytest.param("songmaker", SONGMAKER_RULED, set(), id="songmaker as ruled"),
        pytest.param(
            "songmaker",
            SONGMAKER_RULED | {"use.refresh.tokens": "false"},
            {"use.refresh.tokens"},
            id="songmaker with the refresh line",
        ),
        pytest.param("presentator", {}, set(), id="presentator without a url"),
        pytest.param(
            "presentator",
            {"backchannel.logout.url": "http://x"},
            {"backchannel.logout.url"},
            id="presentator with a url",
        ),
    ],
)
def test_wrong_logout_settings_names_what_differs_from_the_ruling(
    monkeypatch, client_id, attributes, wrong
):
    """#382 cell 8: a wrong logout setting names what differs from the ruling."""
    monkeypatch.syspath_prepend(str(REPOSITORY / "probe" / "keycloak"))
    import backchannel_cell

    assert set(backchannel_cell.wrong_logout_settings(client_id, attributes)) == wrong
