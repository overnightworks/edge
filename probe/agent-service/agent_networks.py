"""The agent networks of compose.yaml in the egress probe: P36, one caller per app's network."""

from __future__ import annotations

import itertools
import json
import time
from typing import TYPE_CHECKING

from edge_copy import expect
from egress_observe import UNREACHABLE

if TYPE_CHECKING:
    from egress import Probe

# `hal_agent_<app>` of compose.yaml, as the probe's copy names them: `<project>_agent_<app>`.
# P36 drives only these, which keep one app from another.
AGENT_NETWORKS = ("agent_songmaker", "agent_presentator", "agent_hopin")
CALLER_PORT = 8000
READY_SECONDS = 90


def service_networks(probe: Probe, project: str) -> tuple[str, ...]:
    """Every network the target joins, from the probe copy's resolved configuration, without the
    project's prefix (every name of the copy is the project's own)."""
    configuration = json.loads(probe.compose("config", "--format", "json").stdout)
    prefix = f"{project}_"
    names = [
        configuration["networks"][key]["name"]
        for key in configuration["services"][probe.target.compose_service]["networks"]
    ]
    expect(all(name.startswith(prefix) for name in names), f"a network outside {prefix}*: {names}")
    return tuple(name.removeprefix(prefix) for name in names)


def cell_p36(
    probe: Probe, *, project: str, label: str, service_port: int, service_listens: bool
) -> None:
    """One caller per agent network: neither reaches the other's port, each reaches the target by
    its name, and the target is a member of both networks."""
    names = {network: f"{project}-caller-{network}" for network in AGENT_NETWORKS}
    addresses = {
        network: start_caller(probe, project, label, network, name)
        for network, name in names.items()
    }
    target = probe.target.compose_service
    reached = "connected" if service_listens else "ECONNREFUSED"
    for caller, other in itertools.permutations(AGENT_NETWORKS):
        beside = names[caller]
        seen = probe.client_in_namespace_of(beside, "connect", addresses[other], str(CALLER_PORT))[
            "result"
        ]
        expect(seen in UNREACHABLE, f"P36: {caller} reaches {other}'s port by address: {seen}")
        found = probe.client_in_namespace_of(beside, "resolve", names[other])["addresses"]
        expect(not found, f"P36: {caller} resolves {names[other]} to {found}")
        answer = probe.client_in_namespace_of(beside, "connect", target, str(service_port))
        expect(answer["result"] == reached, f"P36: {caller} -> {target}:{service_port}: {answer}")
    target_id = probe.container(target)
    for network in AGENT_NETWORKS:
        members = probe.inspect_network(network)["Containers"]
        expect(target_id in members, f"P36: {target} is not a member of {network}")
    probe.hold(
        "P36",
        f"the callers on {', '.join(AGENT_NETWORKS)} reach neither another's port nor its "
        f"name; each reaches {target}:{service_port} ({reached}); {target} is on all "
        f"{len(AGENT_NETWORKS)}",
    )


def start_caller(probe: Probe, project: str, label: str, network: str, name: str) -> str:
    """A container that listens on CALLER_PORT on one agent network, removed with the stack (its
    label); its address there."""
    probe.run(
        ["docker", "run", "--detach", "--name", name, "--label", label,
         f"--network={project}_{network}", "--read-only", "--user", "65534:65534",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--entrypoint",
         "python", probe.image, "-I", "-m", "http.server", str(CALLER_PORT)]
    )  # fmt: skip
    deadline = time.monotonic() + READY_SECONDS
    while (
        probe.client_in_namespace_of(name, "connect", "127.0.0.1", str(CALLER_PORT))["result"]
        != "connected"
    ):
        expect(time.monotonic() < deadline, f"P36: {name} does not listen")
        time.sleep(1)
    address_of = f'{{{{(index .NetworkSettings.Networks "{project}_{network}").IPAddress}}}}'
    return probe.run(["docker", "inspect", "--format", address_of, name]).stdout.strip()
