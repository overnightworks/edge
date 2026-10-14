"""Host listeners, read from `ss -H -ltnupe` output."""

import re
from dataclasses import dataclass

from edge_check.exposure import Protocol, UnreadableOutputError, checked_address

_PROCESS_NAME = re.compile(r'users:\(\("([^"]+)"')
_CGROUP = re.compile(r"cgroup:(\S+)")
_LOCAL_ADDRESS_COLUMN = 4
_SYSTEM_SLICE = "/system.slice/"


@dataclass(frozen=True)
class Listener:
    """A listening socket; ss names the process only for the current user (or for root)."""

    protocol: Protocol
    address: str
    port: int
    process: str | None
    cgroup: str | None

    @property
    def owner(self) -> str | None:
        """What a finding names: the process when ss shows it, otherwise its unit or scope."""
        return self.process or self._unit()

    def is_owned_by(self, owner: str) -> bool:
        """One name matches as user and as root: the process, or a unit of the system slice.

        A unit elsewhere, such as another user's `systemd-run --user --unit`, never matches.
        """
        return owner in (self.process, self._system_unit())

    def _unit(self) -> str | None:
        return (self.cgroup.rpartition("/")[2] or None) if self.cgroup else None

    def _system_unit(self) -> str | None:
        in_system_slice = self.cgroup is not None and self.cgroup.startswith(_SYSTEM_SLICE)
        return self._unit() if in_system_slice else None


def parse_listeners(ss_output: str) -> list[Listener]:
    return [_parse_line(line) for line in ss_output.splitlines() if line.strip()]


def _parse_line(line: str) -> Listener:
    try:
        return _parse_columns(line)
    except (IndexError, ValueError) as error:
        raise UnreadableOutputError(f"ss: cannot read line {line.strip()!r}: {error}") from error


def _parse_columns(line: str) -> Listener:
    columns = line.split()
    address, _, port = columns[_LOCAL_ADDRESS_COLUMN].rpartition(":")
    return Listener(
        protocol=Protocol(columns[0]),
        address=checked_address(_bare_address(address)),
        port=int(port),
        process=process.group(1) if (process := _PROCESS_NAME.search(line)) else None,
        cgroup=cgroup.group(1) if (cgroup := _CGROUP.search(line)) else None,
    )


def _bare_address(address: str) -> str:
    """`[fe80::1]%wlan0` -> `fe80::1`; `127.0.0.53%lo` -> `127.0.0.53`."""
    return address.partition("%")[0].removeprefix("[").removesuffix("]")
