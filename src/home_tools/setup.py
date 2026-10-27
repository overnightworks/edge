"""`hal setup`: quality, its engines and the global commit hook, then the user units, Ansible, the system step and the secrets.

The activation comes before the user units and the system step, so a wrong sudo password never keeps the commit hook from being active.
The user units come before the system step, which is the one that asks for sudo.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from shlex import join as shell_join
from shlex import quote
from typing import TYPE_CHECKING

from hal_output import Glyph, Row, Verdict, write_error, write_row, write_verdict
from home_tools import secrets_setup
from home_tools.secrets_view import ManifestError, Presence, SecretEntry
from home_tools.system_step import (
    AGENT_USER,
    Applied,
    CheckState,
    StepFailed,
    StepRefused,
    SystemStep,
    UnresolvableRunningUser,
)
from home_tools.config import Config
from home_tools.layout import CommandRunner, Layout
from home_tools.user_units import UnitsApplied, UserUnits

if TYPE_CHECKING:
    from home_tools.cli import Ports

# Quality v0.5.1. A commit SHA cannot move and needs no tag; a bump is a one-line change here.
QUALITY_SOURCE = "overnightworks-quality @ git+https://github.com/overnightworks/quality@d3a0466fc6dc87898431b722e9488921d729be42"
# The engine of the system step; a bump is a one-line change here.
ANSIBLE_SOURCE = "ansible-core==2.21.5"
HOOK_NAMES = ("pre-commit", "pre-merge-commit")
HOOK_TEMPLATE = Path(__file__).parent / "hooks" / "commit-hook"
HOOK_MODE = 0o755
HOOK_WHY = "without the hook no commit is checked for secrets"
UNITS_WHY = "without the links and the reload the timers run no unit of this checkout, or an older one"
SECRETS_WHY = "without them the services and the tunnel have no token, password or key to start with"
SECRETS_LEFT_OPEN = "not every secret is set up; the lines above say which one is open"
SYSTEM_WHY = (
    "without it the tunnel ignores edge's global stop after a reboot, and only root may stop it"
)
SUDO_WILL_BE_ASKED_FOR = "tunnel, polkit, journal, mail, agent account"
SKIPPED_FOR_THE_AGENT = "the operator's step"
RERUN = "hal setup --apply"


class SetupRow(StrEnum):
    """The four rows `hal setup` prints, in the order it runs them."""

    COMMIT_HOOK = "commit hook"
    TIMERS = "timers"
    SYSTEM = "system"
    SECRETS = "secrets"


class SetupEvent(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    WILL_CHANGE = "will change"
    NOT_CHECKED = "not checked"
    MISSING = "missing"
    STORED = "stored"
    COMPLETE = "complete"
    FAILED = "failed"
    NOT_STARTED = "not started"
    NOT_RUN = "not run"
    NEEDS_SUDO = "needs sudo"


class Tally(StrEnum):
    """What the verdict counts a row as."""

    CHANGED = "changed"
    OPEN = "open"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class EventLook:
    """`severity` ranks the events of one row's steps: the worst one is the row's."""

    glyph: Glyph
    severity: int
    tally: Tally | None


EVENT_LOOK: dict[SetupEvent, EventLook] = {
    SetupEvent.FAILED: EventLook(Glyph.FAILED, 5, None),
    SetupEvent.NOT_STARTED: EventLook(Glyph.FAILED, 5, None),
    SetupEvent.NOT_CHECKED: EventLook(Glyph.NEEDS_YOU, 4, Tally.OPEN),
    SetupEvent.MISSING: EventLook(Glyph.NEEDS_YOU, 4, Tally.OPEN),
    SetupEvent.CHANGED: EventLook(Glyph.DONE, 3, Tally.CHANGED),
    SetupEvent.STORED: EventLook(Glyph.DONE, 3, Tally.CHANGED),
    SetupEvent.WILL_CHANGE: EventLook(Glyph.CHANGE, 3, Tally.OPEN),
    SetupEvent.UNCHANGED: EventLook(Glyph.UNCHANGED, 2, Tally.UNCHANGED),
    SetupEvent.COMPLETE: EventLook(Glyph.UNCHANGED, 2, Tally.UNCHANGED),
    SetupEvent.SKIPPED: EventLook(Glyph.UNCHANGED, 1, Tally.SKIPPED),
    SetupEvent.NOT_RUN: EventLook(Glyph.UNCHANGED, 0, None),
    SetupEvent.NEEDS_SUDO: EventLook(Glyph.ANNOUNCE, 0, None),
}


@dataclass(frozen=True)
class Outcome:
    """What a step, or the worst of a row's steps, found or did; `fact` is the one sentence the row shows."""

    event: SetupEvent
    fact: str = ""

    def row(self, subject: SetupRow) -> Row:
        return Row(EVENT_LOOK[self.event].glyph, subject, self.event, self.fact)


