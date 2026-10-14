"""Host listeners, read from `ss -H -ltnupe` output."""

import re
from dataclasses import dataclass

from edge_check.exposure import Protocol

_PROCESS_NAME = re.compile(r'users:\(\("([^"]+)"')
_CGROUP = re.compile(r"cgroup:(\S+)")
_LOCAL_ADDRESS_COLUMN = 4


@dataclass(frozen=True)
class Listener:
    protocol: Protocol
    address: str
    port: int
    owner: str | None


def parse_listeners(ss_output: str) -> list[Listener]:
    return [_parse_line(line) for line in ss_output.splitlines() if line.strip()]


def _parse_line(line: str) -> Listener:
    columns = line.split()
    address, _, port = columns[_LOCAL_ADDRESS_COLUMN].rpartition(":")
    return Listener(
        protocol=Protocol(columns[0]),
        address=_bare_address(address),
        port=int(port),
        owner=_owner(line),
    )


def _bare_address(address: str) -> str:
    """`[fe80::1]%wlan0` -> `fe80::1`; `127.0.0.53%lo` -> `127.0.0.53`."""
    return address.partition("%")[0].removeprefix("[").removesuffix("]")


def _owner(line: str) -> str | None:
    """The process name when ss may show it, otherwise the systemd unit or scope of the socket.

    ss names only the current user's processes without root; the cgroup is visible for all.
    """
    if process := _PROCESS_NAME.search(line):
        return process.group(1)
    if cgroup := _CGROUP.search(line):
        return cgroup.group(1).rpartition("/")[2] or None
    return None
