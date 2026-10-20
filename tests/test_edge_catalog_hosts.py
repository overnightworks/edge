"""The catalog's host shape (#131): one reader, one decision about `run.enabled`."""

import json
from pathlib import Path

import pytest
import yaml

from edge import catalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "external" / "tests" / "fixtures"
ZONE = "edge-fixture.invalid"
PROFILES = {"songmaker": ("songmaker",), "kc": ("kc",)}


def document(*entries: dict, zone: str = ZONE) -> dict:
    """A catalog whose services are `entries`, each `{key: {...}}`."""
    return {"cloudflare": {"zone": zone}, "services": {k: v for e in entries for k, v in e.items()}}


def hosted(*hosts: dict, key: str = "app", **entry) -> dict:
    return {key: {"hosts": list(hosts), **entry}}


def host(name: str = "app", exposure: str = "bypass", **fields) -> dict:
    reasons = {} if exposure == "protected" else {"reason": "own login"}
    return {"name": name, "exposure": exposure, **reasons, **fields}


def refusal(catalog_document: dict) -> str:
    with pytest.raises(catalog.CatalogError) as refused:
        catalog.hosts(catalog_document)
    return str(refused.value)


def as_expectation(found: tuple[catalog.Host, ...]) -> dict:
    return {
        one.fqdn: {
            "service": one.service,
            "exposure": one.exposure.value,
            "reason": one.reason,
            "ready": one.ready,
            "target": one.target,
        }
        for one in found
    }


@pytest.mark.parametrize("fixture", sorted(path.stem for path in FIXTURES.glob("hosts-*.yaml")))
def test_the_reader_yields_what_tofu_is_held_to(fixture):
    """C6: both sides assert the same expectation file."""
    expected = json.loads((FIXTURES / f"{fixture}.expected.json").read_text())
    found = catalog.hosts(catalog.read(FIXTURES / f"{fixture}.yaml"))
    assert as_expectation(found) == expected


def test_a_host_carries_its_service_fqdn_and_all_five_fields():
    found = catalog.hosts(
        document(hosted(host("app", ready="/health", target="http://127.0.0.1:8081")))
    )
    assert found == (
        catalog.Host(
            service="app",
            name="app",
            fqdn=f"app.{ZONE}",
            exposure=catalog.Exposure.BYPASS,
            reason="own login",
            ready="/health",
            target="http://127.0.0.1:8081",
        ),
    )


def test_the_repositorys_catalog_declares_songmakers_two_hosts_on_8080_and_keycloaks_auth():
    found = catalog.hosts(catalog.read(PROJECT_ROOT / catalog.FLEET_FILE))
    assert [(one.fqdn, one.exposure, one.target, one.ready) for one in found] == [
        ("hallucinai.de", catalog.Exposure.PUBLIC, "http://localhost:8080", None),
        ("www.hallucinai.de", catalog.Exposure.PUBLIC, "http://localhost:8080", None),
        (
            "auth.hallucinai.de",
            catalog.Exposure.PUBLIC,
            "http://127.0.0.1:8081",
            "/realms/hallucinai/.well-known/openid-configuration",
        ),
    ]


@pytest.mark.parametrize("unknown", ["port", "ready_path", "record"])
def test_an_unknown_host_key_is_refused_naming_it(unknown):
    message = refusal(document(hosted(host(**{unknown: 1}))))
    assert message == f"services.app.hosts[0].{unknown}: unknown key"


@pytest.mark.parametrize("exposure", [None, "private", 3, ["public"]])
def test_a_missing_or_unknown_exposure_is_refused_listing_the_three(exposure):
    entry = {"name": "app", "reason": "x"} | ({} if exposure is None else {"exposure": exposure})
    message = refusal(document(hosted(entry)))
    assert message.startswith("services.app.hosts[0].exposure:")
    assert "public, bypass, protected" in message


def test_a_host_without_target_is_asked_on_traefik():
    (found,) = catalog.hosts(document(hosted(host())))
    assert found.target == "http://127.0.0.1:8081"


