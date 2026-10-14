"""Docker port publications, read from `docker inspect` JSON."""

import json
from dataclasses import dataclass

from edge_check.exposure import PortRange, Protocol, UnreadableOutputError, checked_address


@dataclass(frozen=True)
class Publication:
    container: str
    protocol: Protocol
    host_ip: str
    host_ports: PortRange | None


def parse_publications(inspect_json: str) -> list[Publication]:
    try:
        containers = json.loads(inspect_json)
    except json.JSONDecodeError as error:
        raise UnreadableOutputError(f"docker inspect printed no JSON: {error}") from error
    return [publication for container in containers for publication in _publications(container)]


def _publications(container: dict) -> list[Publication]:
    try:
        return _parse_container(container)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise UnreadableOutputError(
            f"docker inspect: cannot read container {_name(container)!r}: {error!r}"
        ) from error


def _parse_container(container: dict) -> list[Publication]:
    name = container["Name"].lstrip("/")
    port_bindings = container["HostConfig"].get("PortBindings") or {}
    if container["HostConfig"].get("PublishAllPorts"):
        port_bindings = _with_publish_all(port_bindings, container["Config"].get("ExposedPorts"))
    return [
        Publication(
            container=name,
            protocol=_protocol(container_port),
            host_ip=checked_address(host_ip) if (host_ip := binding.get("HostIp")) else "",
            host_ports=_host_ports(binding.get("HostPort") or ""),
        )
        for container_port, bindings in port_bindings.items()
        for binding in bindings or []
    ]


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
