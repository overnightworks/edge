"""`hal setup`: quality, its engines and the global commit hook, then the user units, Ansible, the system step and the secrets.

The activation comes before the user units and the system step, so a wrong sudo password never keeps the commit hook from being active.
The user units come before the system step, which is the one that asks for sudo.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING

from home_tools import secrets_setup
from home_tools.secrets_view import ManifestError, Presence, SecretEntry
from home_tools.system_step import AGENT_USER, Applied, CheckState, StepFailed, SystemStep, UnresolvableRunningUser
from home_tools.tidy_home import Config
from home_tools.layout import Layout
from home_tools.user_units import UserUnits

if TYPE_CHECKING:
    from home_tools.cli import Ports

# Quality v0.5.1. A commit SHA cannot move and needs no tag; a bump is a one-line change here.
QUALITY_SOURCE = "overnightworks-quality @ git+https://github.com/overnightworks/quality@d3a0466fc6dc87898431b722e9488921d729be42"
# The engine of the system step; a bump is a one-line change here.
ANSIBLE_SOURCE = "ansible-core==2.21.5"
HOOK_NAMES = ("pre-commit", "pre-merge-commit")
HOOK_TEMPLATE = Path(__file__).parent / "hooks" / "commit-hook"
HOOK_MODE = 0o755
HOOK_WHY = "ohne den Hook prüft kein Commit auf Geheimnisse"
UNITS_WHY = "without the links and the reload the timers run no unit of this checkout, or an older one"
SECRETS_WHY = "without them the services and the tunnel have no token, password or key to start with"
SECRETS_LEFT_OPEN = "not every secret is set up; the lines above say which one is open"
SYSTEM_WHY = "without it the tunnel ignores edge's global stop after a reboot, and only root may stop it"
SKIPPED_FOR_THE_AGENT = "skipped: the operator's step"


class ExecutionRole(Enum):
    """Who runs `hal setup`: exactly the agents' account, or anyone else (root and a sudoers-only admin included)."""

    AGENT = "agent"
    OPERATOR = "operator"

    @classmethod
    def of(cls, running_user: str) -> ExecutionRole:
        return cls.AGENT if running_user == AGENT_USER else cls.OPERATOR


SETUP_EXAMPLES = """\
Beispiele:
  home setup           zeigt, welche Schritte etwas ändern würden
  home setup --apply   führt sie der Reihe nach aus; the activation and the user units come before the system step, which asks once for sudo
"""


@dataclass(frozen=True)
class Step:
    """`is_verified` is all `home next` asks, so it never needs sudo; `check` is the dry run, `apply` the run.

    `needs_first` names what a `not checked` dry run lacks; only the system step can end up there.
    `details` are the lines printed under the heading of an applied step.
    `would_change` are the lines printed under the heading of a dry run that found something to change.
    `listed_by_next` is false for a step whose open work `home next` already lists from its own owner.
    `operators_only` marks a step the agents' account skips: it needs sudo, the operator's units or the operator's secrets.
    """

    name: str
    is_verified: Callable[[], bool]
    check: Callable[[], CheckState]
    apply: Callable[[], Applied]
    why: str
    needs_first: Callable[[], str] = lambda: ""
    details: Callable[[], list[str]] = lambda: []
    would_change: Callable[[], list[str]] = lambda: []
    listed_by_next: bool = True
    operators_only: bool = False

    def runs_for(self, role: ExecutionRole) -> bool:
        return not (self.operators_only and role is ExecutionRole.AGENT)


def _local_step(name: str, is_current: Callable[[], bool], install: Callable[[], bool], why: str, *, operators_only: bool = False) -> Step:
    """A step in the user's own home: its check needs no password, so it is never `not checked`."""

    def check() -> CheckState:
        return CheckState.UNCHANGED if is_current() else CheckState.WOULD_CHANGE

    def apply() -> Applied:
        if is_current():
            return Applied.UNCHANGED
        if not install():
            raise StepFailed
        return Applied.CHANGED

    return Step(name, is_current, check, apply, why, operators_only=operators_only)


