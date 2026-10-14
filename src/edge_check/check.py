"""The one decision: which publications and listeners reach beyond loopback."""

from edge_check.allowlist import AllowEntry
from edge_check.docker import Publication, parse_publications
from edge_check.exposure import Finding, Problem, is_loopback, ordered
from edge_check.listeners import Listener, parse_listeners


def check(inspect_json: str, ss_output: str, allow_list: tuple[AllowEntry, ...]) -> list[Finding]:
    findings = publication_findings(parse_publications(inspect_json))
    findings += listener_findings(parse_listeners(ss_output), allow_list)
    return ordered(findings)


def publication_findings(publications: list[Publication]) -> list[Finding]:
    return [
        Finding(
            problem=problem,
            subject=publication.container,
            protocol=publication.protocol,
            address=publication.host_ip,
            port=publication.host_port,
        )
        for publication in publications
        if (problem := _publication_problem(publication)) is not None
    ]


def listener_findings(
    listeners: list[Listener], allow_list: tuple[AllowEntry, ...]
) -> list[Finding]:
    return [
        Finding(
            problem=Problem.LISTENER_BEYOND_LOOPBACK,
            subject=listener.owner or "unknown process",
            protocol=listener.protocol,
            address=listener.address,
            port=listener.port,
        )
        for listener in listeners
        if not is_loopback(listener.address)
        and not any(entry.permits(listener) for entry in allow_list)
    ]


def _publication_problem(publication: Publication) -> Problem | None:
    if not publication.host_ip:
        return Problem.PUBLICATION_WITHOUT_HOST_IP
    if not is_loopback(publication.host_ip):
        return Problem.PUBLICATION_BEYOND_LOOPBACK
    return None
