"""The system step of `hal setup`: edge's cloudflared files and the stop-only polkit rule, written by Ansible under one sudo prompt.

The root work is `system/playbook.yml`. This module runs it with the terminal passed through, says what its check found in one
of three states, and keeps the record of the last verified run, which lets `home next` see the step without Ansible or sudo.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from home_tools import secrets_setup
from home_tools.tidy_home import Layout

PLAYBOOK = Path(__file__).parent / "system" / "playbook.yml"
# The agents' own OS account: the play creates it dormant, and `hal setup` skips the operator's steps for exactly this user.
AGENT_USER = "agent"
# The ruled bound on every server log on this PC: „ja 30 tage für alle“.
LOG_RETENTION_DAYS = 30
# journalctl deletes whole archive files by the age of their oldest entry: one day for a file's span
# and one day of slack for a PC that is off at midnight keep every line under LOG_RETENTION_DAYS.
JOURNAL_VACUUM_DAYS = LOG_RETENTION_DAYS - 2
# The last line of a play recap names the host and counts each outcome.
RECAP = re.compile(
    r"^\S+\s*:\s*ok=\d+\s+changed=(?P<changed>\d+)\s+unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)",
    re.MULTILINE,
)
NEEDS_SUDO_AT_A_TERMINAL = "the system step needs sudo at a terminal: run `hal setup --apply` in your own terminal window"


class CheckState(Enum):
    """What a step's check found; only the system step can end up not checked."""

    UNCHANGED = "unchanged"
    WOULD_CHANGE = "would change"
    NOT_CHECKED = "not checked"


NEEDS_A_TERMINAL = "sudo at a terminal"
NEEDS_ANSIBLE = "Ansible, which `hal setup --apply` installs first"


class Applied(Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"


class StepFailed(Exception):
    """A step that refused to run or did not finish; its message says why."""


@dataclass(frozen=True)
class PlaybookRun:
    succeeded: bool
    changed: int


PlaybookRunner = Callable[[Sequence[str]], PlaybookRun]


def read_recap(returncode: int, output: str) -> PlaybookRun:
    """A run succeeded when Ansible exited 0 and its recap counts no failed or unreachable host."""
    recap = RECAP.search(output)
    if recap is None:
        return PlaybookRun(False, 0)
    succeeded = returncode == 0 and recap["failed"] == "0" and recap["unreachable"] == "0"
    return PlaybookRun(succeeded, int(recap["changed"]))


def run_playbook(argv: Sequence[str]) -> PlaybookRun:
    """stdin and stderr stay the terminal's, for the become prompt; stdout is shown line by line and read for the recap.

    No timeout: a person types the sudo password, and without a terminal the step refuses before it gets here.
    """
    shown: list[str] = []
    with (
        tempfile.TemporaryDirectory(prefix="home-ansible-") as ansible_home,
        subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=_run_scoped_environment(Path(ansible_home))) as playbook,
    ):
        for line in playbook.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            shown.append(line)
    return read_recap(playbook.returncode, "".join(shown))


