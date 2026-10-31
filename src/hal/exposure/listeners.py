"""Host listeners, read from `ss -H -ltnupe` output."""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePath

from hal.exposure.exposure import Protocol, UnreadableOutputError, checked_address

_PROCESS = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
_CGROUP = re.compile(r"cgroup:(\S+)")
_LOCAL_ADDRESS_COLUMN = 4
_SYSTEM_SLICE = "/system.slice/"
_VERSION_SUFFIX = re.compile(r"[\d.]+$")
_MODULE_OPTION = "-m"
_INLINE_CODE_OPTIONS = ("-c", "-e")

CommandLineReader = Callable[[int], tuple[str, ...] | None]


class Interpreter(StrEnum):
    """Programs whose process name says nothing about the script they run."""

    PYTHON = "python"
    NODE = "node"
    NODEJS = "nodejs"
    PERL = "perl"
    RUBY = "ruby"


@dataclass(frozen=True)
class Listener:
    """A listening socket; ss names the process only for the current user (or for root)."""

    protocol: Protocol
    address: str
    port: int
    process: str | None
    cgroup: str | None
    process_id: int | None = None
    program: str | None = None
    """The script or module an interpreter runs, when its command line names one."""

    @property
    def owner(self) -> str | None:
        """What a finding names: the program an interpreter runs, else the process when ss shows
        it, otherwise its unit or scope."""
        return self.program or self.process or self._unit()

    def is_owned_by(self, owner: str) -> bool:
        """One name matches as user and as root: the process, the program an interpreter runs, or
        a unit of the system slice.

        A unit elsewhere, such as another user's `systemd-run --user --unit`, never matches.
        """
        return owner in (self.process, self.program, self._system_unit())

    def _unit(self) -> str | None:
        return (self.cgroup.rpartition("/")[2] or None) if self.cgroup else None

    def _system_unit(self) -> str | None:
        in_system_slice = self.cgroup is not None and self.cgroup.startswith(_SYSTEM_SLICE)
        return self._unit() if in_system_slice else None


def parse_listeners(ss_output: str, read_command_line: CommandLineReader) -> list[Listener]:
    """Each interpreter process has its command line read once, however many sockets it holds."""
    programs: dict[int, str | None] = {}
    listeners = []
    for line in (line for line in ss_output.splitlines() if line.strip()):
        listener = _parse_line(line)
        if listener.process_id is not None and _is_interpreter(listener.process):
            if listener.process_id not in programs:
                programs[listener.process_id] = _program_in(read_command_line(listener.process_id))
            listener = replace(listener, program=programs[listener.process_id])
        listeners.append(listener)
    return listeners


def _parse_line(line: str) -> Listener:
    try:
        return _parse_columns(line)
    except (IndexError, ValueError) as error:
        raise UnreadableOutputError(f"ss: cannot read line {line.strip()!r}: {error}") from error


def _parse_columns(line: str) -> Listener:
    columns = line.split()
    address, _, port = columns[_LOCAL_ADDRESS_COLUMN].rpartition(":")
    process = _PROCESS.search(line)
    cgroup = _CGROUP.search(line)
    return Listener(
        protocol=Protocol(columns[0]),
        address=checked_address(_bare_address(address)),
        port=int(port),
        process=process.group(1) if process else None,
        cgroup=cgroup.group(1) if cgroup else None,
        process_id=int(process.group(2)) if process else None,
    )


def _is_interpreter(process: str | None) -> bool:
    return process is not None and _VERSION_SUFFIX.sub("", process) in set(Interpreter)


def _program_in(command_line: tuple[str, ...] | None) -> str | None:
    """The module after `-m`, else the first argument that is no option, as its file name;
    None for inline code or when no command line could be read."""
    arguments = iter(command_line[1:] if command_line else ())
    for argument in arguments:
        if argument == _MODULE_OPTION:
            return next(arguments, None)
        if argument in _INLINE_CODE_OPTIONS:
            return None
        if not argument.startswith("-"):
            return PurePath(argument).name or None
    return None


def _bare_address(address: str) -> str:
    """`[fe80::1]%wlan0` -> `fe80::1`; `127.0.0.53%lo` -> `127.0.0.53`."""
    return address.partition("%")[0].removeprefix("[").removesuffix("]")
