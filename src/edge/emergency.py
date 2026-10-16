"""`edge stop <unit>` and `edge start <unit>`: the emergency switch of one unit.

Stop records the switch, then stops the unit's services; it needs no terminal, so an agent may
run it (R4, D12). Start is the operator's step: a terminal and the exact `yes`. Exit codes are
`ApplyExit`'s. Containers and volumes are never removed here.
"""

import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from edge import apply, catalog, compose, switches, terminal
from edge.apply import ApplyExit

STOP = "edge stop"
START = "edge start"


def _say(command: str, line: str) -> None:
    print(f"{command}: {line}")


def _complain(command: str, line: str) -> None:
    print(f"{command}: {line}", file=sys.stderr)


def _read_unit(
    command: str, engine: compose.Compose, repository: Path, key: str
) -> tuple[apply.Fleet, catalog.Unit] | None:
    """The fleet and the named unit; None, after saying why, when the unit cannot be planned."""
    try:
        fleet = apply.read_fleet(engine, repository)
    except (compose.ComposeError, catalog.CatalogError) as error:
        _complain(command, f"cannot plan, {error}")
        return None
    unit = next((unit for unit in fleet.units if unit.key == key), None)
    if unit is None:
        known = ", ".join(sorted(unit.key for unit in fleet.units))
        _complain(command, f"cannot plan, {key} is not a unit of fleet.yaml (units: {known})")
        return None
    return fleet, unit


def stop_unit(
    state_directory: Path,
    key: str,
    now: datetime,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
) -> int:
    engine = engine or compose.Compose(repository=repository)
    marks = switches.Switches(state_directory)
    try:
        read = _read_unit(STOP, engine, repository, key)
        if read is None:
            return ApplyExit.CANNOT_PLAN
        fleet, unit = read
        services = fleet.profile_services[key]
        running = compose.has_running_container(engine.states(), services)
        since = marks.since(key)
    except (compose.ComposeError, switches.SwitchError) as error:
        _complain(STOP, f"cannot plan, {error}")
        return ApplyExit.CANNOT_PLAN
    if not running and since:
        _say(STOP, f"{key} is already switched off (since {switches.describe_since(since)})")
        return ApplyExit.APPLIED_OR_NOTHING
    if not running and not unit.enabled:
        _say(STOP, f"{key} is disabled in the catalog and nothing of it runs")
        return ApplyExit.APPLIED_OR_NOTHING
    return _close(engine, marks, key, services, now)


def _close(
    engine: compose.Compose,
    marks: switches.Switches,
    key: str,
    services: tuple[str, ...],
    now: datetime,
) -> int:
    record_failure: switches.SwitchError | None = None
    stop_failure: str | None = None
    with ExitStack() as held:
        try:
            marks.record(key, now)
            held.enter_context(marks.lock())
            marks.record(key, now)  # a start that cleared the marker meanwhile must not undo this
        except switches.SwitchError as error:
            record_failure = error
        try:
            engine.stop(key, services)
            if compose.has_running_container(engine.states(), services):
                stop_failure = "a container of it still runs"
        except compose.ComposeError as error:
            stop_failure = f"could not stop it, {error}"
        except KeyboardInterrupt:
            stop_failure = "interrupted while stopping it"
    return _report_close(marks, key, services, record_failure, stop_failure)


def _report_close(
    marks: switches.Switches,
    key: str,
    services: tuple[str, ...],
    record_failure: switches.SwitchError | None,
    stop_failure: str | None,
) -> int:
    if record_failure and stop_failure:
        _complain(
            STOP,
            f"partial: the switch could not be recorded ({record_failure}) and {stop_failure}; "
            f"run `{STOP} {key}` again",
        )
        return ApplyExit.REFUSED
    if record_failure:
        _complain(
            STOP,
            f"partial: {key} is stopped, but the switch could not be recorded "
            f"({record_failure}); `edge apply` would start it again",
        )
        return ApplyExit.REFUSED
    if stop_failure:
        since = switches.describe_since(marks.since(key) or "")
        _complain(
            STOP,
            f"partial: the switch is recorded (since {since}), but {stop_failure}; "
            f"run `{STOP} {key}` again",
        )
        return ApplyExit.REFUSED
    _say(
        STOP,
        f"{key} is switched off (services {', '.join(services)} stopped; volumes stay)",
    )
    return ApplyExit.APPLIED_OR_NOTHING


def start_unit(
    state_directory: Path,
    key: str,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
) -> int:
    if terminal.refused_without_terminal(START):
        return ApplyExit.REFUSED
    engine = engine or compose.Compose(repository=repository)
    marks = switches.Switches(state_directory)
    try:
        read = _read_unit(START, engine, repository, key)
        if read is None:
            return ApplyExit.CANNOT_PLAN
        fleet, unit = read
        since = marks.since(key)
    except (compose.ComposeError, switches.SwitchError) as error:
        _complain(START, f"cannot plan, {error}")
        return ApplyExit.CANNOT_PLAN
    if not unit.enabled:
        _complain(
            START,
            f"refused, {key} is disabled in the catalog; enable it by PR and `{apply.COMMAND}`",
        )
        return ApplyExit.REFUSED
    if since is None:
        _say(START, f"no change, {key} is not switched off")
        return ApplyExit.APPLIED_OR_NOTHING
    services = fleet.profile_services[key]
    _say(
        START,
        f"{key}: start services {', '.join(services)}; "
        f"end the switch since {switches.describe_since(since)}",
    )
    if not terminal.confirmed(f"start {key}"):
        _complain(START, "aborted, nothing changed")
        return ApplyExit.REFUSED
    return _open(engine, marks, key, services, since, fleet.config)


def _open(
    engine: compose.Compose,
    marks: switches.Switches,
    key: str,
    services: tuple[str, ...],
    since: str,
    config: dict,
) -> int:
    try:
        with marks.lock():
            engine.start(key, services)
            observed_on = compose.is_up(engine.states(), config, services)
            if not observed_on:
                _complain(START, f"partial, {key} is not on after the start; its switch stays")
                return ApplyExit.REFUSED
            if not marks.clear(key, since):
                again = switches.describe_since(marks.since(key) or "")
                _complain(
                    START,
                    f"partial: {key} was switched off again (since {again}); it stays switched off",
                )
                return ApplyExit.REFUSED
    except (compose.ComposeError, switches.SwitchError) as error:
        _complain(START, f"partial, could not start {key}, {error}; its switch stays")
        return ApplyExit.REFUSED
    except KeyboardInterrupt:
        _complain(START, f"interrupted while starting {key}; its switch stays")
        return ApplyExit.REFUSED
    _say(START, f"{key} is on")
    return ApplyExit.APPLIED_OR_NOTHING
