"""`edge apply --closed`: switches the units of this repository on or off from `fleet.yaml` and its
apps on at their newest release (`apps.py`).

Exit 0 applied and observed, or nothing to change; 1 refused, aborted, failed or partial;
2 cannot plan. The core (socket-proxy and traefik) comes first: a core service whose compose
definition changed (Compose's `config --hash` against its container's label) or whose recorded
file, today `traefik.yml`, changed since it was last applied is recreated together with the core
services that depend on it, socket-proxy first; each is observed running with what it depends on
and every active barrier proven again before its record is written and its success is said; one
without a live container is named and never started (#85, #87). After the core, a follower
(today traefik-mcp, a profile service binding a file at Traefik's static-config path) whose file or
definition changed is recreated alone while its unit is on, under the switch lock so a marker
stops it, and recorded once observed running; while its unit is off or switched off it is named
and nothing starts, and one that starts with its unit is recorded once the unit is on (#89).
A unit is a compose profile: on starts its services, off removes their containers. Volumes are never
touched. A live service of a unit not planned off whose container runs another image than
compose.yaml now names is named after the unit lines and keeps running: its image is never swapped
here, and the line alone asks nothing (#91). A unit that would be switched on, or is enabled and
already runs, and is not set up is asked about separately; on `yes` its own setup command runs in
the operator's terminal (key material is made there, never here) and `--check` must then pass
before a fresh plan is shown. A unit with an emergency switch (`edge stop`) or a barrier a stop
left is never started and its switch is never cleared here: only `edge start` ends it. If it is
enabled in the catalog, declares a setup, and its containers still run while it is not set up,
the run is refused: nothing is asked or published.
An app (an entry with `repo`) is planned last, in the same plan and under the same `yes`: its
newest release tag is checked out, built, started and recorded once healthy (`apps.py`).

Before any of that it guards, in this order, each refusing with its own lines and nothing else
(#95): a terminal (exit 1); the live checkout `~/git/edge`, since from any other tree it would bind
the live compose project to that tree (#79, exit 2); `apply.lock` in the state directory, held
non-blocking for the whole run, so a second apply is refused (exit 1) while `edge stop` and
`edge start` never wait for it; HEAD on `main` (exit 2); a clean tree, every changed path named
(exit 2), since a change reaches the live checkout through a pull request; and its `.env`, which
is written when missing and otherwise named and left alone when it differs (exit 2, #136).

Then it fetches origin's `main` through the operator's credential helper and pins its sha (#98): a
failed fetch is exit 2 with git's last error line; a checkout ahead of or beside it is exit 2,
nothing reset; an equal one says it is current. Behind, every waiting commit is shown, then its
files by what an update does with them (from the binds of the resolved configuration), then how
to bring them in by hand; the plan that follows applies HEAD, never the waiting commits. Nothing
in the checkout changes: only `.git` gains the fetched objects, FETCH_HEAD and `origin/main`.
"""

import fcntl
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from edge import barrier, catalog, checkout, compose, core, environment, setup, switches, terminal
from edge.apps import AppError, AppRuntime, AppsPlan, plan_apps
from edge.realm import reconcile
from edge.unit_plans import (
    Action,
    Fleet,
    KeptImage,
    Progress,
    UnitPlan,
    plan_kept_images,
    plan_switches,
    read_fleet,
    since_of,
    switch,
    unobserved,
)
from edge.waiting import Waiting, fetched

EnsureEnvironment = Callable[[Path], str | None]
COMMAND = "edge apply --closed"
APPLY_LOCK = "apply.lock"
_PRIVATE_DIRECTORY = 0o700
_PRIVATE_FILE = 0o600
# The emergency command's name; `emergency` owns the verb but imports this module, so it
# cannot be imported here.
EMERGENCY_STOP = "edge stop --all"


class ApplyExit(IntEnum):
    APPLIED_OR_NOTHING = 0
    REFUSED = 1
    CANNOT_PLAN = 2


CANNOT_PLAN_ERRORS = (
    compose.ComposeError,
    catalog.CatalogError,
    barrier.BarrierError,
    core.CoreError,
    setup.SetupUnclear,
    switches.SwitchError,
    AppError,
    checkout.GitError,
)


