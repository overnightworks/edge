"""The realm step of `edge apply --closed`: applies a changed `keycloak/realm.yaml` or
`keycloak/master.yaml` to the running Keycloak after a Keycloak-only backup (#242, #235 rulings
R2-R4).

The step runs only when Keycloak is on, set up and running, and an input changed: the step's entry
in `applied.json` (under `keycloak-config-cli`) is missing, unfinished, or differs from the digests
of the checkout files config-cli reads and its resolved definition, or Keycloak's own
import-checksum attributes differ from the ones the entry saw after the last apply. A rotated
client secret alone does not trigger it: Compose's definition hash ignores the content of a
secret file.

An apply that runs takes `edge-backup backup keycloak --pre-apply`, proves that snapshot with
`restore-test keycloak --snapshot`, observes both realms, runs config-cli, observes again and
lists what changed. The entry is emptied before config-cli and filled only after the observe, so a
step that did not finish is planned again. Nothing is deleted but what config-cli resets to the
file; those removals say so.
"""

import json
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from edge import compose, core, switches
from edge import realm as realm_lines
from edge.realm import OBSERVER_SERVICE, Kind, new_digest_key
from edge.realm.observe import Inventory

REALM_UNIT = "keycloak"
CONFIG_CLI = "keycloak-config-cli"
# config-cli imports these from its directory bind of keycloak/, which `core.file_binds` skips.
REALM_FILES = ("keycloak/master.yaml", "keycloak/realm.yaml")
DEFINITION_KEY = "definition"  # the entry's key for config-cli's `compose config --hash`
BACKUP_SECONDS = 1800
STOP_GRACE_SECONDS = 120  # what edge-backup gets to remove its staging and restore container
RESTORE_GUIDE = 'docs/backup.md "Restore Keycloak"'
_SNAPSHOT_LINE = re.compile(r"^edge-backup: pre-apply-Snapshot (\S+)$", re.MULTILINE)

type Checksums = dict[str, dict[str, str]]  # realm -> import-checksum attribute -> value


class RealmError(Exception):
    """A part of the step failed; the message is one line without a secret."""


# --- What the plan decides ---------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    """What the realm files and config-cli's definition are now."""

    files: core.Digests
    definition: str

    @property
    def entry(self) -> dict[str, str]:
        return {**self.files, DEFINITION_KEY: self.definition}

    def changed_since(self, recorded: Mapping[str, str]) -> str | None:
        for name, digest in self.entry.items():
            if recorded.get(name) != digest:
                return (
                    "config-cli's definition changed"
                    if name == DEFINITION_KEY
                    else f"{name} changed"
                )
        return None


def bound_files(config: Mapping[str, Any], repository: Path) -> tuple[Path, ...]:
    """The checkout files config-cli reads: the realm files and every file it binds (its
    entrypoint)."""
    realm_files = tuple(repository / name for name in REALM_FILES)
    return (*realm_files, *core.file_binds(config["services"].get(CONFIG_CLI, {}), repository))


def read_inputs(engine: compose.Compose, config: Mapping[str, Any], repository: Path) -> Inputs:
    files = core.digests(bound_files(config, repository), repository)
    return Inputs(files, engine.config_hashes((CONFIG_CLI,))[CONFIG_CLI])


def _flat(checksums: Checksums) -> dict[str, str]:
    return {
        f"{realm} {attribute}": value
        for realm, attributes in checksums.items()
        for attribute, value in attributes.items()
    }


class RealmAction(StrEnum):
    NOTHING = "nothing"
    RUN = "run"
    HELD = "held"  # Keycloak is off or switched off: the changed files wait
    NOT_CHECKED = "not checked"  # Keycloak starts in this run
    UNREADABLE = "unreadable"  # Keycloak should run and cannot be read


@dataclass(frozen=True)
class RealmPlan:
    action: RealmAction = RealmAction.NOTHING
    line: str = ""  # what the plan says, empty when it says nothing
    entry: dict[str, str] = field(default_factory=dict)  # the inputs as planned, for the record

    @property
    def settled(self) -> bool:
        return self.action not in (RealmAction.RUN, RealmAction.UNREADABLE)

    @property
    def lines(self) -> list[str]:
        return [self.line] if self.line else []


class Observer:
    """The realm observer through its compose service (`edge.realm.observe`). One observer holds
    one digest key, so the inventories it reads before and after an apply can be compared."""

    def __init__(self, engine: compose.Compose) -> None:
        self.engine = engine
        self.digest_key = new_digest_key()

    def inventory(self) -> Inventory:
        return self._read([], self.digest_key + "\n")

    def checksums(self) -> Checksums:
        return self._read(["--import-checksums"], "")

    def _read(self, arguments: list[str], stdin: str) -> dict:
        try:
            return json.loads(self.engine.run_once(OBSERVER_SERVICE, arguments, stdin))
        except compose.ComposeError as error:
            raise RealmError(f"the realm observer failed ({error})") from error
        except ValueError as error:
            raise RealmError("the realm observer answered no JSON") from error