@dataclass(frozen=True)
class StepEvents:
    """The words a step reports in; the secrets step says what its wizard did, not what a file did."""

    changed: SetupEvent = SetupEvent.CHANGED
    unchanged: SetupEvent = SetupEvent.UNCHANGED
    open: SetupEvent = SetupEvent.WILL_CHANGE


SECRETS_EVENTS = StepEvents(
    changed=SetupEvent.STORED, unchanged=SetupEvent.COMPLETE, open=SetupEvent.MISSING
)


def verdict_of(outcomes: Sequence[Outcome], apply: bool, elapsed_seconds: float) -> Verdict:
    tallies = Counter(EVENT_LOOK[outcome.event].tally for outcome in outcomes)
    counts = tuple((tallies[tally], tally) for tally in Tally)
    if apply:
        return Verdict(Glyph.DONE, "setup done", counts, elapsed_seconds=elapsed_seconds)
    if tallies[Tally.OPEN]:
        return Verdict(Glyph.CHANGE, "setup plan", counts, f"run {RERUN}", elapsed_seconds)
    return Verdict(Glyph.DONE, "setup up to date", counts, elapsed_seconds=elapsed_seconds)


class ExecutionRole(Enum):
    """Who runs `hal setup`: exactly the agents' account, or anyone else (root and a sudoers-only admin included)."""

    AGENT = "agent"
    OPERATOR = "operator"

    @classmethod
    def of(cls, running_user: str) -> ExecutionRole:
        return cls.AGENT if running_user == AGENT_USER else cls.OPERATOR


SETUP_EXAMPLES = """\
Examples:
  home setup           shows which steps would change something
  home setup --apply   runs them in order; the activation and the user units come before the system step, which asks once for sudo
"""


@dataclass(frozen=True)
class Step:
    """`is_verified` is all `home next` asks, so it never needs sudo; `check` is the dry run, `apply` the run.

    `row` is the line the step reports on; `failure` is the one sentence its failure shows when it raises nothing better,
    and `next_step` the command that goes on from there.
    `needs_first` names what a `not checked` dry run lacks; only the system step can end up there.
    `open_fact` names what a dry run that found something to change shows on its row.
    `apply` may return `UnitsApplied`, which adds the fact the row of a changed step shows.
    `before_apply` is the row printed before an apply that waits on the operator (the wizard, sudo's password), or nothing.
    `listed_by_next` is false for a step whose open work `home next` already lists from its own owner.
    `operators_only` marks a step the agents' account skips: it needs sudo, the operator's units or the operator's secrets.
    `may_leave_system_partial` marks the step that runs as root, whose failed apply leaves /etc half written.
    """

    name: str
    row: SetupRow
    is_verified: Callable[[], bool]
    check: Callable[[], CheckState]
    apply: Callable[[], Applied | UnitsApplied]
    why: str
    failure: str
    next_step: str = RERUN
    needs_first: Callable[[], str] = lambda: ""
    open_fact: Callable[[], str] = lambda: ""
    before_apply: Callable[[], Outcome | None] = lambda: None
    events: StepEvents = field(default_factory=StepEvents)
    listed_by_next: bool = True
    operators_only: bool = False
    may_leave_system_partial: bool = False

    def runs_for(self, role: ExecutionRole) -> bool:
        return not (self.operators_only and role is ExecutionRole.AGENT)

    def run(self, apply: bool) -> Outcome:
        if apply:
            done = self.apply()
            if isinstance(done, UnitsApplied):
                event = self.events.changed if done.applied is Applied.CHANGED else self.events.unchanged
                return Outcome(event, done.fact)
            return Outcome(self.events.changed if done is Applied.CHANGED else self.events.unchanged)
        match self.check():
            case CheckState.NOT_CHECKED:
                return Outcome(SetupEvent.NOT_CHECKED, f"needs {self.needs_first()}")
            case CheckState.WOULD_CHANGE:
                return Outcome(self.events.open, self.open_fact())
        return Outcome(self.events.unchanged)


HOOKS_PATH_KEY = "core.hooksPath"