@pytest.mark.parametrize(
    "service,name,target,accepted",
    [
        pytest.param("app", "app", "http://localhost:8080", False, id="not-a-declared-pair"),
        pytest.param("songmaker", "@", "http://localhost:8080", True, id="declared-apex"),
        pytest.param("songmaker", "www", "http://127.0.0.1:8080", True, id="declared-www"),
        pytest.param("songmaker", "www", "http://localhost:8090", False, id="admin-port"),
        pytest.param("app", "app", "http://127.0.0.1:8090", False, id="admin-port-anywhere"),
        pytest.param("songmaker", "@", "http://example.com:8080", False, id="not-loopback"),
        pytest.param("songmaker", "@", "https://localhost:8080", False, id="not-http"),
        pytest.param("songmaker", "@", "http://localhost", False, id="no-port"),
        pytest.param("songmaker", "@", "http://localhost:8080/x", False, id="with-path"),
        pytest.param("songmaker", "@", "http://localhost:8080?x", False, id="with-query"),
        pytest.param("songmaker", "@", "http://me@localhost:8080", False, id="with-user-info"),
        pytest.param("songmaker", "@", "http://localhost:99999", False, id="port-out-of-range"),
        pytest.param("app", "app", "http://127.0.0.1:8081", True, id="the-default-anywhere"),
    ],
)
def test_a_target_is_loopback_http_and_only_gap_35_hosts_may_leave_traefik(
    service, name, target, accepted
):
    catalog_document = document(hosted(host(name, "public", target=target), key=service))
    if accepted:
        (found,) = catalog.hosts(catalog_document)
        assert found.target == target
    else:
        assert refusal(catalog_document).startswith(f"services.{service}.hosts[0].target:")


@pytest.mark.parametrize(
    "ready,expected",
    [("/health", "/health"), (None, None)],
)
def test_ready_is_a_path_or_absent(ready, expected):
    fields = {} if ready is None else {"ready": ready}
    (found,) = catalog.hosts(document(hosted(host(**fields))))
    assert found.ready == expected


@pytest.mark.parametrize(
    "ready", ["health", "", "//x", 7, "/é", "/a b", "/a\tb", "/a\nb", "/a\x7fb"]
)
def test_ready_that_is_no_request_path_is_refused_naming_the_key(ready):
    assert refusal(document(hosted(host(ready=ready)))).startswith("services.app.hosts[0].ready:")


@pytest.mark.parametrize("exposure", ["public", "bypass"])
@pytest.mark.parametrize("reason", [None, "", "  "])
def test_a_public_or_bypass_host_needs_a_reason(exposure, reason):
    entry = {"name": "app", "exposure": exposure} | ({} if reason is None else {"reason": reason})
    assert refusal(document(hosted(entry))).startswith("services.app.hosts[0].reason:")


def test_a_protected_wildcard_needs_no_reason():
    (found,) = catalog.hosts(document(hosted({"name": "*", "exposure": "protected"})))
    assert found.reason is None


@pytest.mark.parametrize(
    "hosts_of_the_catalog,rule",
    [
        pytest.param(
            [host("*", "protected"), host("*", "protected")], "listed twice", id="two-wildcards"
        ),
        pytest.param([host("*", "public")], "wildcard must be protected", id="public-wildcard"),
        pytest.param([host("*", "bypass")], "wildcard must be protected", id="bypass-wildcard"),
        pytest.param([host("app", "protected")], "only the wildcard", id="protected-label"),
        pytest.param(
            [host("*", "protected"), host("app", "public")],
            "own bypass",
            id="public-label-under-wildcard",
        ),
        pytest.param([host("app"), host("app")], "listed twice", id="same-fqdn-twice"),
        pytest.param(
            [host("@", "public"), host("@", "bypass")], "listed twice", id="same-apex-twice"
        ),
    ],
)
def test_the_cross_host_rules_refuse_naming_the_rule(hosts_of_the_catalog, rule):
    assert rule in refusal(document(hosted(*hosts_of_the_catalog)))


@pytest.mark.parametrize(
    "disabled_host,rule",
    [
        pytest.param(host("*", "public"), "wildcard must be protected", id="public-wildcard"),
        pytest.param(host("app", "protected"), "only the wildcard", id="protected-label"),
        pytest.param(host(catalog.PROBE_NAME), "reserved for the host check", id="probe-name"),
    ],
)
def test_the_single_host_rules_hold_for_a_disabled_entry(disabled_host, rule):
    """PB7 (the probe-name row), C-cells for the others."""
    assert rule in refusal(document(hosted(disabled_host, run={"enabled": False})))


def test_the_probe_name_is_reserved_for_the_host_check():
    """PB7: the wildcard is asked there, so no enabled entry may own it."""
    assert catalog.PROBE_NAME == "edge-host-check"
    assert refusal(document(hosted(host(catalog.PROBE_NAME)))) == (
        "services.app.hosts[0].name: reserved for the host check"
    )


def test_the_same_fqdn_across_entries_is_refused():
    assert "listed twice" in refusal(document(hosted(host(), key="a"), hosted(host(), key="b")))


def test_a_public_apex_is_accepted_while_the_wildcard_exists():
    found = catalog.hosts(document(hosted(host("*", "protected"), host("@", "public"))))
    assert [one.fqdn for one in found] == [f"*.{ZONE}", ZONE]


ENTRY_RUN_STATES = [
    pytest.param({}, True, id="no-run"),
    pytest.param({"run": {"enabled": True}}, True, id="enabled"),
    pytest.param({"run": {"enabled": False}}, False, id="disabled"),
]