def plan_realm(
    engine: compose.Compose,
    observer: Observer,
    record: core.Record,
    config: Mapping[str, Any],
    repository: Path,
    gate: core.Gate | None,
    starts_now: bool,
) -> RealmPlan:
    """What the step does: `gate` is the Keycloak unit's, None when the catalog has none, and
    `starts_now` says this run switches it on."""
    if gate is None:
        return RealmPlan()
    if starts_now:
        line = "realm: not checked, Keycloak starts now; the next `hal apply` checks it"
        return RealmPlan(RealmAction.NOT_CHECKED, line)
    inputs = read_inputs(engine, config, repository)
    reading = record.read()
    if gate is not core.Gate.ON:
        if why := _why(reading, inputs, None):
            line = f"realm: {why}; Keycloak is {gate}, so it is not applied"
            return RealmPlan(RealmAction.HELD, line)
        return RealmPlan()
    try:
        observed = observer.checksums()
    except RealmError as error:
        line = f"realm: Keycloak could not be read; not checked ({error})"
        return RealmPlan(RealmAction.UNREADABLE, line)
    if why := _why(reading, inputs, observed):
        return RealmPlan(RealmAction.RUN, f"realm: apply keycloak/*.yaml ({why})", inputs.entry)
    return RealmPlan()


def _why(reading: core.Reading, inputs: Inputs, observed: Checksums | None) -> str | None:
    """Why the realm has to be applied; `observed` is None while Keycloak is not read."""
    if reading.problem:
        return reading.problem
    if (recorded := reading.entries.get(CONFIG_CLI)) is None:
        return "no realm apply is recorded"
    if reading.unfinished(CONFIG_CLI):
        return "the last realm apply did not finish"
    if why := inputs.changed_since(recorded):
        return why
    saw = {name: value for name, value in recorded.items() if name not in inputs.entry}
    if observed is not None and saw != _flat(observed):
        return "Keycloak's import checksum is not the recorded one"
    return None


# --- The step ----------------------------------------------------------------------------------


