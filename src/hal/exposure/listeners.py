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
_VERSION_CHARACTERS = "0123456789."
_MODULE_OPTION = "-m"
_INLINE_CODE_OPTIONS = frozenset({"-c", "-e", "-E", "-p", "--eval", "--print"})

CommandLineReader = Callable[[int], tuple[str, ...] | None]


class Interpreter(StrEnum):
    """Programs whose process name says nothing about the script they run."""

    PYTHON = "python"
    NODE = "node"
    NODEJS = "nodejs"
    PERL = "perl"
    RUBY = "ruby"


# The options of each interpreter whose value is a separate argument before the script.
_NODE_OPTIONS_WITH_VALUE = frozenset(
    {"-r", "--require", "--import", "--loader", "-C", "--conditions"}
)
_OPTIONS_WITH_VALUE = {
    Interpreter.PYTHON: frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    Interpreter.NODE: _NODE_OPTIONS_WITH_VALUE,
    Interpreter.NODEJS: _NODE_OPTIONS_WITH_VALUE,
    Interpreter.PERL: frozenset({"-I"}),
    Interpreter.RUBY: frozenset({"-I", "-r", "-C"}),
}


class UnitType(StrEnum):
    """The unit types of systemd.unit(5); a name with one of them as suffix names a unit."""

    SERVICE = ".service"
    SOCKET = ".socket"
    DEVICE = ".device"
    MOUNT = ".mount"
    AUTOMOUNT = ".automount"
    SWAP = ".swap"
    TARGET = ".target"
    PATH = ".path"
    TIMER = ".timer"
    SLICE = ".slice"
    SCOPE = ".scope"


def _is_unit_name(name: str) -> bool:
    return name.endswith(tuple(UnitType))


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
    """The script or module an interpreter runs, when its command line names one; it names a
    finding and never matches an allow-list entry."""

    @property
    def owner(self) -> str | None:
        """What a finding names: the program an interpreter runs, else the process when ss shows
        it, otherwise its unit or scope."""
        return self.program or self.process or self._unit()

    def is_owned_by(self, owner: str) -> bool:
        """A unit name matches only a unit of the system slice, which only root can place: never a
        unit elsewhere, such as another user's `systemd-run --user --unit`, nor a process any user
        can name like a unit. Any other name matches the process. The program an interpreter runs
        never matches, since any process chooses its arguments.
        """
        if _is_unit_name(owner):
            return owner == self._system_unit()
        return owner == self.process

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
        interpreter = _interpreter(listener.process)
        if listener.process_id is not None and interpreter is not None:
            if listener.process_id not in programs:
                command_line = read_command_line(listener.process_id)
                programs[listener.process_id] = _program_in(command_line, interpreter)
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


def _interpreter(process: str | None) -> Interpreter | None:
    if process is None:
        return None
    name = process.rstrip(_VERSION_CHARACTERS)
    return Interpreter(name) if name in set(Interpreter) else None


def _program_in(command_line: tuple[str, ...] | None, interpreter: Interpreter) -> str | None:
    """The module after `-m`, else the first argument that is neither an option nor its value, as
    its file name; None for inline code or when no command line could be read."""
    arguments = iter(command_line[1:] if command_line else ())
    for argument in arguments:
        if argument == _MODULE_OPTION:
            return next(arguments, None)
        if argument in _INLINE_CODE_OPTIONS:
            return None
        if argument in _OPTIONS_WITH_VALUE[interpreter]:
            next(arguments, None)
        elif not argument.startswith("-"):
            return PurePath(argument).name or None
    return None


def _bare_address(address: str) -> str:
    """`[fe80::1]%wlan0` -> `fe80::1`; `127.0.0.53%lo` -> `127.0.0.53`."""
    return address.partition("%")[0].removeprefix("[").removesuffix("]")