@pytest.mark.parametrize("run,returned", ENTRY_RUN_STATES)
def test_only_the_hosts_of_enabled_entries_are_returned(run, returned):
    found = catalog.hosts(document(hosted(host(), **run)))
    assert bool(found) is returned


def test_the_hosts_of_a_disabled_entry_are_still_validated():
    broken = hosted(host(port=1), run={"enabled": False})
    assert refusal(document(broken)) == "services.app.hosts[0].port: unknown key"


@pytest.mark.parametrize("enabled", ["no", "false", "off", 0, None])
def test_an_enabled_that_is_no_boolean_is_refused_by_hosts_and_units(enabled, tmp_path):
    entry = hosted(host(), key="songmaker", run={"enabled": enabled})
    message = "services.songmaker.run.enabled: must be true or false"
    catalog_document = document(entry)
    assert refusal(catalog_document) == message
    with pytest.raises(catalog.CatalogError, match="must be true or false"):
        catalog.units(catalog_document, PROFILES, tmp_path)


@pytest.mark.parametrize("written", ["no", "on", "yes", "off", '"false"', "True"])
def test_the_file_is_read_so_that_only_true_and_false_are_booleans(written, tmp_path):
    (tmp_path / catalog.FLEET_FILE).write_text(
        f"services:\n  kc:\n    run:\n      enabled: {written}\n"
    )
    with pytest.raises(catalog.CatalogError, match="must be true or false"):
        catalog.load(tmp_path, PROFILES)


def test_true_and_false_are_read_as_booleans(tmp_path):
    (tmp_path / catalog.FLEET_FILE).write_text("services:\n  kc:\n    run:\n      enabled: true\n")
    assert [unit.enabled for unit in catalog.load(tmp_path, PROFILES)] == [True]


def test_a_broken_hosts_line_does_not_stop_units_or_load(tmp_path):
    broken = {"services": {"kc": {"run": {"enabled": True}, "hosts": [{"name": "kc", "port": 1}]}}}
    (tmp_path / catalog.FLEET_FILE).write_text(yaml.safe_dump(broken))
    assert [unit.key for unit in catalog.units(broken, PROFILES, tmp_path)] == ["kc"]
    assert [unit.key for unit in catalog.load(tmp_path, PROFILES)] == ["kc"]
    assert "hosts[0]" in refusal({"cloudflare": {"zone": ZONE}, **broken})


@pytest.mark.parametrize(
    "listed,message",
    [
        pytest.param("hosts", "services.app.hosts: must be a list", id="hosts-is-no-list"),
        pytest.param(["hosts"], "services.app.hosts[0]: must be a map", id="item-is-no-map"),
    ],
)
def test_a_hosts_value_of_the_wrong_shape_is_refused(listed, message):
    assert refusal(document({"app": {"hosts": listed}})) == message


@pytest.mark.parametrize(
    "name",
    ["", "Hopin", "hopin.edge-fixture.invalid", "a.b", "-x", "a" * 64, 7],
)
def test_a_name_that_is_no_apex_wildcard_or_label_is_refused(name):
    assert refusal(document(hosted(host(name)))).startswith("services.app.hosts[0].name:")


@pytest.mark.parametrize("written", ["1e3", "0o17"])
def test_a_label_that_tofu_reads_as_a_number_is_refused(written, tmp_path):
    (tmp_path / catalog.FLEET_FILE).write_text(
        f"cloudflare:\n  zone: {ZONE}\nservices:\n  app:\n    hosts:\n"
        f"      - {{name: {written}, exposure: bypass, reason: own login}}\n"
    )
    catalog_document = catalog.read(tmp_path / catalog.FLEET_FILE)
    with pytest.raises(catalog.CatalogError, match=r"hosts\[0\]\.name:"):
        catalog.hosts(catalog_document)


@pytest.mark.parametrize("name", ["hopin", "a", "a" * 63, "a-b", "9"])
def test_a_lowercase_label_is_accepted(name):
    (found,) = catalog.hosts(document(hosted(host(name))))
    assert found.fqdn == f"{name}.{ZONE}"


@pytest.mark.parametrize("zone", ["Edge-Fixture.invalid", "edge-fixture.invalid.", None, "", "a b"])
def test_a_zone_with_a_capital_or_a_trailing_dot_is_refused(zone):
    assert refusal(document(hosted(host()), zone=zone)).startswith("cloudflare.zone:")


def test_the_zone_is_readable_alone_for_the_other_readers_of_the_catalog():
    assert catalog.zone(document()) == ZONE


@pytest.mark.parametrize("broken", [None, [], "zone", {"cloudflare": "zone"}])
def test_a_catalog_without_a_cloudflare_map_has_no_zone(broken):
    with pytest.raises(catalog.CatalogError, match=r"^cloudflare\.zone:"):
        catalog.zone(broken)
