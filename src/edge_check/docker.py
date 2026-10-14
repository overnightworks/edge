"""Docker port publications, read from `docker inspect` JSON."""

import json
from dataclasses import dataclass

from edge_check.exposure import Protocol


@dataclass(frozen=True)
class Publication:
    container: str
    protocol: Protocol
    host_ip: str
    host_port: int | None


def parse_publications(inspect_json: str) -> list[Publication]:
    publications = []
    for container in json.loads(inspect_json):
        name = container["Name"].lstrip("/")
        port_bindings = container["HostConfig"].get("PortBindings") or {}
        for container_port, bindings in port_bindings.items():
            protocol = Protocol(container_port.partition("/")[2] or Protocol.TCP)
            for binding in bindings or []:
                host_port = binding.get("HostPort") or ""
                publications.append(
                    Publication(
                        container=name,
                        protocol=protocol,
                        host_ip=binding.get("HostIp") or "",
                        host_port=int(host_port) if host_port else None,
                    )
                )
    return publications