@dataclass(frozen=True)
class Plan:
    fleet: Fleet
    read: Mapping[str, switches.Mark | None]
    core: list[core.CorePlan]
    followers: list[core.FollowerPlan]
    plans: list[UnitPlan]
    images: list[KeptImage]
    realm: reconcile.RealmPlan
    app_plans: AppsPlan

    @property
    def changes_nothing(self) -> bool:
        return (
            self.realm.settled
            and all(plan.action is Action.STAYS_STOPPED for plan in self.plans)
            and all(plan.action is core.CoreAction.NOT_STARTED for plan in self.core)
            and all(plan.action is core.FollowerAction.HELD for plan in self.followers)
            and self.app_plans.changes_nothing
        )

    def describe(self) -> list[str]:
        plans = (*self.core, *self.followers, *self.plans, *self.images)
        return [
            *(plan.describe() for plan in plans),
            *self.realm.lines,
            *self.app_plans.describe(),
        ]


@dataclass(frozen=True)
class MissingSetup:
    unit: catalog.Unit
    check_line: str
    kept_stopped: bool = False


def _plan(
    engine: compose.Compose,
    marks: switches.Switches,
    record: core.Record,
    repository: Path,
    app_runtime: AppRuntime,
) -> Plan:
    fleet = read_fleet(engine, repository)
    read = {unit.key: marks.mark(unit.key) for unit in fleet.units}
    switched_off = _switched_off(fleet, read, repository)
    gates = _gates(fleet.units, switched_off)
    followers = core.follower_units(fleet.config, gates)
    hashes = engine.config_hashes((*core.core_services(fleet.config), *followers))
    containers = engine.containers()
    states = compose.states_of(containers)
    plans = plan_switches(fleet.units, fleet.profile_services, fleet.config, states, switched_off)
    core_plans = core.plan_core(fleet.config, containers, hashes, repository, record)
    follower_plans = core.plan_followers(
        fleet.config, containers, hashes, repository, record, gates
    )
    images = plan_kept_images(
        fleet.units, fleet.profile_services, fleet.config, containers, plans, followers
    )
    starts_keycloak = any(
        plan.unit.key == reconcile.REALM_UNIT and plan.action is Action.ON for plan in plans
    )
    realm = reconcile.plan_realm(
        engine,
        reconcile.Observer(engine),
        record,
        fleet.config,
        repository,
        gates.get(reconcile.REALM_UNIT),
        starts_keycloak,
    )
    app_plans = plan_apps(repository, fleet, app_runtime, marks)
    return Plan(fleet, read, core_plans, follower_plans, plans, images, realm, app_plans)


def _gates(
    units: Sequence[catalog.Unit], switched_off: Mapping[str, str | None]
) -> dict[str, core.Gate]:
    """Each catalogued unit's gate for its followers: on while enabled and not switched off,
    whether or not it is up (#89)."""
    return {unit.key: _gate(unit, switched_off) for unit in units}


def _gate(unit: catalog.Unit, switched_off: Mapping[str, str | None]) -> core.Gate:
    if unit.key in switched_off:
        return core.Gate.SWITCHED_OFF
    if not unit.enabled:
        return core.Gate.OFF
    return core.Gate.ON


def _switched_off(
    fleet: Fleet, read: Mapping[str, switches.Mark | None], repository: Path
) -> dict[str, str | None]:
    """Each unit switched off by its marker (its time) or by a barrier a stop left (None)."""
    switched_off = {}
    for unit in fleet.units:
        mark = read[unit.key]
        services = fleet.profile_services[unit.key]
        ingress = barrier.Barrier.of_unit(fleet.config, services, repository, unit)
        if barrier.switched_off(mark, ingress):
            switched_off[unit.key] = mark.since if mark else None
    return switched_off


