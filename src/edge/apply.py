"""`edge apply --closed`: switches the units of this repository on or off from `fleet.yaml`.

Exit 0 applied and observed, or nothing to change; 1 refused, aborted, failed or partial;
2 cannot plan. A unit is a compose profile: on starts its services, off removes their containers.
Volumes are never touched, and no key material is made here: a unit that needs a setup is
refused until its setup command says `--check` passes.
"""

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from edge import catalog, compose, terminal

COMMAND = "edge apply --closed"
SETUP_CHECK_SECONDS = 300


class ApplyExit(IntEnum):
    APPLIED_OR_NOTHING = 0
    REFUSED = 1
    CANNOT_PLAN = 2


class Action(StrEnum):
    ON = "on"
    OFF = "off"


class SetupCheckExit(IntEnum):
    SET_UP = 0
    NOT_SET_UP = 1


@dataclass(frozen=True)
class UnitPlan:
    unit: catalog.Unit
    action: Action
    services: tuple[str, ...]
    volumes: tuple[str, ...]

    def describe(self) -> str:
        touched = f"services {', '.join(self.services)}"
        if self.action is Action.OFF:
            return f"{self.unit.key}: off ({touched}; volumes {', '.join(self.volumes)} stay)"
        return f"{self.unit.key}: on ({touched})"


class SetupUnclear(Exception):
    """A setup check could not tell; the message is its own line."""


SetupCheck = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_setup_check(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=SETUP_CHECK_SECONDS
    )


def plan_switches(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    states: Mapping[str, set[str]],
) -> list[UnitPlan]:
    """What each unit needs to become what the catalog says; a unit already so is left out."""
    plans = []
    for unit in units:
        services = profile_services[unit.key]
        if unit.enabled and not compose.is_up(states, config, services):
            action = Action.ON
        elif not unit.enabled and not compose.is_gone(states, services):
            action = Action.OFF
        else:
            continue
        volumes = sorted(
            {v for name in services for v in compose.named_volumes(config["services"][name])}
        )
        plans.append(UnitPlan(unit, action, services, tuple(volumes)))
    return plans


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no output"


def _check_setup(plan: UnitPlan, repository: Path, check: SetupCheck) -> str | None:
    """None when the unit is set up, else the refusal; raises SetupUnclear when it cannot tell."""
    setup = plan.unit.setup
    if setup is None:
        return None
    try:
        result = check([str(repository / setup), "--check"])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupUnclear(f"`{setup} --check` did not run ({type(error).__name__})") from error
    if result.returncode == SetupCheckExit.SET_UP:
        return None
    if result.returncode == SetupCheckExit.NOT_SET_UP:
        return (
            f"{plan.unit.key} is not set up: run `{setup}`, then `{COMMAND}` again; "
            f"{_last_line(result)}"
        )
    raise SetupUnclear(f"`{setup} --check` cannot tell: {_last_line(result)}")


@dataclass
class Progress:
    unit: str = ""
    step: str = ""


def _switch(engine: compose.Compose, plan: UnitPlan, progress: Progress) -> None:
    progress.unit = plan.unit.key
    if plan.action is Action.ON:
        progress.step = "start"
        engine.start(plan.unit.key, plan.services)
        return
    progress.step = "stop"
    engine.stop(plan.unit.key, plan.services)
    progress.step = "remove the containers"
    engine.remove_containers(plan.unit.key, plan.services)


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


def apply_closed(
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    check: SetupCheck = run_setup_check,
) -> int:
    if terminal.refused_without_terminal(COMMAND):
        return ApplyExit.REFUSED
    engine = engine or compose.Compose(repository=repository)
    try:
        config = engine.resolved_configuration()
        profile_services = compose.profile_services(config)
        units = catalog.load(repository, profile_services)
        plans = plan_switches(units, profile_services, config, engine.states())
        refusal = _setup_refusal([p for p in plans if p.action is Action.ON], repository, check)
    except (compose.ComposeError, catalog.CatalogError, SetupUnclear) as error:
        print(f"{COMMAND}: cannot plan, {error}", file=sys.stderr)
        return ApplyExit.CANNOT_PLAN
    if refusal:
        print(f"{COMMAND}: refused, {refusal}", file=sys.stderr)
        return ApplyExit.REFUSED
    if not plans:
        print(f"{COMMAND}: nothing to change")
        return ApplyExit.APPLIED_OR_NOTHING
    return _confirm_and_switch(engine, plans, config)


def _setup_refusal(plans: Sequence[UnitPlan], repository: Path, check: SetupCheck) -> str | None:
    for plan in plans:
        refusal = _check_setup(plan, repository, check)
        if refusal:
            return refusal
    return None


def _confirm_and_switch(
    engine: compose.Compose, plans: Sequence[UnitPlan], config: Mapping[str, Any]
) -> int:
    print(f"{COMMAND}: the plan")
    for plan in plans:
        print(f"  {plan.describe()}")
    if not terminal.confirmed("apply exactly this plan"):
        print(f"{COMMAND}: aborted, nothing changed", file=sys.stderr)
        return ApplyExit.REFUSED
    progress = Progress()
    try:
        for plan in plans:
            _switch(engine, plan, progress)
        progress.step = "observe the result"
        mismatched = _unobserved(engine, plans, config)
    except compose.ComposeError as error:
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
    for plan in mismatched:
        print(
            f"{COMMAND}: partial, {plan.unit.key} is not {plan.action} after the change",
            file=sys.stderr,
        )
    if mismatched:
        return ApplyExit.REFUSED
    for plan in plans:
        print(f"{COMMAND}: {plan.unit.key} is {plan.action}")
    return ApplyExit.APPLIED_OR_NOTHING