def _run_scoped_environment(ansible_home: Path) -> dict[str, str]:
    """Ansible keeps its state in the run's own directory, never in `~/.ansible`, which `home check` would flag.

    Plain output with the default callback, whatever the operator configured, keeps the recap readable.
    """
    return {
        **os.environ,
        "ANSIBLE_HOME": str(ansible_home),
        "ANSIBLE_LOCAL_TEMP": str(ansible_home / "tmp"),
        "ANSIBLE_REMOTE_TEMP": str(ansible_home / "remote-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_STDOUT_CALLBACK": "default",
    }


class UnresolvableRunningUser(ValueError):
    """The running uid has no passwd name, or `agent` shares it with another name; the role cannot be told from the name."""


def _running_user() -> str:
    uid = os.getuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        raise UnresolvableRunningUser(f"no passwd entry for uid {uid}") from None
    names = [entry.pw_name for entry in pwd.getpwall() if entry.pw_uid == uid]
    if len(names) > 1 and AGENT_USER in names:
        raise UnresolvableRunningUser(f"uid {uid} has the names {', '.join(names)}; {AGENT_USER} must have a uid of its own")
    return name


@dataclass(frozen=True)
class Boundaries:
    """The terminal the become prompt needs, Ansible itself, and who runs `home`."""

    has_terminal: Callable[[], bool]
    run_playbook: PlaybookRunner
    operator_user: Callable[[], str]


# The terminal fact is the one `home secrets --setup` asks: both ends are a terminal.
TERMINAL_BOUNDARIES = Boundaries(secrets_setup.TERMINAL_BOUNDARIES.terminal.is_interactive, run_playbook, _running_user)


@dataclass(frozen=True)
class SystemStep:
    layout: Layout
    boundaries: Boundaries

    def extra_vars(self) -> dict[str, str | int]:
        """The values the playbook fills in; the tunnel id it reads itself, as root, from cloudflared's config."""
        return {
            "home": str(self.layout.home),
            "operator_user": self.boundaries.operator_user(),
            "agent_user": AGENT_USER,
            "edge_tunnel_dir": str(self.layout.edge_tunnel_dir),
            "journal_vacuum_days": JOURNAL_VACUUM_DAYS,
        }

    def inputs_digest(self) -> str:
        """What a verified run rendered: the playbook, edge's `tunnel/` files and the filled-in values."""
        digest = hashlib.sha256(json.dumps(self.extra_vars(), sort_keys=True).encode())
        digest.update(b"\0" + PLAYBOOK.read_bytes())
        tunnel_dir = self.layout.edge_tunnel_dir
        for path in sorted(path for path in tunnel_dir.rglob("*") if path.is_file()):
            digest.update(b"\0" + str(path.relative_to(tunnel_dir)).encode() + b"\0" + path.read_bytes())
        return digest.hexdigest()

    def is_verified(self) -> bool:
        """`home next` reads this: a verified run rendered today's inputs. A hand edit in /etc only `hal setup` sees."""
        try:
            return self.layout.system_step_record.read_text() == self.inputs_digest()
        except OSError:
            return False

    def needs_first(self) -> str:
        """What the check needs before it can look, empty when it can: a terminal for sudo, then the engine to check with."""
        if not self.boundaries.has_terminal():
            return NEEDS_A_TERMINAL
        if not self.layout.ansible_playbook_executable.is_file():
            return NEEDS_ANSIBLE
        return ""

    def check(self) -> CheckState:
        """At a terminal, Ansible's check mode asks for sudo once and shows the diff; without what it needs nothing is read."""
        if self.needs_first():
            return CheckState.NOT_CHECKED
        digest = self.inputs_digest()
        if not (run := self._run_playbook(check=True)).succeeded:
            raise StepFailed("Ansible's check did not finish; its output above says why")
        if run.changed:
            return CheckState.WOULD_CHANGE
        self._record(digest)
        return CheckState.UNCHANGED

    def apply(self) -> Applied:
        if not self.boundaries.has_terminal():
            raise StepFailed(NEEDS_SUDO_AT_A_TERMINAL)
        digest = self.inputs_digest()
        # An apply may change the system, so an earlier verification no longer stands, whatever the outcome.
        self.layout.system_step_record.unlink(missing_ok=True)
        if not (run := self._run_playbook(check=False)).succeeded:
            raise StepFailed("Ansible did not finish; its output above says why, and no verified run is recorded")
        self._record(digest)
        return Applied.CHANGED if run.changed else Applied.UNCHANGED

    def _run_playbook(self, check: bool) -> PlaybookRun:
        argv = [
            str(self.layout.ansible_playbook_executable),
            str(PLAYBOOK),
            "--inventory",
            "localhost,",
            "--ask-become-pass",
            "--diff",
            "--extra-vars",
            json.dumps({**self.extra_vars(), "ansible_python_interpreter": "auto_silent"}),
            *(["--check"] if check else []),
        ]
        return self.boundaries.run_playbook(argv)

    def _record(self, digest: str) -> None:
        # A torn write only reads as "never verified", so the record needs no atomic replace.
        self.layout.system_step_record.parent.mkdir(parents=True, exist_ok=True)
        self.layout.system_step_record.write_text(digest)
