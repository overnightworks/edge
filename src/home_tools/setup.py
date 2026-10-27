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

from hal_output import Glyph, Row, write_error, write_row, write_verdict
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
SYSTEM_WHY = (
    "without it the tunnel ignores edge's global stop after a reboot, and only root may stop it"
)
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


@dataclass(frozen=True)
class SetupVerdict:
    """The last line of a run, computed from the rows it printed and never read back from their glyphs."""

    glyph: Glyph
    summary: str
    counts: tuple[tuple[int, Tally], ...]
    run_next: str = ""

    @property
    def detail(self) -> str:
        counted = ", ".join(f"{number} {tally}" for number, tally in self.counts)
        return f"{counted} · {self.run_next}" if self.run_next else counted


def verdict_of(outcomes: Sequence[Outcome], apply: bool) -> SetupVerdict:
    tallies = Counter(EVENT_LOOK[outcome.event].tally for outcome in outcomes)
    counts = tuple((tallies[tally], tally) for tally in Tally if tallies[tally])
    if apply:
        return SetupVerdict(Glyph.DONE, "setup done", counts)
    if tallies[Tally.OPEN]:
        return SetupVerdict(Glyph.CHANGE, "setup plan", counts, f"run {RERUN}")
    return SetupVerdict(Glyph.DONE, "setup up to date", counts)


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

    `row` is the line the step reports on; `failure` is the one sentence its failure shows when it raises nothing better,
    and `next_step` the command that goes on from there.
    `needs_first` names what a `not checked` dry run lacks; only the system step can end up there.
    `open_fact` names what a dry run that found something to change shows on its row.
    `details` are the lines printed under the row of an applied step.
    `listed_by_next` is false for a step whose open work `home next` already lists from its own owner.
    `operators_only` marks a step the agents' account skips: it needs sudo, the operator's units or the operator's secrets.
    `may_leave_system_partial` marks the step that runs as root, whose failed apply leaves /etc half written.
    """

    name: str
    row: SetupRow
    is_verified: Callable[[], bool]
    check: Callable[[], CheckState]
    apply: Callable[[], Applied]
    why: str
    failure: str
    next_step: str = RERUN
    needs_first: Callable[[], str] = lambda: ""
    open_fact: Callable[[], str] = lambda: ""
    details: Callable[[], list[str]] = lambda: []
    events: StepEvents = field(default_factory=StepEvents)
    listed_by_next: bool = True
    operators_only: bool = False
    may_leave_system_partial: bool = False

    def runs_for(self, role: ExecutionRole) -> bool:
        return not (self.operators_only and role is ExecutionRole.AGENT)

    def run(self, apply: bool) -> Outcome:
        if apply:
            return Outcome(
                self.events.changed if self.apply() is Applied.CHANGED else self.events.unchanged
            )
        match self.check():
            case CheckState.NOT_CHECKED:
                return Outcome(SetupEvent.NOT_CHECKED, f"needs {self.needs_first()}")
            case CheckState.WOULD_CHANGE:
                return Outcome(self.events.open, self.open_fact())
            case CheckState.UNCHANGED:
                return Outcome(self.events.unchanged)


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

    hook, timers, system_row = SetupRow.COMMIT_HOOK, SetupRow.TIMERS, SetupRow.SYSTEM
    return (
        _local_step(
            "quality",
            hook,
            quality_current,
            install_quality,
            HOOK_WHY,
            "uv could not install quality",
            shell_join(["uv", "tool", "install", "--force", QUALITY_SOURCE]),
        ),
        _local_step(
            "Engines",
            hook,
            engines_current,
            install_engines,
            HOOK_WHY,
            "the quality engines did not install",
            shell_join([quality, "engines", "install", engines_dir]),
        ),
        _local_step(
            "Hook-Dateien",
            hook,
            lambda: all(hook_current(path) for path in hook_paths),
            write_hooks,
            HOOK_WHY,
            "could not write the hook files",
        ),
        _local_step(
            "Aktivierung",
            hook,
            hooks_active,
            activate_hooks,
            HOOK_WHY,
            "git could not set core.hooksPath",
            shell_join(["git", "config", "--global", "core.hooksPath", str(layout.git_hooks_dir)]),
        ),
        Step(
            "User units",
            timers,
            units.is_verified,
            units.check,
            units.apply,
            UNITS_WHY,
            "the user units did not come up",
            details=units.loaded_command_lines,
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
            operators_only=True,
            may_leave_system_partial=True,
        ),
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


def setup(layout: Layout, ports: Ports, apply: bool) -> int:
    """Stops at the first failed step, so a missing scanner never gets a hook activated; the later rows say `not run`."""
    try:
        role = _role(ports)
    except UnresolvableRunningUser as error:
        write_error(str(error))
        return 1
    steps = _steps(layout, ports)
    outcomes: list[Outcome] = []
    rows = list(SetupRow)
    for position, row in enumerate(rows):
        row_outcomes: list[Outcome] = []
        row_details: list[str] = []
        for step in (step for step in steps if step.row is row):
            if not step.runs_for(role):
                row_outcomes.append(Outcome(SetupEvent.SKIPPED, SKIPPED_FOR_THE_AGENT))
                continue
            try:
                row_outcomes.append(step.run(apply))
                if apply:
                    row_details.extend(step.details())
            except (OSError, StepFailed) as error:
                event = (
                    SetupEvent.NOT_STARTED if isinstance(error, StepRefused) else SetupEvent.FAILED
                )
                write_row(Outcome(event, _failure_sentence(step, error)).row(row))
                for later in rows[position + 1 :]:
                    write_row(Outcome(SetupEvent.NOT_RUN).row(later))
                _stopped(row, step, event, apply)
                return 1
        outcome = max(row_outcomes, key=lambda candidate: EVENT_LOOK[candidate.event].severity)
        write_row(outcome.row(row))
        for line in row_details:
            print(line)
        outcomes.append(outcome)
    verdict = verdict_of(outcomes, apply)
    write_verdict(verdict.glyph, verdict.summary, verdict.detail)
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
