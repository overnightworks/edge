"""traefik-mcp: compose.yaml, traefik-mcp.yml, mcp/dynamic/ and ca.json as files, no containers.

The running TLS path is proven by probe/step-ca/probe.py; these tests pin the facts the probe
cannot see in one run: the three-network split, the one names list, and the lifetime the resolver
assumes of the CA.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MCP_SUFFIX = ".mcp.internal"
NETWORKS = {"mcp-ca", "mcp", "mcp-backends"}
CA_DEFAULT_LIFETIME_HOURS = 24  # step-ca's default; ca.json sets no claims


def load(path: str):
    return yaml.safe_load((ROOT / path).read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return load("compose.yaml")


@pytest.fixture(scope="module")
def traefik_mcp(compose) -> dict:
    return compose["services"]["traefik-mcp"]


@pytest.fixture(scope="module")
def static_config() -> dict:
    return load("traefik-mcp.yml")


@pytest.fixture(scope="module")
def routers() -> dict:
    merged = {}
    for file in sorted((ROOT / "mcp" / "dynamic").glob("*.yml")):
        merged.update(yaml.safe_load(file.read_text())["http"]["routers"])
    return merged


def test_the_three_mcp_networks_are_internal(compose):
    assert {name: compose["networks"][name].get("internal") for name in NETWORKS} == {
        name: True for name in NETWORKS
    }


def test_traefik_mcp_is_the_only_service_on_all_three_networks(compose):
    on_all = [
        name
        for name, service in compose["services"].items()
        if set(service.get("networks") or []) >= NETWORKS
    ]

    assert on_all == ["traefik-mcp"]


def test_traefik_mcp_answers_to_the_same_names_on_mcp_ca_and_mcp(traefik_mcp):
    aliases = {
        network: traefik_mcp["networks"][network]["aliases"] for network in ("mcp-ca", "mcp")
    }

    assert aliases["mcp-ca"] == aliases["mcp"]
    assert "aliases" not in traefik_mcp["networks"]["mcp-backends"]


def test_every_alias_is_a_router_host_inside_the_names_the_ca_allows(traefik_mcp, routers):
    ca_policy = load("step-ca/ca.json")["authority"]["policy"]["x509"]["allow"]["dns"]
    aliases = set(traefik_mcp["networks"]["mcp"]["aliases"])
    router_hosts = {
        router["rule"].removeprefix("Host(`").removesuffix("`)") for router in routers.values()
    }

    assert ca_policy == ["*" + MCP_SUFFIX]
    assert all(alias.endswith(MCP_SUFFIX) for alias in aliases)
    assert router_hosts == aliases


def test_every_router_asks_the_internal_resolver_on_the_mcp_entrypoint(routers, static_config):
    entrypoint = next(iter(static_config["entryPoints"]))

    assert routers
    for router in routers.values():
        assert router["tls"] == {"certResolver": "internal"}
        assert router["entryPoints"] == [entrypoint]


def test_traefik_mcp_shares_the_public_traefiks_image_and_hardening(compose, traefik_mcp):
    shared = ("image", "user", "read_only", "cap_drop", "security_opt")
    public = compose["services"]["traefik"]

    assert {key: traefik_mcp[key] for key in shared} == {key: public[key] for key in shared}


def test_traefik_mcp_cannot_reach_the_docker_api_or_the_host(traefik_mcp, static_config):
    assert "network_mode" not in traefik_mcp
    assert not any(
        "docker.sock" in volume or "socket-proxy" in volume for volume in traefik_mcp["volumes"]
    )
    assert "socket-proxy" not in traefik_mcp.get("depends_on", {})
    assert set(static_config["providers"]) == {"file"}


def test_traefik_mcp_binds_443_without_a_capability(traefik_mcp):
    assert traefik_mcp["cap_drop"] == ["ALL"]
    assert "cap_add" not in traefik_mcp
    assert traefik_mcp["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": 0}


def test_the_static_configuration_has_one_tls_entrypoint_on_443_and_no_management_api(
    static_config,
):
    assert [e["address"] for e in static_config["entryPoints"].values()] == [":443"]
    assert next(iter(static_config["entryPoints"].values()))["http"]["tls"] == {}
    assert not {"api", "ping", "metrics"} & set(static_config)


def test_the_file_provider_reads_a_directory_of_its_own(traefik_mcp, static_config):
    directory = static_config["providers"]["file"]["directory"]

    assert f"./mcp/dynamic:{directory}:ro" in traefik_mcp["volumes"]
    assert directory != "/dynamic"


def test_the_internal_resolver_trusts_only_the_cas_root_over_tls_alpn_01(compose, static_config):
    acme = static_config["certificatesResolvers"]["internal"]["acme"]
    ca_server_host = acme["caServer"].split("/")[2]

    assert ca_server_host == f"step-ca:{load('step-ca/ca.json')['address'].lstrip(':')}"
    assert acme["caSystemCertPool"] is False
    assert acme["caCertificates"] == ["/run/secrets/step_ca_root_certificate"]
    assert acme["tlsChallenge"] == {}
    assert not {"httpChallenge", "dnsChallenge"} & set(acme)
    assert compose["secrets"]["step_ca_root_certificate"]["file"].endswith(
        "/.secrets/step-ca/root_ca.crt"
    )


def test_the_resolver_assumes_the_24_hour_lifetime_the_ca_keeps_by_default(static_config):
    acme = static_config["certificatesResolvers"]["internal"]["acme"]
    ca = load("step-ca/ca.json")

    assert acme["certificatesDuration"] == CA_DEFAULT_LIFETIME_HOURS
    assert "claims" not in ca["authority"]
    assert all("claims" not in provisioner for provisioner in ca["authority"]["provisioners"])


def test_the_acme_storage_is_a_named_volume_that_traefik_mcp_can_write(
    compose, traefik_mcp, static_config
):
    storage = static_config["certificatesResolvers"]["internal"]["acme"]["storage"]
    owner = compose["services"]["traefik-mcp-acme-owner"]

    assert f"traefik-mcp-acme:{Path(storage).parent}" in traefik_mcp["volumes"]
    assert "traefik-mcp-acme" in compose["volumes"]
    assert owner["entrypoint"] == ["chown", "65534:65534", str(Path(storage).parent)]
    assert owner["network_mode"] == "none"
    assert traefik_mcp["depends_on"]["traefik-mcp-acme-owner"] == {
        "condition": "service_completed_successfully"
    }


def test_the_naming_table_owns_the_mcp_origin_name():
    naming = (ROOT / "docs" / "naming.md").read_text()

    assert "`<stem>.mcp.internal`" in naming
