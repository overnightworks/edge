"""The system step of `hal setup`: edge's cloudflared files and the stop-only polkit rule, written by Ansible as root under one sudo call.

The root work is `system/playbook.yml`. This module runs it with the terminal passed through, says what its check found in one
of three states.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from home_tools import secrets_setup
from home_tools.layout import Layout
from hal_output.command import Verb, command

PLAYBOOK = Path(__file__).parent / "system" / "playbook.yml"
# The agents' own OS account: the play creates it dormant, and `hal setup` skips the operator's steps for exactly this user.
AGENT_USER = "agent"
# The ruled bound on every server log on this PC: „ja 30 tage für alle“.
LOG_RETENTION_DAYS = 30
# The default log driver of Docker, which the play merges into daemon.json and `hal check` expects.
DOCKER_LOG_DRIVER = "journald"
# journalctl deletes whole archive files by the age of their oldest entry: one day for a file's span
# and one day of slack for a PC that is off at midnight keep every line under LOG_RETENTION_DAYS.
JOURNAL_VACUUM_DAYS = LOG_RETENTION_DAYS - 2
# The files rsyslog wrote: the play deletes them (and their rotations) once rsyslog is purged, and `hal check` reports them.
RSYSLOG_FILE_NAMES = ("syslog", "auth.log", "kern.log", "mail.log", "mail.err", "ufw.log")
# The last line of a play recap names the host and counts each outcome.
RECAP = re.compile(
    r"^\S+\s*:\s*ok=\d+\s+changed=(?P<changed>\d+)\s+unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)",
    re.MULTILINE,
)


class CheckState(Enum):
    """What a step's check found; only the system step can end up not checked."""

    UNCHANGED = "unchanged"
    WOULD_CHANGE = "would change"
    NOT_CHECKED = "not checked"


NEEDS_A_TERMINAL = "sudo at a terminal"
NEEDS_ANSIBLE = f"Ansible, which `{command(Verb.SETUP)}` installs first"


class Applied(Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"


class StepFailed(Exception):
    """A step that did not finish; its message says why."""


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


def _forget_the_sudo_ticket() -> None:
    try:
        subprocess.run(["sudo", "-k"], check=True)
    except subprocess.CalledProcessError as failure:
        raise StepFailed(f"sudo could not forget its ticket (exit {failure.returncode}) · it may still be valid") from failure


def run_playbook_as_root(argv: Sequence[str]) -> PlaybookRun:
    """Runs `argv` under one `sudo` in the packaged `system/` directory, then makes sudo forget its ticket.

    The system asks for the password at the terminal, which stays stdin and stderr's; stdout is shown line by line and read for
    the recap. No `ANSIBLE_*` variable reaches the play, so the packaged `ansible.cfg` there decides. `sudo -k` follows whatever
    the play did, and a failing `sudo -k` fails the step; it never follows a sudo that did not start.
    Root's Python writes no bytecode, which would leave root-owned files in the operator's tool environment.
    No timeout: a person types the password; the run asks its yes at a terminal first and `--dry-run` reaches here only when `needs_first` finds one.
    """
    environment = {name: value for name, value in os.environ.items() if not name.startswith("ANSIBLE_")}
    shown: list[str] = []
    with subprocess.Popen(["sudo", "env", "PYTHONDONTWRITEBYTECODE=1", *argv], stdout=subprocess.PIPE, text=True, cwd=PLAYBOOK.parent, env=environment) as playbook:
        try:
            for line in playbook.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                shown.append(line)
        finally:
            _forget_the_sudo_ticket()
    return read_recap(playbook.returncode, "".join(shown))


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
    """The terminal the sudo prompt needs, Ansible itself, and who runs `home`."""

    has_terminal: Callable[[], bool]
    run_playbook: PlaybookRunner
    operator_user: Callable[[], str]


# The terminal fact is the one the wizard asks: both ends are a terminal.
TERMINAL_BOUNDARIES = Boundaries(secrets_setup.TERMINAL_BOUNDARIES.terminal.is_interactive, run_playbook_as_root, _running_user)


@dataclass(frozen=True)
class SystemStep:
    layout: Layout
    boundaries: Boundaries

    def extra_vars(self) -> dict[str, str | int | tuple[str, ...]]:
        """The values the playbook fills in; the tunnel id it reads itself, as root, from cloudflared's config.

        sudo logs its whole argv, so nothing here may ever be a secret.
        """
        return {
            "home": str(self.layout.home),
            "operator_user": self.boundaries.operator_user(),
            "agent_user": AGENT_USER,
            "edge_tunnel_dir": str(self.layout.edge_tunnel_dir),
            "journal_vacuum_days": JOURNAL_VACUUM_DAYS,
            "docker_log_driver": DOCKER_LOG_DRIVER,
            "rsyslog_file_names": RSYSLOG_FILE_NAMES,
        }

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
        if not (run := self._run_playbook(check=True)).succeeded:
            raise StepFailed("Ansible's check did not finish · its output above says why")
        if run.changed:
            return CheckState.WOULD_CHANGE
        return CheckState.UNCHANGED

    def apply(self) -> Applied:
        if not (run := self._run_playbook(check=False)).succeeded:
            raise StepFailed("Ansible did not finish · its output above says why")
        return Applied.CHANGED if run.changed else Applied.UNCHANGED

    def _run_playbook(self, check: bool) -> PlaybookRun:
        argv = [
            str(self.layout.ansible_playbook_executable),
            str(PLAYBOOK),
            "--inventory",
            "localhost,",
            "--diff",
            "--extra-vars",
            json.dumps({**self.extra_vars(), "ansible_python_interpreter": "auto_silent"}),
            *(["--check"] if check else []),
        ]
        return self.boundaries.run_playbook(argv)
