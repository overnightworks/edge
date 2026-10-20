"""The agent service's egress as files, no containers (#189 cell E16): compose.yaml, the gateway's
static and dynamic files and the probe overlay.

The running paths are proven by probe/agent-service/egress.py; these tests hold the static facts
the probe does not see: every network a member of the agent attachments joins is internal and
isolated, the gateway is the only service with a route out, and the names the service resolves are
the names the gateway passes.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "agent-egress"
STAND_IN = "agent-probe"
OBSERVER = "realm-observer"
ISOLATED_MODE = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
HOST_SNI = re.compile(r"HostSNI\(`([^`]+)`\)")


def load(path: str):
    return yaml.safe_load((ROOT / path).read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return load("compose.yaml")


@pytest.fixture(scope="module")
def overlay() -> dict:
    return load("probe/agent-service/compose.probe.yaml")


@pytest.fixture(scope="module")
def routers() -> dict:
    return load("agent-service/egress/dynamic/allowed.yml")["tcp"]["routers"]


@pytest.fixture(scope="module")
def attachments(compose) -> dict:
    return compose["x-agent-attachments"]


@pytest.fixture(scope="module")
def passed_names(routers) -> set[str]:
    return {HOST_SNI.fullmatch(router["rule"]).group(1) for router in routers.values()}


def test_every_network_of_the_agent_attachments_is_internal_and_isolated(compose, attachments):
    """E16: the service and the stand-in join only networks with no route out or host address."""
    joined = {name: compose["networks"][name] for name in attachments["networks"]}

    assert set(joined) == {"agent", "egress"}
    for network in joined.values():
        assert network["internal"] is True
        assert network["driver_opts"] == ISOLATED_MODE


def test_the_stand_in_carries_the_attachments_and_nothing_else_of_the_network(compose, attachments):
    """E16: the stand-in merges the one anchor; it adds no port, no network mode, no network."""
    stand_in = compose["services"][STAND_IN]

    assert stand_in["networks"] == attachments["networks"]
    assert stand_in["extra_hosts"] == attachments["extra_hosts"]
    assert not {"ports", "network_mode", "expose"} & stand_in.keys()


def test_the_gateway_is_the_only_service_on_internet(compose):
    """E16: one route out, and it is the gateway's."""
    on_internet = [
        name
        for name, service in compose["services"].items()
        if "internet" in (service.get("networks") or [])
    ]

    assert on_internet == [GATEWAY]
    assert compose["networks"]["internet"].get("internal") is not True
    assert "ports" not in compose["services"][GATEWAY]
    assert "network_mode" not in compose["services"][GATEWAY]


def test_the_names_the_service_resolves_are_the_names_the_gateway_passes(attachments, passed_names):
    """E16: extra_hosts and the gateway's HostSNI names are one list, at the gateway's address."""
    assert set(attachments["extra_hosts"]) == passed_names
    assert set(attachments["extra_hosts"].values()) == {"172.31.252.10"}


def test_the_gateways_address_stands_in_compose_and_in_its_static_file(compose):
    """E16: x-egress-address is the gateway's address on egress and the one entrypoint's host."""
    address = compose["x-egress-address"]
    entrypoints = load("agent-service/egress/traefik.yml")["entryPoints"]

    assert compose["services"][GATEWAY]["networks"]["egress"] == {"ipv4_address": address}
    assert [entrypoint["address"] for entrypoint in entrypoints.values()] == [f"{address}:443"]
    subnet = compose["networks"]["egress"]["ipam"]["config"][0]["subnet"]
    assert subnet == "172.31.252.0/24"


def test_the_gateway_shares_the_public_traefiks_image_and_hardening(compose):
    """E16: one image digest and one hardening for every Traefik instance."""
    shared = ("image", "user", "read_only", "cap_drop", "security_opt")
    public = compose["services"]["traefik"]

    assert {key: compose["services"][GATEWAY][key] for key in shared} == {
        key: public[key] for key in shared
    }


def test_the_gateway_starts_only_with_the_agent_service_profile(compose):
    """No plain `docker compose up -d` starts the gateway or the stand-in."""
    assert compose["services"][GATEWAY]["profiles"] == ["agent-service"]
    assert compose["services"][STAND_IN]["profiles"] == ["agent-probe"]


def test_the_gateways_static_file_keeps_global_and_log_of_the_public_traefik_and_no_api():
    """E16: version check and telemetry stay off, and api, ping and metrics stay absent."""
    public, gateway = load("traefik.yml"), load("agent-service/egress/traefik.yml")

    assert {key: gateway[key] for key in ("global", "log")} == {
        key: public[key] for key in ("global", "log")
    }
    assert not {"api", "ping", "metrics"} & gateway.keys()


def test_every_router_passes_its_name_through_to_that_name_on_443(routers):
    """The gateway holds no key: each router passes TLS through to the host it matched."""
    services = load("agent-service/egress/dynamic/allowed.yml")["tcp"]["services"]

    assert routers
    for router in routers.values():
        (server,) = services[router["service"]]["loadBalancer"]["servers"]
        assert router["tls"] == {"passthrough": True}
        assert server["address"] == f"{HOST_SNI.fullmatch(router['rule']).group(1)}:443"


def test_an_unmatched_or_missing_sni_is_refused_in_the_gateway():
    """No catch-all router exists, and the default TLS options refuse an unknown name."""
    allowed = load("agent-service/egress/dynamic/allowed.yml")

    assert allowed["tls"]["options"]["default"]["sniStrict"] is True
    assert all(HOST_SNI.fullmatch(router["rule"]) for router in allowed["tcp"]["routers"].values())


@pytest.mark.parametrize("service", ["capture-egress", "capture-host"])
def test_a_capture_service_runs_the_observers_pin_with_net_raw_alone(compose, overlay, service):
    """The packet windows use the realm observer's image and add one capability to none."""
    capture = overlay["services"][service]

    assert capture["image"] == compose["services"][OBSERVER]["image"]
    assert capture["cap_drop"] == ["ALL"]
    assert capture["cap_add"] == ["NET_RAW"]
    assert capture["read_only"] is True
    assert capture["volumes"] == ["./probe/agent-service/capture.py:/probe/capture.py:ro"]
    assert "ports" not in capture


def test_the_capture_windows_watch_the_gateways_namespace_and_the_hosts(overlay):
    """capture-egress joins the gateway's namespace; capture-host the host's."""
    services = overlay["services"]

    assert services["capture-egress"]["network_mode"] == f"service:{GATEWAY}"
    assert services["capture-host"]["network_mode"] == "host"
