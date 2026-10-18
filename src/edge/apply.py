"""`edge apply --closed`: switches the units of this repository on or off from `fleet.yaml`.

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
A unit is a compose profile: on
starts its services, off removes their containers. Volumes are never touched. A unit that would be
switched on and is not set up is asked about separately; on `yes` its own setup command runs in
the operator's terminal (key material is made there, never here) and `--check` must then pass
before a fresh plan is shown. A unit with an emergency switch (`edge stop`) or a barrier a stop
left is never started and its switch is never cleared here: only `edge start` ends it. It runs
only from the live checkout `~/git/edge`: from any other tree it would bind the live compose
project to that tree, so it refuses there with exit 2 (#79).
"""

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from edge import barrier, catalog, checkout, compose, core, setup, switches, terminal

COMMAND = "edge apply --closed"
# The emergency command's name; `emergency` owns the verb but imports this module, so it
# cannot be imported here.
EMERGENCY_STOP = "edge stop --all"


class ApplyExit(IntEnum):
    APPLIED_OR_NOTHING = 0
    REFUSED = 1
    CANNOT_PLAN = 2


class Action(StrEnum):
    ON = "on"
    OFF = "off"
    STAYS_STOPPED = "stays stopped"


@dataclass(frozen=True)
class UnitPlan:
    unit: catalog.Unit
    action: Action
    services: tuple[str, ...]
    volumes: tuple[str, ...]
    switched_off_since: str | None = None

    def describe(self) -> str:
        touched = f"services {', '.join(self.services)}"
        if self.action is Action.STAYS_STOPPED and self.switched_off_since is None:
            return f"{self.unit.key} stays stopped (its barrier stands; no switch recorded)"
        if self.action is Action.STAYS_STOPPED:
            return f"{self.unit.key} stays stopped (switch since {self._since()})"
        if self.action is Action.OFF:
            line = f"{self.unit.key}: off ({touched}; volumes {', '.join(self.volumes)} stay)"
            if self.switched_off_since:
                line += f"; its switch since {self._since()} stays"
            return line
        return f"{self.unit.key}: on ({touched})"

    def _since(self) -> str:
        return switches.describe_since(self.switched_off_since or "")


@dataclass(frozen=True)
class Fleet:
    """What `fleet.yaml` and Compose say about this repository's units."""

    config: Mapping[str, Any]
    profile_services: Mapping[str, tuple[str, ...]]
    units: tuple[catalog.Unit, ...]


def read_fleet(engine: compose.Compose, repository: Path) -> Fleet:
    config = engine.resolved_configuration()
    profile_services = compose.profile_services(config)
    return Fleet(config, profile_services, catalog.load(repository, profile_services))


def plan_switches(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    states: Mapping[str, set[str]],
    switched_off: Mapping[str, str | None],
) -> list[UnitPlan]:
    """What each unit needs to become what the catalog says; a unit already so is left out.

    `switched_off` maps a switched-off unit to the time of its switch, None when only its barrier
    stands: that unit is never started, and an enabled one is listed as staying stopped.
    """
    plans = []
    for unit in units:
        services = profile_services[unit.key]
        since = switched_off.get(unit.key)
        if unit.enabled and unit.key in switched_off:
            action = Action.STAYS_STOPPED
        elif unit.enabled and not compose.is_up(states, config, services):
            action = Action.ON
        elif not unit.enabled and not compose.is_gone(states, services):
            action = Action.OFF
        else:
            continue
        volumes = sorted(
            {v for name in services for v in compose.named_volumes(config["services"][name])}
        )
        plans.append(UnitPlan(unit, action, services, tuple(volumes), since))
    return plans


@dataclass
class Progress:
    unit: str = ""
    step: str = ""


def _switch(
    engine: compose.Compose,
    plan: UnitPlan,
    progress: Progress,
    marks: switches.Switches,
    read: Mapping[str, switches.Mark | None],
) -> str | None:
    """Switch one unit; the time of a switch that appeared meanwhile when the unit stays down."""
    progress.unit = plan.unit.key
    if plan.action is Action.ON:
        progress.step = "start"
        with marks.lock():
            if changed := marks.changed(plan.unit.key, read[plan.unit.key]):
                return _since_of(changed)
            engine.start(plan.unit.key, plan.services)
        return None
    progress.step = "stop"
    engine.stop(plan.unit.key, plan.services)
    progress.step = "remove the containers"
    engine.remove_containers(plan.unit.key, plan.services)
    return None


