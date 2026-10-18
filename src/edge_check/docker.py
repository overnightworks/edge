"""Docker containers, their port publications and edge labels, read from `docker inspect` JSON."""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from edge_check.exposure import PortRange, Protocol, UnreadableOutputError, checked_address

EDGE_NAME_LABEL = "edge.name"
_TRAEFIK_ENABLE_LABEL = "traefik.enable"
# Traefik reads the label with Go's strconv.ParseBool, so these are exactly the "true" spellings.
GO_TRUE_SPELLINGS = frozenset({"1", "t", "T", "true", "TRUE", "True"})


@dataclass(frozen=True)
class Publication:
    container: str
    protocol: Protocol
    host_ip: str
    host_ports: PortRange | None


@dataclass(frozen=True)
class Container:
    name: str
    running: bool
    routed: bool
    """Labelled `traefik.enable=true`, so Traefik would route it."""
    edge_name: str | None
    publications: tuple[Publication, ...]


def parse_containers(inspect_json: str) -> list[Container]:
    try:
        containers = json.loads(inspect_json)
    except json.JSONDecodeError as error:
        raise UnreadableOutputError(f"docker inspect printed no JSON: {error}") from error
    if not isinstance(containers, list):
        raise UnreadableOutputError(
            f"docker inspect printed {inspect_json.strip()[:40]!r}, not a list of containers"
        )
    return [_container(container) for container in containers]


def _container(container: dict) -> Container:
    try:
        return _parse_container(container)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise UnreadableOutputError(
            f"docker inspect: cannot read container {_name(container)!r}: {error!r}"
        ) from error


def is_routed(labels: Mapping[str, str]) -> bool:
    """Whether Traefik routes the service that carries these labels."""
    return labels.get(_TRAEFIK_ENABLE_LABEL) in GO_TRUE_SPELLINGS


def _parse_container(container: dict) -> Container:
    name = container["Name"].lstrip("/")
    labels = container["Config"].get("Labels") or {}
    return Container(
        name=name,
        running=container["State"]["Running"],
        routed=is_routed(labels),
        edge_name=labels.get(EDGE_NAME_LABEL),
        publications=_publications(name, container),
    )


def _checked_host_ip(host_ip: str | None) -> str:
    return checked_address(host_ip) if host_ip else ""


def _publications(name: str, container: dict) -> tuple[Publication, ...]:
    port_bindings = container["HostConfig"].get("PortBindings") or {}
    if container["HostConfig"].get("PublishAllPorts"):
        port_bindings = _with_publish_all(port_bindings, container["Config"].get("ExposedPorts"))
    return tuple(
        Publication(
            container=name,
            protocol=_protocol(container_port),
            host_ip=_checked_host_ip(binding.get("HostIp")),
            host_ports=_host_ports(binding.get("HostPort") or ""),
        )
        for container_port, bindings in port_bindings.items()
        for binding in bindings or []
    )


def _with_publish_all(port_bindings: dict, exposed_ports: dict | None) -> dict:
    """`-P` publishes every exposed port without an explicit binding: no host IP, random port."""
    publish_all = {port: [{}] for port in exposed_ports or {} if not port_bindings.get(port)}
    return port_bindings | publish_all


def _protocol(container_port: str) -> Protocol:
    """`80/tcp` -> tcp; Docker writes the protocol, but a bare port means tcp."""
    return Protocol(container_port.partition("/")[2] or Protocol.TCP)


def _host_ports(host_port: str) -> PortRange | None:
    """`8080` or `8000-8010`; empty means Docker picks a random port."""
    if not host_port:
        return None
    first, _, last = host_port.partition("-")
    return PortRange(int(first), int(last or first))


def _name(container: object) -> str:
    if isinstance(container, dict):
        return str(container.get("Name") or container.get("Id") or "unnamed").lstrip("/")
    return "unnamed"
