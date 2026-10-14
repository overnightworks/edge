"""What counts as exposed, and the findings that say so."""

import ipaddress
from dataclasses import dataclass
from enum import StrEnum

WILDCARD_ADDRESS = "*"


class Protocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"
    SCTP = "sctp"


class Problem(StrEnum):
    PUBLICATION_BEYOND_LOOPBACK = "publication-beyond-loopback"
    PUBLICATION_WITHOUT_HOST_IP = "publication-without-host-ip"
    LISTENER_BEYOND_LOOPBACK = "listener-beyond-loopback"


@dataclass(frozen=True)
class Finding:
    problem: Problem
    subject: str
    protocol: Protocol
    address: str
    port: int | None

    def as_json(self) -> dict[str, str | int | None]:
        return {
            "problem": self.problem.value,
            "subject": self.subject,
            "protocol": self.protocol.value,
            "address": self.address,
            "port": self.port,
        }


def is_loopback(address: str) -> bool:
    if address == WILDCARD_ADDRESS:
        return False
    return ipaddress.ip_address(address).is_loopback


def ordered(findings: list[Finding]) -> list[Finding]:
    """Drop duplicates and sort, so two runs on the same state print the same list."""

    def sort_key(finding: Finding) -> tuple[str, str, str, str, int]:
        port = -1 if finding.port is None else finding.port
        return (finding.problem, finding.subject, finding.protocol, finding.address, port)

    return sorted(set(findings), key=sort_key)