def _since_of(changed: switches.Changed) -> str:
    """The time of a switch that appeared after the plan was read."""
    return changed.current.since if changed.current else switches.UNKNOWN_SWITCH_TIME


def _unobserved(
    engine: compose.Compose, plans: Sequence[UnitPlan], config: Mapping[str, Any]
) -> list[UnitPlan]:
    states = engine.states()
    return [
        plan
        for plan in plans
        if not (
            compose.is_up(states, config, plan.services)
            if plan.action is Action.ON
            else compose.is_gone(states, plan.services)
        )
    ]


CANNOT_PLAN_ERRORS = (
    compose.ComposeError,
    catalog.CatalogError,
    barrier.BarrierError,
    core.CoreError,
    setup.SetupUnclear,
    switches.SwitchError,
)


@dataclass(frozen=True)
class Plan:
    fleet: Fleet
    read: Mapping[str, switches.Mark | None]
    core: list[core.CorePlan]
    followers: list[core.FollowerPlan]
    plans: list[UnitPlan]

    @property
    def changes_nothing(self) -> bool:
        return (
            all(plan.action is Action.STAYS_STOPPED for plan in self.plans)
            and all(plan.action is core.CoreAction.NOT_STARTED for plan in self.core)
            and all(plan.action is core.FollowerAction.HELD for plan in self.followers)
        )

    def describe(self) -> list[str]:
        return [plan.describe() for plan in (*self.core, *self.followers, *self.plans)]


@dataclass(frozen=True)
class MissingSetup:
    unit: catalog.Unit
    check_line: str


def _plan(
    engine: compose.Compose, marks: switches.Switches, record: core.Record, repository: Path
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
    return Plan(fleet, read, core_plans, follower_plans, plans)


def _gates(
    units: Sequence[catalog.Unit], switched_off: Mapping[str, str | None]
) -> dict[str, core.Gate]:
    """Each catalogued unit's gate for its followers: on while enabled and not switched off,
    whether or not it is up (#89)."""
    return {unit.key: _gate(unit, switched_off) for unit in units}


def _gate(unit: catalog.Unit, switched_off: Mapping[str, str | None]) -> core.Gate:
    if not unit.enabled:
        return core.Gate.OFF
    if unit.key in switched_off:
        return core.Gate.SWITCHED_OFF
    return core.Gate.ON


def _switched_off(
    fleet: Fleet, read: Mapping[str, switches.Mark | None], repository: Path
) -> dict[str, str | None]:
    """Each unit switched off by its marker (its time) or by a barrier a stop left (None)."""
    switched_off = {}
    for unit in fleet.units:
        mark = read[unit.key]
        services = fleet.profile_services[unit.key]
        ingress = barrier.Barrier.of_unit(fleet.config, services, repository, unit.key)
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
) -> int:
    if terminal.refused_without_terminal(COMMAND):
        return ApplyExit.REFUSED
    if not checkout.is_live_checkout(repository, home or Path.home()):
        print(
            f"{COMMAND}: runs only from the live checkout {checkout.LIVE_CHECKOUT} "
            f"(this is {repository})",
            file=sys.stderr,
        )
        return ApplyExit.CANNOT_PLAN
    engine = engine or compose.Compose(repository=repository)
    marks = switches.Switches(state_directory)
    record = core.Record(state_directory)
    try:
        planned = _plan(engine, marks, record, repository)
        missing = _missing_setups(planned.plans, repository, check)
        completed: list[str] = []
        if missing:
            exit_code = _ask_and_run_setups(missing, repository, check, run_setup)
            if exit_code is not None:
                return exit_code
            completed = [m.unit.key for m in missing]
            planned = _plan(engine, marks, record, repository)
            if unready := _missing_setups(planned.plans, repository, check):
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
        engine, planned, marks, CorePhase(record, repository, verifier, watch), completed
    )


def _missing_setups(
    plans: Sequence[UnitPlan], repository: Path, check: setup.SetupCheck
) -> list[MissingSetup]:
    """The units that would be switched on and whose `--check` says not set up, in catalog order."""
    missing = []
    for plan in plans:
        if plan.action is Action.ON and (
            line := setup.not_set_up_line(plan.unit, repository, check)
        ):
            missing.append(MissingSetup(plan.unit, line))
    return missing