def _local_step(
    name: str,
    row: SetupRow,
    is_current: Callable[[], bool],
    install: Callable[[], bool],
    why: str,
    failure: str,
    next_step: str = RERUN,
    *,
    operators_only: bool = False,
) -> Step:
    """A step in the user's own home: its check needs no password, so it is never `not checked`."""

    def check() -> CheckState:
        return CheckState.UNCHANGED if is_current() else CheckState.WOULD_CHANGE

    def apply() -> Applied:
        if is_current():
            return Applied.UNCHANGED
        if not install():
            raise StepFailed
        return Applied.CHANGED

    return Step(
        name, row, is_current, check, apply, why, failure, next_step, operators_only=operators_only
    )


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

    def open_names() -> str:
        return ", ".join(_open_name(entry, found) for entry, found in open_secrets())

    return Step(
        "Geheimnisse",
        SetupRow.SECRETS,
        is_verified,
        check,
        apply,
        SECRETS_WHY,
        SECRETS_LEFT_OPEN,
        open_fact=open_names,
        before_apply=lambda: Outcome(SetupEvent.MISSING, open_names()) if open_secrets() else None,
        events=SECRETS_EVENTS,
        listed_by_next=False,
        operators_only=True,
    )


def _open_name(entry: SecretEntry, found: Presence) -> str:
    if found is Presence.UNKNOWN:
        return f"{entry.name} (unknown)"
    return entry.name if entry.written_by_setup else f"{entry.name} (by hand)"


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


def _quality_steps(layout: Layout, run: CommandRunner) -> tuple[Step, ...]:
    quality, engines_dir = str(layout.quality_executable), str(layout.quality_engines_dir)

    def quality_current() -> bool:
        return _recorded_quality_source_matches(layout) and _is_executable(layout.quality_executable)

    def install_quality() -> bool:
        return run(["uv", "tool", "install", "--force", QUALITY_SOURCE]) is not None and _is_executable(layout.quality_executable)

    def engines_current() -> bool:
        return _is_executable(layout.quality_executable) and run([quality, "engines", "verify", engines_dir]) is not None

    def install_engines() -> bool:
        return run([quality, "engines", "install", engines_dir]) is not None and engines_current()

    return (
        _local_step(
            "quality",
            SetupRow.COMMIT_HOOK,
            quality_current,
            install_quality,
            HOOK_WHY,
            "uv could not install quality",
            shell_join(["uv", "tool", "install", "--force", QUALITY_SOURCE]),
        ),
        _local_step(
            "Engines",
            SetupRow.COMMIT_HOOK,
            engines_current,
            install_engines,
            HOOK_WHY,
            "the quality engines did not install",
            shell_join([quality, "engines", "install", engines_dir]),
        ),
    )


def _hook_steps(layout: Layout, run: CommandRunner) -> tuple[Step, ...]:
    hook_content = render_hook(layout)
    hook_paths = [layout.git_hooks_dir / name for name in HOOK_NAMES]

    def hook_current(path: Path) -> bool:
        return _has_hook_mode(path) and path.read_text() == hook_content

    def write_hooks() -> bool:
        for path in hook_paths:
            if not hook_current(path):
                _write_atomically(path, hook_content)
        return True

    def hooks_active() -> bool:
        return (run(["git", "config", "--global", "--get", HOOKS_PATH_KEY]) or "").strip() == str(layout.git_hooks_dir)

    def activate_hooks() -> bool:
        return run(["git", "config", "--global", HOOKS_PATH_KEY, str(layout.git_hooks_dir)]) is not None

    return (
        _local_step(
            "Hook-Dateien",
            SetupRow.COMMIT_HOOK,
            lambda: all(hook_current(path) for path in hook_paths),
            write_hooks,
            HOOK_WHY,
            "could not write the hook files",
        ),
        _local_step(
            "Aktivierung",
            SetupRow.COMMIT_HOOK,
            hooks_active,
            activate_hooks,
            HOOK_WHY,
            "git could not set core.hooksPath",
            shell_join(["git", "config", "--global", HOOKS_PATH_KEY, str(layout.git_hooks_dir)]),
        ),
    )