def apply_closed(
    state_directory: Path,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    check: setup.SetupCheck = setup.run_setup_check,
    run_setup: setup.SetupRunner = setup.run_setup,
    home: Path | None = None,
    verifier: barrier.Verifier = barrier.PROVEN_OVER_HTTP,
    watch: compose.Watch = compose.SETTLED,
    git: checkout.Runner = checkout.run_git,
    ensure_environment: EnsureEnvironment = environment.ensure,
    realm_backup: reconcile.Backup | None = None,
    app_runtime: AppRuntime | None = None,
) -> int:
    if terminal.refused_without_terminal(COMMAND):
        return ApplyExit.REFUSED
    if refusal := checkout.not_live_checkout_line(repository, home or Path.home()):
        _say(refusal)
        return ApplyExit.CANNOT_PLAN
    try:
        lock = _take_apply_lock(state_directory)
    except OSError as error:
        _say(f"cannot take {state_directory / APPLY_LOCK} ({error.strerror})")
        return ApplyExit.CANNOT_PLAN
    if lock is None:
        _say(f"another `{COMMAND}` is running; nothing changed")
        return ApplyExit.REFUSED
    try:
        live = checkout.Git(repository, git)
        if not (_on_clean_main(live) and _environment_ready(repository, ensure_environment)):
            return ApplyExit.CANNOT_PLAN
        if (waiting := fetched(live, _show, _say)) is None:
            return ApplyExit.CANNOT_PLAN
        return _plan_and_apply(
            state_directory,
            engine,
            repository,
            check,
            run_setup,
            verifier,
            watch,
            waiting,
            realm_backup or reconcile.Backup.installed(repository, _tell),
            app_runtime or AppRuntime.of(home or Path.home(), state_directory),
        )
    finally:
        os.close(lock)


def _say(line: str) -> None:
    print(f"{COMMAND}: {line}", file=sys.stderr)


def _show(line: str) -> None:
    print(f"{COMMAND}: {line}")


def _tell(line: str) -> None:
    print(f"{COMMAND}: {line}", flush=True)


