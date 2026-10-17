"""The boundary to systemd for the tunnel: `systemctl` verbs and `pkcheck`, nothing else.

edge never handles credentials (rule 8): a password goes to the system's own prompt, and the
tunnel's own files are never read. The unit and the manager are parameters, so the probe can drive
a throwaway user unit in place of `cloudflared.service`.
"""

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

CLOUDFLARED_UNIT = "cloudflared.service"
BOOT_DROP_IN = "30-edge-global-stop.conf"
MANAGE_UNITS_ACTION = "org.freedesktop.systemd1.manage-units"
UNIT_VARIABLE = "EDGE_TUNNEL_UNIT"
MANAGER_VARIABLE = "EDGE_TUNNEL_MANAGER"
_SECONDS = 120

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class TunnelError(Exception):
    """systemd cannot be asked; the message is one line."""


class Manager(StrEnum):
    SYSTEM = "system"
    USER = "user"

    @property
    def flag(self) -> str:
        return f"--{self.value}"


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Only stdout is captured: stdin and stderr stay the terminal's, for the system's prompt."""
    try:
        return subprocess.run(
            command, stdout=subprocess.PIPE, text=True, check=False, timeout=_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TunnelError(f"cannot run {command[0]} ({error})") from error


@dataclass(frozen=True)
class Tunnel:
    unit: str = CLOUDFLARED_UNIT
    manager: Manager = Manager.SYSTEM
    run: Runner = run_command

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> Self:
        """`cloudflared.service` of the system manager unless the probe names a throwaway unit."""
        return cls(
            environment.get(UNIT_VARIABLE, CLOUDFLARED_UNIT),
            Manager(environment.get(MANAGER_VARIABLE, Manager.SYSTEM)),
        )

    def stop(self, *, may_ask_password: bool) -> None:
        """Ask for the stop; whether it happened is for `is_inactive` to say. Without a terminal
        nobody could answer a prompt."""
        flags = [] if may_ask_password else ["--no-ask-password"]
        self._systemctl(*flags, "stop")

    def start(self) -> bool:
        """True when systemd accepted the start; a password goes to the system's own prompt."""
        return self._systemctl("start").returncode == 0

    def is_active(self) -> bool:
        return self._state() == "active"

    def is_inactive(self) -> bool:
        return self._state() == "inactive"

    def has_boot_drop_in(self) -> bool:
        return BOOT_DROP_IN in self._systemctl("show", "-p", "DropInPaths").stdout

    def authorized_to_start(self, process_id: int) -> bool:
        """The system's authorization for `start`, asked before anything is cleared. The user
        manager answers to its own user, so there is nothing to ask."""
        if self.manager is Manager.USER:
            return True
        asked = self.run(
            ["pkcheck", "--action-id", MANAGE_UNITS_ACTION, "--process", str(process_id),
             "--allow-user-interaction"]
        )  # fmt: skip
        return asked.returncode == 0

    def _state(self) -> str:
        return self._systemctl("is-active").stdout.strip()

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run(["systemctl", self.manager.flag, *arguments, self.unit])
