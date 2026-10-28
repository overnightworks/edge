"""The one decision: what reaches beyond loopback, and which routed names Traefik would mix up."""

from collections import defaultdict

from hal.exposure.allowlist import AllowEntry
from hal.exposure.docker import TRAEFIK_NAME, Container, Publication, parse_containers
from hal.exposure.exposure import Finding, PortRange, Problem, is_loopback, ordered
from hal.exposure.listeners import Listener, parse_listeners

# docker-proxy runs in dockerd's unit: ss shows that cgroup to every caller, the process to root.
_DOCKER_PROXY_UNIT = "docker.service"


def check(inspect_json: str, ss_output: str, allow_list: tuple[AllowEntry, ...]) -> list[Finding]:
    containers = parse_containers(inspect_json)
    publications = [
        publication for container in containers for publication in container.publications
    ]
    running_publications = [
        publication
        for container in containers
        if container.running
        for publication in container.publications
    ]
    findings = publication_findings(publications)
    findings += edge_name_findings(containers)
    findings += listener_findings(parse_listeners(ss_output), allow_list, running_publications)
    return ordered(findings)


def publication_findings(publications: list[Publication]) -> list[Finding]:
    return [
        Finding(
            problem=problem,
            subject=publication.container,
            protocol=publication.protocol,
            address=publication.host_ip,
            port=publication.host_ports,
        )
        for publication in publications
        if (problem := _publication_problem(publication)) is not None
    ]


def listener_findings(
    listeners: list[Listener],
    allow_list: tuple[AllowEntry, ...],
    running_publications: list[Publication],
) -> list[Finding]:
    """Listeners no allow-list entry permits, apart from docker-proxy's sockets for publications.

    A publication beyond loopback is already its own finding; counting docker-proxy's listener
    again would tempt someone to allow-list docker.service, which would hide the next one.
    """
    return [
        Finding(
            problem=Problem.LISTENER_BEYOND_LOOPBACK,
            subject=listener.owner or "unknown process",
            protocol=listener.protocol,
            address=listener.address,
            port=PortRange.single(listener.port),
        )
        for listener in listeners
        if not is_loopback(listener.address)
        and not any(entry.permits(listener) for entry in allow_list)
        and not any(
            _holds_socket_for(publication, listener) for publication in running_publications
        )
    ]


def edge_name_findings(containers: list[Container]) -> list[Finding]:
    findings = [
        Finding(problem=Problem.ROUTED_WITHOUT_EDGE_NAME, subject=container.name)
        for container in containers
        if container.routed and container.edge_name is None
    ]
    findings += [
        Finding(problem=Problem.INVALID_EDGE_NAME, subject=container.name, edge_name=name)
        for container in containers
        for name in _claimed_hosts(container)
        if not TRAEFIK_NAME.fullmatch(name)
    ]
    findings += [
        Finding(
            problem=Problem.DUPLICATE_EDGE_NAME, subject=", ".join(sorted(holders)), edge_name=name
        )
        for name, holders in _running_containers_by_host(containers).items()
        if len(holders) > 1
    ]
    return findings


def _claimed_hosts(container: Container) -> tuple[str, ...]:
    """The `edge.name` and the `edge.alias` of a container, each only when it carries it."""
    return tuple(host for host in (container.edge_name, container.edge_alias) if host is not None)


def _running_containers_by_host(containers: list[Container]) -> dict[str, list[str]]:
    by_host: dict[str, list[str]] = defaultdict(list)
    for container in containers:
        if container.running:
            for host in dict.fromkeys(_claimed_hosts(container)):
                by_host[host].append(container.name)
    return by_host


def _publication_problem(publication: Publication) -> Problem | None:
    if not publication.host_ip:
        return Problem.PUBLICATION_WITHOUT_HOST_IP
    if not is_loopback(publication.host_ip):
        return Problem.PUBLICATION_BEYOND_LOOPBACK
    return None


def _holds_socket_for(publication: Publication, listener: Listener) -> bool:
    """docker-proxy listens on the published address and port; no host IP means every address."""
    return (
        listener.is_owned_by(_DOCKER_PROXY_UNIT)
        and publication.protocol == listener.protocol
        and publication.host_ip in ("", listener.address)
        and publication.host_ports is not None
        and publication.host_ports.contains(listener.port)
    )