def _system_steps(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    run = ports.run
    system = SystemStep(layout, ports.system)
    units = UserUnits(layout, ports.unit_directory, run)
    system_row = SetupRow.SYSTEM

    def ansible_current() -> bool:
        return _recorded_ansible_source_matches(layout) and _is_executable(layout.ansible_playbook_executable)

    def install_ansible() -> bool:
        return run(["uv", "tool", "install", "--force", ANSIBLE_SOURCE]) is not None and ansible_current()

    return (
        Step(
            "User units",
            SetupRow.TIMERS,
            units.is_verified,
            units.check,
            units.apply,
            UNITS_WHY,
            "the user units did not come up",
            operators_only=True,
        ),
        _local_step(
            "Ansible",
            system_row,
            ansible_current,
            install_ansible,
            SYSTEM_WHY,
            "uv could not install Ansible",
            shell_join(["uv", "tool", "install", "--force", ANSIBLE_SOURCE]),
            operators_only=True,
        ),
        Step(
            "System",
            system_row,
            system.is_verified,
            system.check,
            system.apply,
            SYSTEM_WHY,
            "the system step did not finish",
            needs_first=system.needs_first,
            before_apply=lambda: (
                Outcome(SetupEvent.NEEDS_SUDO, SUDO_WILL_BE_ASKED_FOR) if system.will_ask_for_sudo() else None
            ),
            operators_only=True,
            may_leave_system_partial=True,
        ),
    )


def _steps(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    return (*_quality_steps(layout, ports.run), *_hook_steps(layout, ports.run), *_system_steps(layout, ports), _secrets_step(layout, ports))


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


def _failure_sentence(step: Step, error: OSError | StepFailed) -> str:
    if isinstance(error, StepFailed):
        return str(error) or step.failure
    return f"{step.failure}: {error.strerror or type(error).__name__}"


def _stopped(row: SetupRow, step: Step, event: SetupEvent, apply: bool) -> None:
    """The error pair after a failed row: what stopped, what /etc is left as, and the command that goes on."""
    leaves = (
        "/etc may be partial"
        if step.may_leave_system_partial and apply and event is SetupEvent.FAILED
        else "/etc unchanged"
    )
    write_error(
        f"setup stopped at {row}" + (f" · {leaves}" if row is SetupRow.SYSTEM else ""),
        step.next_step,
    )


class _RowStopped(Exception):
    def __init__(self, step: Step, error: OSError | StepFailed) -> None:
        super().__init__(str(error))
        self.step = step
        self.error = error


def _run_row(row: SetupRow, steps: tuple[Step, ...], role: ExecutionRole, apply: bool) -> Outcome:
    outcomes: list[Outcome] = []
    for step in (step for step in steps if step.row is row):
        if not step.runs_for(role):
            outcomes.append(Outcome(SetupEvent.SKIPPED, SKIPPED_FOR_THE_AGENT))
            continue
        try:
            if apply and (announced := step.before_apply()):
                write_row(announced.row(row))
            outcomes.append(step.run(apply))
        except (OSError, StepFailed) as error:
            raise _RowStopped(step, error) from error
    return max(outcomes, key=lambda candidate: EVENT_LOOK[candidate.event].severity)


def _stop_after_failure(rows: list[SetupRow], position: int, stopped: _RowStopped, apply: bool) -> None:
    event = SetupEvent.NOT_STARTED if isinstance(stopped.error, StepRefused) else SetupEvent.FAILED
    write_row(Outcome(event, _failure_sentence(stopped.step, stopped.error)).row(rows[position]))
    for later in rows[position + 1 :]:
        write_row(Outcome(SetupEvent.NOT_RUN).row(later))
    _stopped(rows[position], stopped.step, event, apply)


def setup(layout: Layout, ports: Ports, apply: bool) -> int:
    """Stops at the first failed step, so a missing scanner never gets a hook activated; the later rows say `not run`."""
    try:
        role = _role(ports)
    except UnresolvableRunningUser as error:
        write_error(str(error))
        return 1
    started = ports.clock()
    steps = _steps(layout, ports)
    outcomes: list[Outcome] = []
    rows = list(SetupRow)
    for position, row in enumerate(rows):
        try:
            outcome = _run_row(row, steps, role, apply)
        except _RowStopped as stopped:
            _stop_after_failure(rows, position, stopped, apply)
            return 1
        write_row(outcome.row(row))
        outcomes.append(outcome)
    verdict = verdict_of(outcomes, apply, ports.clock() - started)
    write_verdict(verdict)
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
        help="set up quality, the engines and the global commit hook, then the user units, the system step and the secrets",
        description=(
            "Shows what is missing, or with --apply sets up quality, the engines and the hook."
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
    parser.add_argument("--apply", action="store_true", help="run the steps instead of only showing them")
    parser.set_defaults(handler=_setup_from_args)
