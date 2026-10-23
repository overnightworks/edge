"""The probes' network isolation (#346 P26): what probe/edge_copy.py moves and refuses."""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probe"))
sys.path.insert(0, str(ROOT / "src"))

import edge_copy  # noqa: E402

PROJECT = "edge-some-probe"


def fixed_networks(text: str) -> dict[str, str]:
    networks = yaml.safe_load(text)["networks"]
    return {key: network["name"] for key, network in networks.items() if "name" in network}


def test_every_fixed_network_of_compose_moves_into_the_project():
    compose = (ROOT / "compose.yaml").read_text()

    moved = fixed_networks(edge_copy.namespaced_compose(compose, PROJECT))

    assert moved
    assert all(name.startswith(f"{PROJECT}_") for name in moved.values())
    assert moved.keys() == fixed_networks(compose).keys()


def test_a_compose_text_with_no_fixed_network_is_refused():
    with pytest.raises(edge_copy.ProbeFailed, match="no fixed hal_ network"):
        edge_copy.namespaced_compose("networks:\n  mcp:\n    internal: true\n", PROJECT)


def test_a_hal_network_name_the_rewrite_cannot_match_is_refused():
    text = "networks:\n  a:\n    name: hal_a\n  b:\n    name: hal_b # live\n"

    with pytest.raises(edge_copy.ProbeFailed, match="remains after the rewrite"):
        edge_copy.namespaced_compose(text, PROJECT)


def test_a_resolved_network_outside_the_project_fails_the_guard():
    resolved = {"networks": {"a": {"name": f"{PROJECT}_a"}, "b": {"name": "hal_agent_songmaker"}}}

    with pytest.raises(edge_copy.ProbeFailed, match="hal_agent_songmaker"):
        edge_copy.expect_project_networks(resolved, PROJECT)


def test_a_resolved_configuration_inside_the_project_passes_the_guard():
    edge_copy.expect_project_networks({"networks": {"a": {"name": f"{PROJECT}_a"}}}, PROJECT)


OCCUPIED = ["172.31.250.0/24", "172.31.251.0/24", "10.0.0.0/8"]
CANDIDATES = ["172.31.250.0/24", "172.31.251.0/24", "172.31.249.0/24"]


def test_the_first_candidate_that_overlaps_no_occupied_subnet_is_free():
    assert str(edge_copy.free_subnet(CANDIDATES, OCCUPIED)) == "172.31.249.0/24"


def test_a_candidate_inside_a_wider_occupied_subnet_is_taken():
    with pytest.raises(edge_copy.ProbeFailed, match="no free /24"):
        edge_copy.free_subnet(CANDIDATES, ["172.31.0.0/16"])