def _refuse_units_still_not_set_up(missing: Sequence[MissingSetup]) -> None:
    """A unit the fresh plan switches on is still not set up: no question, no switch."""
    for item in missing:
        print(
            f"{COMMAND}: {item.unit.key} is not set up after the setups ran; "
            f"nothing switched; run `{item.unit.setup}`, then `{COMMAND}` again; "
            f"{item.check_line}",
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
        """Mark every recorded service of the set unfinished, then in start order recreate each,
        observe it running with the core services it depends on, prove every active barrier, and
        only then record it with its plan-time digests (#85 rule 3, #87 rule 4); the failure line
        of the first step that fails, whose set is left marked unfinished, so the next run
        recreates it (#85 rule 5)."""
        config = planned.fleet.config
        recreates = [plan for plan in planned.core if plan.action is core.CoreAction.RECREATE]
        try:
            followed: frozenset[str] = frozenset()
            if recreates:
                progress.step = "find the services edge follows"
                followed = core.followed_services(config, self.repository)
            if recorded := [plan.service for plan in recreates if plan.service in followed]:
                progress.unit = recorded[0]
                progress.step = f"mark {', '.join(recorded)} unfinished in {core.APPLIED_FILE}"
                self.record.mark_unfinished(recorded, followed)
            for plan in recreates:
                progress.unit = plan.service
                progress.step = f"recreate {plan.service}"
                engine.recreate(plan.service)
                progress.step = f"observe {plan.service} running"
                observed = _in_start_order(
                    config, {plan.service, *core.dependencies(config, plan.service)}
                )
                if down := engine.down_while_watched(config, observed, self.watch):
                    verb = "is" if len(down) == 1 else "are"
                    return (
                        f"{' and '.join(down)} {verb} not running after the {plan.service} recreate"
                    )
                progress.step = "prove the barriers"
                if unproven := self._unproven_barrier(planned.fleet, marks, plan.service):
                    return unproven
                if plan.service in followed:
                    progress.step = f"record {plan.service} in {core.APPLIED_FILE}"
                    self.record.write(plan.service, plan.digests, followed)
                print(f"{COMMAND}: {plan.recreated(followed)}")
        except (compose.ComposeError, core.CoreError) as error:
            return f"could not {progress.step}, {error}"
        if recreates and len(recreates) == len(planned.core):
            print(f"{COMMAND}: the core is current")
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
            progress.step = "find the services edge follows"
            followed = core.followed_services(config, self.repository)
            for plan in recreates:
                progress.unit = plan.service
                with marks.lock():
                    if changed := marks.changed(plan.unit, planned.read[plan.unit]):
                        since = switches.describe_since(_since_of(changed))
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
        progress.step = "find the services edge follows"
        followed = core.followed_services(planned.fleet.config, self.repository)
        for plan in started:
            progress.unit = plan.unit
            progress.step = f"record {plan.service} in {core.APPLIED_FILE}"
            self.record.write(plan.service, plan.digests, followed)

    def _unproven_barrier(self, fleet: Fleet, marks: switches.Switches, service: str) -> str | None:
        """Say each proven barrier; the failure line naming every one that is not, after all
        active barriers were tried (S9)."""
        unproven: list[barrier.Unproven] = []
        for proof in barrier.prove_active(
            fleet.units, fleet.profile_services, fleet.config, marks, self.repository, self.verifier
        ):
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


def _in_start_order(config: Mapping[str, Any], services: set[str]) -> tuple[str, ...]:
    return tuple(service for service in core.start_order(config) if service in services)


def _confirm_and_switch(
    engine: compose.Compose,
    planned: Plan,
    marks: switches.Switches,
    core_phase: CorePhase,
    completed_setups: Sequence[str],
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
    plans, config, read = planned.plans, planned.fleet.config, planned.read
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
            if since := _switch(engine, plan, progress, marks, read):
                withheld[plan.unit.key] = since
        progress.step = "observe the result"
        started = [plan for plan in changes if plan.unit.key not in withheld]
        mismatched = _unobserved(engine, started, config)
        switched_on = [p for p in started if p.action is Action.ON and p not in mismatched]
        core_phase.record_switched_on(planned, switched_on, progress)
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
    if mismatched or withheld:
        return ApplyExit.REFUSED
    for plan in changes:
        print(f"{COMMAND}: {plan.unit.key} is {plan.action}")
    return ApplyExit.APPLIED_OR_NOTHING
