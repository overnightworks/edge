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


def test_the_ca_the_gateway_and_its_helper_start_only_with_the_step_ca_profile(compose):
    """#48 rule 2: a plain `docker compose up -d` starts none of them."""
    services = ("step-ca", "traefik-mcp", "traefik-mcp-acme-owner")

    assert {name: compose["services"][name].get("profiles") for name in services} == {
        name: ["step-ca"] for name in services
    }


def test_traefik_mcp_starts_traefik_only_once_the_ca_listens(traefik_mcp, static_config):
    """#48 rule 4 (defect: no certificate after a late CA): the wait is in the entrypoint."""
    ca_host, ca_port = (
        static_config["certificatesResolvers"]["internal"]["acme"]["caServer"]
        .split("/")[2]
        .split(":")
    )

    assert traefik_mcp["entrypoint"][:2] == ["sh", "-c"]
    assert traefik_mcp["entrypoint"][2].startswith(f"until nc -z {ca_host} {ca_port};")
    assert traefik_mcp["entrypoint"][2].endswith("; exec traefik")


def test_the_three_mcp_networks_are_internal(compose):
    """#48 rule 1: the three MCP networks have no route out."""

    assert {name: compose["networks"][name].get("internal") for name in NETWORKS} == {
        name: True for name in NETWORKS
    }


def test_traefik_mcp_is_the_only_service_on_all_three_networks(compose):
    """#48 rule 1: only traefik-mcp bridges the three networks."""

    on_all = [
        name
        for name, service in compose["services"].items()
        if set(service.get("networks") or []) >= NETWORKS
    ]

    assert on_all == ["traefik-mcp"]


def test_traefik_mcp_answers_to_the_same_names_on_mcp_ca_and_mcp(traefik_mcp):
    """#48 rule 1: one alias list serves mcp-ca and mcp, none on mcp-backends."""

    aliases = {
        network: traefik_mcp["networks"][network]["aliases"] for network in ("mcp-ca", "mcp")
    }

    assert aliases["mcp-ca"] == aliases["mcp"]
    assert "aliases" not in traefik_mcp["networks"]["mcp-backends"]


def test_every_alias_is_a_router_host_inside_the_names_the_ca_allows(traefik_mcp, routers):
    """#48 rule 5: aliases, router hosts and the CA policy name the same origins."""

    ca_policy = load("step-ca/ca.json")["authority"]["policy"]["x509"]["allow"]["dns"]
    aliases = set(traefik_mcp["networks"]["mcp"]["aliases"])
    router_hosts = {
        router["rule"].removeprefix("Host(`").removesuffix("`)") for router in routers.values()
    }

    assert ca_policy == ["*" + MCP_SUFFIX]
    assert all(alias.endswith(MCP_SUFFIX) for alias in aliases)
    assert router_hosts == aliases


def test_every_router_asks_the_internal_resolver_on_the_mcp_entrypoint(routers, static_config):
    """#48 rule 5: each router takes its certificate from the internal resolver."""

    entrypoint = next(iter(static_config["entryPoints"]))

    assert routers
    for router in routers.values():
        assert router["tls"] == {"certResolver": "internal"}
        assert router["entryPoints"] == [entrypoint]


def test_traefik_mcp_shares_the_public_traefiks_image_and_hardening(compose, traefik_mcp):
    """#48 rule 2b: one image digest and one hardening for both Traefiks."""

    shared = ("image", "user", "read_only", "cap_drop", "security_opt")
    public = compose["services"]["traefik"]

    assert {key: traefik_mcp[key] for key in shared} == {key: public[key] for key in shared}


def test_traefik_mcp_cannot_reach_the_docker_api(traefik_mcp, static_config):
    """#48 rule 2b: no Docker API access (network_mode is pinned by CI)."""

    assert not any(
        "docker.sock" in volume or "socket-proxy" in volume for volume in traefik_mcp["volumes"]
    )
    assert "socket-proxy" not in traefik_mcp.get("depends_on", {})
    assert set(static_config["providers"]) == {"file"}


def test_traefik_mcp_adds_no_capability(traefik_mcp):
    """#48 rule 2b: cap_drop ALL stays, no cap_add (the 443 sysctl is pinned by CI)."""

    assert traefik_mcp["cap_drop"] == ["ALL"]
    assert "cap_add" not in traefik_mcp


def test_the_static_configuration_has_one_tls_entrypoint_on_443_and_no_management_api(
    static_config,
):
    """#48 rule 3: one TLS entrypoint on 443, no api, ping or metrics."""

    assert [e["address"] for e in static_config["entryPoints"].values()] == [":443"]
    assert next(iter(static_config["entryPoints"].values()))["http"]["tls"] == {}
    assert not {"api", "ping", "metrics"} & set(static_config)


def test_the_file_provider_reads_a_directory_of_its_own(traefik_mcp, static_config):
    """#48 rule 3: the file provider reads mcp/dynamic, never the public dynamic/."""

    directory = static_config["providers"]["file"]["directory"]

    assert f"./mcp/dynamic:{directory}:ro" in traefik_mcp["volumes"]
    assert directory != "/dynamic"


def test_the_internal_resolver_trusts_only_the_cas_root_over_tls_alpn_01(compose, static_config):
    """#48 rule 4: the resolver trusts the CA root alone, over TLS-ALPN-01."""

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
    """#48 rule 4: certificatesDuration equals step-ca's 24 h lifetime."""

    acme = static_config["certificatesResolvers"]["internal"]["acme"]
    ca = load("step-ca/ca.json")

    assert acme["certificatesDuration"] == CA_DEFAULT_LIFETIME_HOURS
    assert "claims" not in ca["authority"]
    assert all("claims" not in provisioner for provisioner in ca["authority"]["provisioners"])


def test_the_acme_storage_is_a_named_volume_that_traefik_mcp_can_write(
    compose, traefik_mcp, static_config
):
    """#48 rule 4: the ACME storage is a named volume that uid 65534 can write."""

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
    """#48 rule 5: docs/naming.md owns the <stem>.mcp.internal name."""

    naming = (ROOT / "docs" / "naming.md").read_text()

    assert "`<stem>.mcp.internal`" in naming