def _secrets_step(layout: Layout, ports: Ports) -> Step:
    """The wizard as a step: verified by `stat` when no live secret is open or undeterminable, applied through the wizard itself."""
    boundaries = ports.secrets_setup

    def open_secrets() -> list[tuple[SecretEntry, Presence]]:
        try:
            return secrets_setup.open_entries(layout)
        except ManifestError as error:
            raise StepFailed(str(error)) from None

    def is_verified() -> bool:
        return not open_secrets()

    def check() -> CheckState:
        return CheckState.UNCHANGED if is_verified() else CheckState.WOULD_CHANGE

    def apply() -> Applied:
        if is_verified():
            return Applied.UNCHANGED
        exit_code = secrets_setup.run_setup(layout, boundaries, ports.now)
        if exit_code == secrets_setup.ABORTED_EXIT:
            raise KeyboardInterrupt  # the wizard has said so; the caller's own handling gives the exit code
        if exit_code != 0:
            raise StepFailed(SECRETS_LEFT_OPEN)
        return Applied.CHANGED

    def open_lines() -> list[str]:
        return [f"  {_open_label(entry, found)}: {entry.name}" for entry, found in open_secrets()]

    return Step("Geheimnisse", is_verified, check, apply, SECRETS_WHY, would_change=open_lines, listed_by_next=False, operators_only=True)


def _open_label(entry: SecretEntry, found: Presence) -> str:
    if found is Presence.UNKNOWN:
        return "nicht feststellbar"
    return "fehlt" if entry.written_by_setup else "von Hand"


