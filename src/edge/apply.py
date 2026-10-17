"""`edge apply --closed`: switches the units of this repository on or off from `fleet.yaml`.

Exit 0 applied and observed, or nothing to change; 1 refused, aborted, failed or partial;
2 cannot plan. A unit is a compose profile: on starts its services, off removes their containers.
Volumes are never touched. A unit that would be switched on and is not set up is asked about
separately; on `yes` its own setup command runs in the operator's terminal (key material is made
there, never here) and `--check` must then pass before a fresh plan is shown. A unit with an
emergency switch (`edge stop`) is never started and its switch is never cleared here: only
`edge start` ends it.
"""

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from edge import catalog, compose, setup, switches, terminal

COMMAND = "edge apply --closed"


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
    switched_off: Mapping[str, str],
) -> list[UnitPlan]:
    """What each unit needs to become what the catalog says; a unit already so is left out.

    `switched_off` maps a unit to the time of its switch: that unit is never started, and an
    enabled one is listed as staying stopped.
    """
    plans = []
    for unit in units:
        services = profile_services[unit.key]
        since = switched_off.get(unit.key)
        if unit.enabled and since:
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
                current = changed.current
                return current.since if current else switches.UNKNOWN_SWITCH_TIME
            engine.start(plan.unit.key, plan.services)
        return None
    progress.step = "stop"
    engine.stop(plan.unit.key, plan.services)
    progress.step = "remove the containers"
    engine.remove_containers(plan.unit.key, plan.services)
    return None


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
    setup.SetupUnclear,
    switches.SwitchError,
)


@dataclass(frozen=True)
class Plan:
    fleet: Fleet
    read: Mapping[str, switches.Mark | None]
    plans: list[UnitPlan]


@dataclass(frozen=True)
class MissingSetup:
    unit: catalog.Unit
    check_line: str


def _plan(engine: compose.Compose, marks: switches.Switches, repository: Path) -> Plan:
    fleet = read_fleet(engine, repository)
    read = {unit.key: marks.mark(unit.key) for unit in fleet.units}
    switched_off = {key: mark.since for key, mark in read.items() if mark}
    plans = plan_switches(
        fleet.units, fleet.profile_services, fleet.config, engine.states(), switched_off
    )
    return Plan(fleet, read, plans)


def apply_closed(
    state_directory: Path,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    check: setup.SetupCheck = setup.run_setup_check,
    run_setup: setup.SetupRunner = setup.run_setup,
) -> int:
    if terminal.refused_without_terminal(COMMAND):
        return ApplyExit.REFUSED
    engine = engine or compose.Compose(repository=repository)
    marks = switches.Switches(state_directory)
    try:
        planned = _plan(engine, marks, repository)
        missing = _missing_setups(planned.plans, repository, check)
        completed: list[str] = []
        if missing:
            exit_code = _ask_and_run_setups(missing, repository, check, run_setup)
            if exit_code is not None:
                return exit_code
            completed = [m.unit.key for m in missing]
            planned = _plan(engine, marks, repository)
    except CANNOT_PLAN_ERRORS as error:
        print(f"{COMMAND}: cannot plan, {error}", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    plans = planned.plans
    if all(plan.action is Action.STAYS_STOPPED for plan in plans):
        for plan in plans:
            print(f"{COMMAND}: {plan.describe()}")
        print(f"{COMMAND}: nothing to change")
        return ApplyExit.APPLIED_OR_NOTHING
    return _confirm_and_switch(engine, plans, planned.fleet.config, marks, planned.read, completed)


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


def _confirm_and_switch(
    engine: compose.Compose,
    plans: Sequence[UnitPlan],
    config: Mapping[str, Any],
    marks: switches.Switches,
    read: Mapping[str, switches.Mark | None],
    completed_setups: Sequence[str],
) -> int:
    print(f"{COMMAND}: the plan")
    for plan in plans:
        print(f"  {plan.describe()}")
    if not terminal.confirmed("apply exactly this plan"):
        outcome = (
            f"setup of {', '.join(completed_setups)} completed; nothing switched"
            if completed_setups
            else "nothing changed"
        )
        print(f"{COMMAND}: aborted, {outcome}", file=sys.stderr)
        return ApplyExit.REFUSED
    changes = [plan for plan in plans if plan.action is not Action.STAYS_STOPPED]
    progress = Progress()
    withheld: dict[str, str] = {}
    try:
        for plan in changes:
            if since := _switch(engine, plan, progress, marks, read):
                withheld[plan.unit.key] = since
        progress.step = "observe the result"
        started = [plan for plan in changes if plan.unit.key not in withheld]
        mismatched = _unobserved(engine, started, config)
    except (compose.ComposeError, switches.SwitchError) as error:
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