def run_edge_backup(
    command: list[str], timeout: int, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run edge-backup to its end. On the timeout or an interrupt it is stopped, not killed
    first: SIGTERM, then `STOP_GRACE_SECONDS` to remove its staging and restore container.

    It shares the terminal's process group, so a Ctrl-C reached it as SIGINT already."""
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=compose.REPOSITORY,
        ) as process:
            try:
                stdout, stderr = process.communicate(stdin, timeout)
            except subprocess.TimeoutExpired as error:
                _stop(process, (signal.SIGTERM,))
                raise compose.ComposeError(
                    f"`{' '.join(command[:3])}` took longer than {timeout}s"
                ) from error
            except KeyboardInterrupt:
                _stop(process, (None, signal.SIGTERM))
                raise
    except OSError as error:
        raise compose.ComposeError(f"cannot run {command[0]}: {error.strerror}") from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _stop(process: subprocess.Popen[str], signals: tuple[signal.Signals | None, ...]) -> None:
    """Each signal, then a grace to exit; a process still running after the last is killed. A
    further interrupt stops the waiting and kills."""
    try:
        for sent in signals:
            if sent is not None:
                process.send_signal(sent)
            try:
                process.communicate(timeout=STOP_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        pass
    process.kill()
    process.communicate()


@dataclass(frozen=True)
class Backup:
    """edge-backup as the timers run it, for Keycloak alone."""

    command: tuple[str, ...]
    run: compose.Runner = run_edge_backup

    @classmethod
    def installed(cls, repository: Path) -> "Backup":
        return cls((str(repository / ".venv" / "bin" / "edge-backup"),))

    def pre_apply(self) -> str:
        """The new snapshot's ID."""
        result = self._run(["backup", REALM_UNIT, "--pre-apply"], "the Keycloak backup failed")
        if not (named := _SNAPSHOT_LINE.search(result.stdout)):
            raise RealmError("edge-backup named no pre-apply snapshot")
        return named[1]

    def restore_test(self, snapshot: str) -> None:
        """Proves the snapshot just made, whatever a timer backs up in between."""
        arguments = ["restore-test", REALM_UNIT, "--snapshot", snapshot]
        self._run(arguments, "the restore test failed")

    def _run(self, arguments: list[str], failure: str) -> subprocess.CompletedProcess[str]:
        try:
            result = self.run([*self.command, *arguments], BACKUP_SECONDS)
        except compose.ComposeError as error:
            raise RealmError(f"{failure} ({error})") from error
        if result.returncode != 0:
            raise RealmError(f"{failure} ({compose.last_line(result.stderr + result.stdout)})")
        return result


class Stage(StrEnum):
    BEFORE = "before"  # nothing is applied yet
    APPLYING = "applying"  # config-cli started
    APPLIED = "applied"  # config-cli ended well; its result is not recorded yet


@dataclass
class _Progress:
    stage: Stage = Stage.BEFORE
    snapshot: str | None = None
    proven: bool = False


@dataclass(frozen=True)
class StepResult:
    """The lines to say, and why the step ended partial when it did."""

    lines: tuple[str, ...] = ()
    failure: str | None = None


@dataclass(frozen=True)
class Realm:
    """The step, with what it drives injected: Compose, the observer, the backup and the record."""

    engine: compose.Compose
    observer: Observer
    backup: Backup
    record: core.Record
    config: Mapping[str, Any]
    repository: Path
    marks: switches.Switches
    mark: switches.Mark | None  # Keycloak's marker as the plan read it
    clock: Callable[[], float] = time.monotonic

    def run(self, plan: RealmPlan) -> StepResult:
        """Alone decides whether the step acts: a settled plan has no effect."""
        if plan.settled:
            return StepResult()
        if plan.action is RealmAction.UNREADABLE:
            return StepResult(failure=plan.line)
        progress = _Progress()
        try:
            return self._apply(plan, progress)
        except (RealmError, compose.ComposeError, core.CoreError, switches.SwitchError) as error:
            return StepResult(failure=_failure(progress, str(error)))
        except KeyboardInterrupt:
            return StepResult(failure=_failure(progress, "interrupted"))

    def _apply(self, plan: RealmPlan, progress: _Progress) -> StepResult:
        followed = core.followed_services(self.config, self.repository)
        self._refuse_while_config_cli_runs()
        progress.snapshot, backup_seconds = self._timed(self.backup.pre_apply)
        _, test_seconds = self._timed(lambda: self.backup.restore_test(progress.snapshot))
        progress.proven = True
        before = self.observer.inventory()
        self._run_config_cli(progress, followed)
        after = self.observer.inventory()
        checksums = self.observer.checksums()
        timing = (
            f"realm: applied keycloak/*.yaml; pre-apply snapshot {progress.snapshot} "
            f"({backup_seconds:.0f}s backup, {test_seconds:.0f}s restore test)"
        )
        lines = (timing, *_change_lines(before, after, progress.snapshot))
        try:
            self.record.write(CONFIG_CLI, {**plan.entry, **_flat(checksums)}, followed)
        except core.CoreError as error:
            return StepResult(
                lines,
                f"realm: applied and observed, but the record could not be written ({error}); "
                "the next run repeats the step",
            )
        return StepResult(lines)

    def _timed[T](self, action: Callable[[], T]) -> tuple[T, float]:
        started = self.clock()
        return action(), self.clock() - started

    def _refuse_while_config_cli_runs(self) -> None:
        if compose.has_running_container(self.engine.states(), (CONFIG_CLI,)):
            raise RealmError("a config-cli container is still running")

    def _run_config_cli(self, progress: _Progress, followed: frozenset[str]) -> None:
        """Under the switch lock, so a marker set since the plan stops it before Keycloak could
        be touched; config-cli never starts Keycloak (`--no-deps`)."""
        with self.marks.lock():
            if self.marks.changed(REALM_UNIT, self.mark):
                raise RealmError(f"{REALM_UNIT} was switched off meanwhile; config-cli is not run")
            self.record.mark_unfinished([CONFIG_CLI], followed)
            progress.stage = Stage.APPLYING
            self.engine.run_once(CONFIG_CLI, [])
        progress.stage = Stage.APPLIED


def _failure(progress: _Progress, cause: str) -> str:
    snapshot = progress.snapshot
    if progress.stage is Stage.BEFORE:
        proof = "proven" if progress.proven else "unproven"
        kept = f"; pre-apply snapshot {snapshot} is {proof}" if snapshot else ""
        return f"realm: {cause}; nothing applied{kept}"
    done = (
        "the realm may be partly applied"
        if progress.stage is Stage.APPLYING
        else "the realm was applied but not observed"
    )
    return (
        f"realm: {cause}; {done}; pre-apply snapshot {snapshot}, the way back is {RESTORE_GUIDE}; "
        "the next run repeats the step"
    )


def _change_lines(before: Inventory, after: Inventory, snapshot: str | None) -> list[str]:
    """One line per change; a removal is config-cli resetting a console change to the file."""
    lines = []
    for change in realm_lines.diff(before, after):
        shown = realm_lines.line(change)
        if change.kind is Kind.REMOVAL:
            shown += f": reset to the file, pre-apply snapshot {snapshot}"
        lines.append(shown)
    return lines or ["realm: no changes"]
