"""What `hal check exposure` prints: one row for each process or container that reaches beyond
loopback, one row for each routing-name finding, and the verdict. The facts name the subject, the
protocol and the count, never an address, a port or a problem code; `--json` lists each socket."""

from collections import Counter
from collections.abc import Sequence
from enum import StrEnum

from hal.exposure.exposure import Finding, Problem, Protocol
from hal.output import Glyph, Noun, Row, TrailingNoun, Verdict, counted, write_row, write_verdict


class Event(StrEnum):
    LISTENS = "listens"
    PUBLISHES = "publishes"
    ROUTING_NAME = "routing name"


class PortLabel(TrailingNoun):
    TCP = "TCP port"
    UDP = "UDP port"
    SCTP = "SCTP port"


class Label(Noun):
    FINDING = "finding"
    PORT = "port"


PORT_LABELS = {
    Protocol.TCP: PortLabel.TCP,
    Protocol.UDP: PortLabel.UDP,
    Protocol.SCTP: PortLabel.SCTP,
}

_EVENT_AND_FACT = {
    Problem.LISTENER_BEYOND_LOOPBACK: (Event.LISTENS, "{ports} beyond loopback"),
    Problem.PUBLICATION_BEYOND_LOOPBACK: (Event.PUBLISHES, "{ports} beyond loopback"),
    Problem.PUBLICATION_WITHOUT_HOST_IP: (Event.PUBLISHES, "{ports} without a host IP"),
    Problem.ROUTED_WITHOUT_EDGE_NAME: (Event.ROUTING_NAME, "none, though Traefik routes it"),
    Problem.INVALID_EDGE_NAME: (Event.ROUTING_NAME, "{name} is not a valid name"),
    Problem.DUPLICATE_EDGE_NAME: (Event.ROUTING_NAME, "{name} is claimed more than once"),
}

_GroupKey = tuple[Problem, str, str | None]


def _groups(findings: Sequence[Finding]) -> dict[_GroupKey, Counter[Protocol]]:
    """Socket findings share a row per problem and subject; a routing-name finding stands alone,
    keyed by its name."""
    groups: dict[_GroupKey, Counter[Protocol]] = {}
    for finding in findings:
        routing_name = finding.edge_name if finding.protocol is None else None
        sockets = groups.setdefault((finding.problem, finding.subject, routing_name), Counter())
        if finding.protocol is not None:
            sockets[finding.protocol] += 1
    return groups


def rows_of(findings: Sequence[Finding]) -> tuple[Row, ...]:
    rows = []
    for (problem, subject, routing_name), sockets in _groups(findings).items():
        event, template = _EVENT_AND_FACT[problem]
        ports = ", ".join(
            counted(sockets[protocol], label)
            for protocol, label in PORT_LABELS.items()
            if sockets[protocol]
        )
        rows.append(
            Row(Glyph.NEEDS_YOU, subject, event, template.format(ports=ports, name=routing_name))
        )
    return tuple(rows)


def verdict_of(findings: Sequence[Finding]) -> Verdict:
    groups = _groups(findings)
    if not groups:
        return Verdict(Glyph.DONE, "nothing reaches beyond loopback", ())
    ports = sum(sum(sockets.values()) for sockets in groups.values())
    grouped = any(sum(sockets.values()) > 1 for sockets in groups.values())
    summary = counted(len(groups), Label.FINDING)
    if not grouped:
        return Verdict(Glyph.NEEDS_YOU, summary, ())
    return Verdict(Glyph.NEEDS_YOU, summary, ((ports, Label.PORT),), hint="--json lists each")


def write(findings: Sequence[Finding]) -> None:
    for row in rows_of(findings):
        write_row(row)
    write_verdict(verdict_of(findings))