def _take_apply_lock(state_directory: Path) -> int | None:
    """The descriptor holding `apply.lock`, or None while another apply holds it; raises OSError.

    The file is private and never unlinked, so every apply locks the same inode; the descriptor
    is not inherited, so no child outlives the lock with it (#95 rule 4).
    """
    state_directory.mkdir(mode=_PRIVATE_DIRECTORY, parents=True, exist_ok=True)
    descriptor = os.open(
        state_directory / APPLY_LOCK, os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, _PRIVATE_FILE
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _on_clean_main(git: checkout.Git) -> bool:
    """True on a born `main` with nothing changed; else False, after saying why (#95 rules 5, 6)."""
    refusals = checkout.not_clean_main(git)
    for line in refusals:
        _say(line)
    return not refusals


def _environment_ready(repository: Path, ensure_environment: EnsureEnvironment) -> bool:
    """True when `.env` holds the derived settings, written now or before; else False, after
    saying why (#136)."""
    try:
        written = ensure_environment(repository)
    except environment.EnvironmentRefused as refusal:
        _say(str(refusal))
        return False
    if written:
        _say(written)
    return True


def _plan_and_apply(
    state_directory: Path,
    engine: compose.Compose | None,
    repository: Path,
    check: setup.SetupCheck,
    run_setup: setup.SetupRunner,
    verifier: barrier.Verifier,
    watch: compose.Watch,
    waiting: Waiting,
    realm_backup: reconcile.Backup,
    app_runtime: AppRuntime,
) -> int:
    engine = engine or compose.Compose(repository=repository)
    marks = switches.Switches(state_directory)
    record = core.Record(state_directory)
    try:
        if waiting.commits:
            fleet = read_fleet(engine, repository)
            binds = core.checkout_binds(
                fleet.config, repository, {unit.key for unit in fleet.units}
            )
            for line in waiting.describe(binds, reconcile.REALM_FILES):
                print(f"{COMMAND}: {line}")
        planned = _plan(engine, marks, record, repository, app_runtime)
        missing = _missing_setups(planned, repository, check)
        completed: list[str] = []
        if kept := [m for m in missing if m.kept_stopped]:
            _refuse_switched_off_units_still_running(kept)
            return ApplyExit.REFUSED
        if missing:
            exit_code = _ask_and_run_setups(missing, repository, check, run_setup)
            if exit_code is not None:
                return exit_code
            completed = [m.unit.key for m in missing]
            planned = _plan(engine, marks, record, repository, app_runtime)
            if unready := _missing_setups(planned, repository, check):
                _refuse_units_still_not_set_up(unready)
                return ApplyExit.REFUSED
    except CANNOT_PLAN_ERRORS as error:
        print(f"{COMMAND}: cannot plan, {error}", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    if planned.changes_nothing:
        for line in planned.describe():
            print(f"{COMMAND}: {line}")
        print(f"{COMMAND}: nothing to change")
        return ApplyExit.APPLIED_OR_NOTHING
    return _confirm_and_switch(
        engine,
        planned,
        marks,
        CorePhase(record, repository, verifier, watch),
        completed,
        realm_backup,
    )


def _missing_setups(planned: Plan, repository: Path, check: setup.SetupCheck) -> list[MissingSetup]:
    """The enabled units whose `--check` says not set up, in catalog order.

    A unit whose containers already run is checked too: an interrupted setup leaves them up, and
    the publish must not follow before the setup is done. A unit kept stopped is checked only
    while its containers still run."""
    stopped = {p.unit.key: p.running for p in planned.plans if p.action is Action.STAYS_STOPPED}
    missing = []
    for unit in planned.fleet.units:
        if not unit.enabled or not stopped.get(unit.key, True):
            continue
        if line := setup.not_set_up_line(unit, repository, check):
            missing.append(MissingSetup(unit, line, kept_stopped=unit.key in stopped))
    return missing


def _refuse_units_still_not_set_up(missing: Sequence[MissingSetup]) -> None:
    """A unit that is switched on or already runs is still not set up: no question, no switch."""
    for item in missing:
        print(
            f"{COMMAND}: {item.unit.key} is not set up after the setups ran; "
            f"nothing switched; run `{item.unit.setup}`, then `{COMMAND}` again; "
            f"{item.check_line}",
            file=sys.stderr,
        )


def _refuse_switched_off_units_still_running(missing: Sequence[MissingSetup]) -> None:
    """A unit stopped by `edge stop` never gets a setup or a start,
    but its containers still run."""
    for item in missing:
        print(
            f"{COMMAND}: {item.unit.key} is switched off but still runs and is not set up; "
            f"nothing switched; run `edge stop {item.unit.key}` again; {item.check_line}",
            file=sys.stderr,
        )


def _ask_and_run_setups(
    missing: Sequence[MissingSetup],
    repository: Path,
    check: setup.SetupCheck,
    run_setup: setup.SetupRunner,
) -> int | None:
    """Ask for every setup first, then run them in turn; an exit code when the run ends here."""
    for item in missing:
        print(f"{item.unit.key} is not set up: {item.check_line}")
        if not terminal.confirmed(f"run `{item.unit.setup}` now"):
            print(
                f"{COMMAND}: aborted, nothing changed; run `{item.unit.setup}` yourself "
                "or answer yes",
                file=sys.stderr,
            )
            return ApplyExit.REFUSED
    for item in missing:
        if (exit_code := _run_one_setup(item, repository, check, run_setup)) is not None:
            return exit_code
    return None


def _run_one_setup(
    item: MissingSetup,
    repository: Path,
    check: setup.SetupCheck,
    run_setup: setup.SetupRunner,
) -> int | None:
    command = item.unit.setup
    try:
        ran = run_setup(repository / str(command), repository)
    except OSError as error:
        print(f"{COMMAND}: cannot start `{command}`, {error}", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    if ran.interrupted:
        print(
            f"\n{COMMAND}: interrupted during `{command}`; nothing switched; keys may already "
            f"exist; run `{COMMAND}` again",
            file=sys.stderr,
        )
        return ApplyExit.REFUSED
    if ran.exit_code:
        print(
            f"{COMMAND}: `{command}` failed (exit {ran.exit_code}); nothing switched; keys may "
            f"already exist; run `{COMMAND}` again",
            file=sys.stderr,
        )
        return ApplyExit.REFUSED
    if line := setup.not_set_up_line(item.unit, repository, check):
        print(f"{COMMAND}: `{command}` ran, still not set up: {line}", file=sys.stderr)
        return ApplyExit.REFUSED
    return None


@dataclass(frozen=True)
class CorePhase:
    """Recreate each changed core service with a live container, then each changed follower whose
    unit is on, before any unit is touched; record the followers a switch-on started."""

    record: core.Record
    repository: Path
    verifier: barrier.Verifier
    watch: compose.Watch

    def run(
        self,
        engine: compose.Compose,
        planned: Plan,
        marks: switches.Switches,
        progress: Progress,
    ) -> str | None:
        """The core, then the followers; the failure line of the first step that fails."""
        if failure := self._recreate_core(engine, planned, marks, progress):
            return failure
        return self._recreate_followers(engine, planned, marks, progress)

    def _recreate_core(
        self,
        engine: compose.Compose,
        planned: Plan,
        marks: switches.Switches,
        progress: Progress,
    ) -> str | None:
        """In start order, look again whether each still has a live container; before the
        set's first recreate mark every recorded service of the set unfinished, whatever its
        liveness then; recreate each, observe it running with the core services it depends on,
        prove every active barrier, and only then record it with its plan-time digests (#85 rule
        3, #87 rule 4). The failure line of the first step that fails, whose set is left marked
        unfinished once its first recreate began, so the next run recreates it (#85 rule 5). One
        stopped since the plan is not started; where it is the set's first, no recreate began
        and no entry changes (C3, #93 rule 2)."""
        config = planned.fleet.config
        recreates = [plan for plan in planned.core if plan.action is core.CoreAction.RECREATE]
        try:
            followed = self._followed(config, progress) if recreates else frozenset()
            for index, plan in enumerate(recreates):
                progress.unit = plan.service
                if stopped := self._stopped_meanwhile(engine, plan, progress):
                    return stopped
                if index == 0:
                    self._mark_unfinished(recreates, followed, progress)
                if failure := self._recreate_one(engine, planned, marks, plan, followed, progress):
                    return failure
        except (compose.ComposeError, core.CoreError) as error:
            return f"could not {progress.step}, {error}"
        if recreates and len(recreates) == len(planned.core):
            print(f"{COMMAND}: the core is current")
        return None

    def _stopped_meanwhile(
        self, engine: compose.Compose, plan: core.CorePlan, progress: Progress
    ) -> str | None:
        """The failure line when the service has no live container now (#93 rules 1, 2)."""
        progress.step = f"look whether {plan.service} still runs"
        if _stopped_since_the_plan(engine.states(), plan):
            return f"{plan.service} was stopped meanwhile; it is not started"
        return None

    def _mark_unfinished(
        self, recreates: Sequence[core.CorePlan], followed: frozenset[str], progress: Progress
    ) -> None:
        """Empty the entry of every recorded service of the set, so a recreate that does not
        finish, or a dependent stopped before its own, is recreated by the next run (#85 rule 5,
        #93 rule 2)."""
        if recorded := [plan.service for plan in recreates if plan.service in followed]:
            progress.step = f"mark {', '.join(recorded)} unfinished in {core.APPLIED_FILE}"
            self.record.mark_unfinished(recorded, followed)

    def _recreate_one(
        self,
        engine: compose.Compose,
        planned: Plan,
        marks: switches.Switches,
        plan: core.CorePlan,
        followed: frozenset[str],
        progress: Progress,
    ) -> str | None:
        """Recreate the service, observe it running with the core services it depends on, prove
        every active barrier, and only then record it; the failure line of the first step that
        fails."""
        config = planned.fleet.config
        progress.step = f"recreate {plan.service}"
        engine.recreate(plan.service)
        progress.step = f"observe {plan.service} running"
        observed = _in_start_order(config, {plan.service, *core.dependencies(config, plan.service)})
        if down := engine.down_while_watched(config, observed, self.watch):
            verb = "is" if len(down) == 1 else "are"
            return f"{' and '.join(down)} {verb} not running after the {plan.service} recreate"
        progress.step = "prove the barriers"
        if unproven := self._unproven_barrier(planned, marks, plan.service):
            return unproven
        if plan.service in followed:
            progress.step = f"record {plan.service} in {core.APPLIED_FILE}"
            self.record.write(plan.service, plan.digests, followed)
        print(f"{COMMAND}: {plan.recreated(followed)}")
        return None

    def _recreate_followers(
        self,
        engine: compose.Compose,
        planned: Plan,
        marks: switches.Switches,
        progress: Progress,
    ) -> str | None:
        """Recreate each changed follower whose unit is on, alone (`--no-deps`: its unit's other
        services are never touched, C6), under the switch lock, so a marker that appeared since
        the plan holds it (P7). Its entry is emptied first and its plan-time digests written once
        it is observed running (#89 rule 3). No barrier is proven again: a follower serves no
        barrier."""
        recreates = [
            plan for plan in planned.followers if plan.action is core.FollowerAction.RECREATE
        ]
        if not recreates:
            return None
        config = planned.fleet.config
        try:
            followed = self._followed(config, progress)
            for plan in recreates:
                progress.unit = plan.service
                with marks.lock():
                    if changed := marks.changed(plan.unit, planned.read[plan.unit]):
                        since = switches.describe_since(since_of(changed))
                        return (
                            f"{plan.unit} was switched off meanwhile (since {since}); "
                            f"{plan.service} is not recreated"
                        )
                    progress.step = f"mark {plan.service} unfinished in {core.APPLIED_FILE}"
                    self.record.mark_unfinished([plan.service], followed)
                    progress.step = f"recreate {plan.service}"
                    engine.recreate(plan.service)
                progress.step = f"observe {plan.service} running"
                if engine.down_while_watched(config, (plan.service,), self.watch):
                    return f"{plan.service} is not running after the {plan.service} recreate"
                progress.step = f"record {plan.service} in {core.APPLIED_FILE}"
                self.record.write(plan.service, plan.digests, followed)
                print(f"{COMMAND}: {plan.recreated()}")
        except (compose.ComposeError, core.CoreError, switches.SwitchError) as error:
            return f"could not {progress.step}, {error}"
        return None

    def record_switched_on(
        self, planned: Plan, switched_on: Sequence[UnitPlan], progress: Progress
    ) -> None:
        """Write the plan-time digests of each follower that started with a unit observed on, so
        the next run does not recreate it (#89, P15)."""
        units = {plan.unit.key for plan in switched_on}
        started = [
            plan
            for plan in planned.followers
            if plan.action is core.FollowerAction.STARTS_WITH_UNIT and plan.unit in units
        ]
        if not started:
            return
        followed = self._followed(planned.fleet.config, progress)
        for plan in started:
            progress.unit = plan.unit
            progress.step = f"record {plan.service} in {core.APPLIED_FILE}"
            self.record.write(plan.service, plan.digests, followed)

    def _followed(self, config: Mapping[str, Any], progress: Progress) -> frozenset[str]:
        progress.step = "find the services edge follows"
        return core.followed_services(config, self.repository)

    def _unproven_barrier(
        self, planned: Plan, marks: switches.Switches, service: str
    ) -> str | None:
        """Say each proven barrier; the failure line naming every one that is not, after all
        active barriers were tried (S9)."""
        unproven: list[barrier.Unproven] = []
        fleet = planned.fleet
        for proof in barrier.prove_active(
            fleet.units, fleet.profile_services, fleet.config, marks, self.repository,
            self.verifier, planned.app_plans.catalogued,
        ):  # fmt: skip
            match proof:
                case barrier.Proven(unit):
                    print(f"{COMMAND}: the barrier of {unit} is proven")
                case barrier.NoHost(unit):
                    print(f"{COMMAND}: {unit}: no host behind Traefik, so no barrier")
                case barrier.Unproven():
                    unproven.append(proof)
        if not unproven:
            return None
        units = ", ".join(proof.unit for proof in unproven)
        if len(unproven) == 1:
            barriers, failures = f"the barrier of {units} is", unproven[0].failure
        else:
            barriers = f"the barriers of {units} are"
            failures = "; ".join(f"{proof.unit}: {proof.failure}" for proof in unproven)
        return (
            f"{barriers} not proven after the {service} recreate ({failures}): {units} may be "
            f"reachable through Traefik; to close the tunnel now: {EMERGENCY_STOP}"
        )


def _stopped_since_the_plan(states: Mapping[str, set[str]], plan: core.CorePlan) -> bool:
    """The core service planned for a recreate has no live container now; one created and never
    started counts as live only where the plan found edge's unfinished recreate left it so (#87
    rule 4): any other was put there since the plan and is not started (#93 rule 1)."""
    recovering = plan.left_unstarted and compose.never_started(states, plan.service)
    return not (recovering or compose.has_running_container(states, (plan.service,)))


def _in_start_order(config: Mapping[str, Any], services: set[str]) -> tuple[str, ...]:
    return tuple(service for service in core.start_order(config) if service in services)


def _observe_and_record(
    engine: compose.Compose,
    planned: Plan,
    core_phase: CorePhase,
    started: Sequence[UnitPlan],
    progress: Progress,
) -> list[UnitPlan]:
    """The started plans not observed as planned; each unit observed on records the followers
    that started with it (#89, P15)."""
    progress.step = "observe the result"
    mismatched = unobserved(engine, started, planned.fleet.config, core_phase.watch)
    switched_on = [plan for plan in started if plan.action is Action.ON and plan not in mismatched]
    core_phase.record_switched_on(planned, switched_on, progress)
    return mismatched


def _apply_realm(
    engine: compose.Compose,
    planned: Plan,
    marks: switches.Switches,
    core_phase: CorePhase,
    backup: reconcile.Backup,
) -> bool:
    """The realm step after the unit switches; says its lines and whether it ended partial."""
    realm = reconcile.Realm(
        engine,
        reconcile.Observer(engine),
        backup,
        core_phase.record,
        planned.fleet.config,
        core_phase.repository,
        marks,
        planned.read.get(reconcile.REALM_UNIT),
    )
    result = realm.run(planned.realm)
    for line in result.lines:
        print(f"{COMMAND}: {line}")
    if result.failure:
        print(f"{COMMAND}: partial, {result.failure}; run `{COMMAND}` again", file=sys.stderr)
    return bool(result.failure)


def _apply_apps(planned: Plan, marks: switches.Switches, progress: Progress) -> bool:
    """The app step after the realm step; says its lines and whether it ended partial."""
    failed = False
    for plan, failure in planned.app_plans.run(marks, progress):
        if failure:
            print(f"{COMMAND}: partial, {failure}; run `{COMMAND}` again", file=sys.stderr)
            failed = True
        else:
            print(f"{COMMAND}: {plan.app.key} is on at {plan.release.tag}")
    return failed


def _confirm_and_switch(
    engine: compose.Compose,
    planned: Plan,
    marks: switches.Switches,
    core_phase: CorePhase,
    completed_setups: Sequence[str],
    realm_backup: reconcile.Backup,
) -> int:
    print(f"{COMMAND}: the plan")
    for line in planned.describe():
        print(f"  {line}")
    if not terminal.confirmed("apply exactly this plan"):
        outcome = (
            f"setup of {', '.join(completed_setups)} completed; nothing switched"
            if completed_setups
            else "nothing changed"
        )
        print(f"{COMMAND}: aborted, {outcome}", file=sys.stderr)
        return ApplyExit.REFUSED
    plans, read = planned.plans, planned.read
    changes = [plan for plan in plans if plan.action is not Action.STAYS_STOPPED]
    progress = Progress()
    withheld: dict[str, str] = {}
    try:
        if failure := core_phase.run(engine, planned, marks, progress):
            print(
                f"{COMMAND}: partial, {failure}; no unit switched; run `{COMMAND}` again",
                file=sys.stderr,
            )
            return ApplyExit.REFUSED
        for plan in changes:
            if since := switch(engine, plan, progress, marks, read):
                withheld[plan.unit.key] = since
        started = [plan for plan in changes if plan.unit.key not in withheld]
        mismatched = _observe_and_record(engine, planned, core_phase, started, progress)
        progress.unit, progress.step = reconcile.REALM_UNIT, "apply the realm step"
        realm_failed = _apply_realm(engine, planned, marks, core_phase, realm_backup)
        apps_failed = _apply_apps(planned, marks, progress)
    except (compose.ComposeError, switches.SwitchError, core.CoreError) as error:
        print(
            f"{COMMAND}: partial, {progress.unit}: could not {progress.step}, {error}",
            file=sys.stderr,
        )
        return ApplyExit.REFUSED
    except KeyboardInterrupt:
        print(
            f"\n{COMMAND}: interrupted while changing {progress.unit} ({progress.step}); "
            f"its state may be partial, run `{COMMAND}` to see",
            file=sys.stderr,
        )
        return ApplyExit.REFUSED
    return _report_switched(changes, withheld, mismatched, realm_failed or apps_failed)


def _report_switched(
    changes: Sequence[UnitPlan],
    withheld: Mapping[str, str],
    mismatched: Sequence[UnitPlan],
    later_steps_failed: bool,
) -> int:
    for unit, since in withheld.items():
        print(
            f"{COMMAND}: partial, {unit} was switched off meanwhile "
            f"(since {switches.describe_since(since)}); not started",
            file=sys.stderr,
        )
    for plan in mismatched:
        print(
            f"{COMMAND}: partial, {plan.unit.key} is not {plan.action} after the change",
            file=sys.stderr,
        )
    if mismatched or withheld or later_steps_failed:
        return ApplyExit.REFUSED
    for plan in changes:
        print(f"{COMMAND}: {plan.unit.key} is {plan.action}")
    return ApplyExit.APPLIED_OR_NOTHING