def render_hook(layout: Layout) -> str:
    template = HOOK_TEMPLATE.read_text()
    return template.replace("@QUALITY@", quote(str(layout.quality_executable))).replace("@ENGINES@", quote(str(layout.quality_engines_dir)))


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _has_hook_mode(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o777 == HOOK_MODE


def _uv_recorded(receipt_path: Path, requirement: Mapping[str, str]) -> bool:
    """Whether uv's receipt for a tool records this requirement, so a pin that moved reinstalls the tool."""
    try:
        receipt = tomllib.loads(receipt_path.read_text())
    except (OSError, ValueError):
        return False
    recorded = receipt.get("tool", {}).get("requirements", [])
    return any(all(entry.get(key) == value for key, value in requirement.items()) for entry in recorded)


def _recorded_quality_source_matches(layout: Layout) -> bool:
    """uv records a git requirement as `<url>?rev=<sha>`."""
    name, _, source = QUALITY_SOURCE.partition(" @ ")
    url, _, revision = source.removeprefix("git+").rpartition("@")
    return _uv_recorded(layout.quality_uv_receipt, {"name": name, "git": f"{url}?rev={revision}"})


def _recorded_ansible_source_matches(layout: Layout) -> bool:
    """uv records a pinned version as its specifier."""
    name, _, version = ANSIBLE_SOURCE.partition("==")
    return _uv_recorded(layout.ansible_uv_receipt, {"name": name, "specifier": f"=={version}"})


def _write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(content)
    temporary.chmod(HOOK_MODE)
    temporary.replace(path)


def _steps(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    run = ports.run
    quality, engines_dir = str(layout.quality_executable), str(layout.quality_engines_dir)
    hook_content = render_hook(layout)
    hook_paths = [layout.git_hooks_dir / name for name in HOOK_NAMES]
    system = SystemStep(layout, ports.system)
    units = UserUnits(layout, ports.unit_directory, run)

    def quality_current() -> bool:
        return _recorded_quality_source_matches(layout) and _is_executable(layout.quality_executable)

    def install_quality() -> bool:
        return run(["uv", "tool", "install", "--force", QUALITY_SOURCE]) is not None and _is_executable(layout.quality_executable)

    def engines_current() -> bool:
        return _is_executable(layout.quality_executable) and run([quality, "engines", "verify", engines_dir]) is not None

    def install_engines() -> bool:
        return run([quality, "engines", "install", engines_dir]) is not None and engines_current()

    def hook_current(path: Path) -> bool:
        return _has_hook_mode(path) and path.read_text() == hook_content

    def write_hooks() -> bool:
        for path in hook_paths:
            if not hook_current(path):
                _write_atomically(path, hook_content)
        return True

    def hooks_active() -> bool:
        return (run(["git", "config", "--global", "--get", "core.hooksPath"]) or "").strip() == str(layout.git_hooks_dir)

    def activate_hooks() -> bool:
        return run(["git", "config", "--global", "core.hooksPath", str(layout.git_hooks_dir)]) is not None

    def ansible_current() -> bool:
        return _recorded_ansible_source_matches(layout) and _is_executable(layout.ansible_playbook_executable)

    def install_ansible() -> bool:
        return run(["uv", "tool", "install", "--force", ANSIBLE_SOURCE]) is not None and ansible_current()

    return (
        _local_step("quality", quality_current, install_quality, HOOK_WHY),
        _local_step("Engines", engines_current, install_engines, HOOK_WHY),
        _local_step("Hook-Dateien", lambda: all(hook_current(path) for path in hook_paths), write_hooks, HOOK_WHY),
        _local_step("Aktivierung", hooks_active, activate_hooks, HOOK_WHY),
        Step("User units", units.is_verified, units.check, units.apply, UNITS_WHY, details=units.loaded_command_lines, operators_only=True),
        _local_step("Ansible", ansible_current, install_ansible, SYSTEM_WHY, operators_only=True),
        Step("System", system.is_verified, system.check, system.apply, SYSTEM_WHY, system.needs_first, operators_only=True),
        _secrets_step(layout, ports),
    )


def _role(ports: Ports) -> ExecutionRole:
    return ExecutionRole.of(ports.system.operator_user())


def runs_as_agent(ports: Ports) -> bool:
    """A running user the role cannot be told from (no passwd name, or `agent` sharing its uid) is not the agent; only setup itself reports that."""
    try:
        return _role(ports) is ExecutionRole.AGENT
    except UnresolvableRunningUser:
        return False


def apply_asks_for_sudo(ports: Ports) -> bool:
    """`--apply` runs every step, a verified one too, and the system step always asks for sudo at a terminal; the agent skips it.

    The one fact `home next` reads for this command, as it reads `secrets_setup.SETUP_NEEDS_A_TERMINAL` for the wizard.
    """
    return not runs_as_agent(ports)


def pending(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    """The open steps `home next` lists from `hal setup --apply`'s plan; it never asks for sudo.

    The secrets step is left out: `home next` lists the missing secrets from the view, one wizard step each.
    """
    return tuple(step for step in _steps(layout, ports) if step.runs_for(_role(ports)) and step.listed_by_next and not step.is_verified())


def _outcome(step: Step, apply: bool) -> str:
    if apply:
        return step.apply().value
    state = step.check()
    needs = step.needs_first() if state is CheckState.NOT_CHECKED else ""
    return f"{state.value} (needs {needs})" if needs else state.value


def _lines_under_heading(step: Step, apply: bool, outcome: str) -> list[str]:
    if apply:
        return step.details()
    return step.would_change() if outcome == CheckState.WOULD_CHANGE.value else []


def setup(layout: Layout, ports: Ports, apply: bool) -> int:
    """Stops at the first failed step, so a missing scanner never gets a hook activated."""
    try:
        role = _role(ports)
    except UnresolvableRunningUser as error:
        print(f"hal setup: {error}", file=sys.stderr)
        return 1
    for step in _steps(layout, ports):
        if not step.runs_for(role):
            print(f"{step.name}: {SKIPPED_FOR_THE_AGENT}")
            continue
        try:
            outcome = _outcome(step, apply)
            print(f"{step.name}: {outcome}")
            for line in _lines_under_heading(step, apply, outcome):
                print(line)
        except (OSError, StepFailed) as error:
            if str(error):
                print(f"hal setup: {error}", file=sys.stderr)
            print(f"hal setup: Schritt „{step.name}“ ist fehlgeschlagen; die späteren Schritte sind nicht gelaufen", file=sys.stderr)
            return 1
    return 0


def _setup_from_args(args: argparse.Namespace, layout: Layout, _config: Config, ports: Ports) -> int:
    try:
        return setup(layout, ports, args.apply)
    except KeyboardInterrupt:
        # A Ctrl-C in any step ends with 130; only the wizard prints its abort line.
        return secrets_setup.ABORTED_EXIT


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "setup",
        help="quality, Engines und den globalen Commit-Hook einrichten, then the user units, the system step and the secrets",
        description=(
            "Zeigt, was fehlt, oder richtet mit --apply quality, die Engines und den Hook ein."
            " Then it links, reloads and enables the checkout's user units (the backup and home-check timers),"
            " installs Ansible and runs the system step, edge's cloudflared files and the stop-only polkit rule,"
            " which asks once for sudo at a terminal; the activation comes before the system step."
            " The system step makes the journal Docker's default log driver and restarts Docker while its default"
            " driver is not journald: every container stops meanwhile, and one without a restart policy stays down."
            " Last it walks you through every missing secret at the terminal, never showing a value."
        ),
        epilog=SETUP_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="führt die Schritte aus, statt sie nur zu zeigen")
    parser.set_defaults(handler=_setup_from_args)
