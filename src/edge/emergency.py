"""`edge stop <unit>` and `edge start <unit>`: the emergency switch of one unit; with `--all`, of
the tunnel (`stop_all`, `start_all`).

Stop records the switch, then stops the unit's services; it needs no terminal, so an agent may
run it (R4, D12). Start is the operator's step: a terminal and the exact `yes`. Exit codes are
`ApplyExit`'s. Containers and volumes are never removed here.
"""

import os
import sys
from collections.abc import Iterable
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from edge import apply, barrier, catalog, compose, setup, switches, terminal, tunnel, unit_plans
from edge.apply import ApplyExit

STOP = "edge stop"
START = "edge start"


def _say(command: str, line: str) -> None:
    print(f"{command}: {line}")


def _complain(command: str, line: str) -> None:
    print(f"{command}: {line}", file=sys.stderr)


def _read_fleet(command: str, engine: compose.Compose, repository: Path) -> unit_plans.Fleet | None:
    """The fleet; None, after saying why, when it cannot be planned."""
    try:
        return unit_plans.read_fleet(engine, repository)
    except (compose.ComposeError, catalog.CatalogError) as error:
        _complain(command, f"cannot plan, {error}")
        return None


def _read_unit(
    command: str, engine: compose.Compose, repository: Path, key: str
) -> tuple[unit_plans.Fleet, catalog.Unit] | None:
    """The fleet and the named unit; None, after saying why, when the unit cannot be planned."""
    fleet = _read_fleet(command, engine, repository)
    if fleet is None:
        return None
    unit = next((unit for unit in fleet.units if unit.key == key), None)
    if unit is None:
        known = ", ".join(sorted(unit.key for unit in fleet.units))
        _complain(command, f"cannot plan, {key} is not a unit of fleet.yaml (units: {known})")
        return None
    return fleet, unit


def stop_unit(
    marks: switches.Switches,
    key: str,
    now: datetime,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    verifier: barrier.Verifier = barrier.PROVEN_OVER_HTTP,
) -> int:
    engine = engine or compose.Compose(repository=repository)
    try:
        read = _read_unit(STOP, engine, repository, key)
        if read is None:
            return ApplyExit.CANNOT_PLAN
        fleet, unit = read
        services = fleet.profile_services[key]
        ingress = barrier.Barrier.of_unit(fleet.config, services, repository, key, verifier)
        running = compose.has_running_container(engine.states(), services)
    except (compose.ComposeError, barrier.BarrierError) as error:
        _complain(STOP, f"cannot plan, {error}")
        return ApplyExit.CANNOT_PLAN
    if not running and not unit.enabled:
        _say(STOP, f"{key} is disabled in the catalog and nothing of it runs")
        return ApplyExit.APPLIED_OR_NOTHING
    try:
        off = None if running else _confirmed_off(engine, marks, ingress, key, services, now)
    except KeyboardInterrupt:
        return _report_close(
            marks,
            key,
            services,
            _recorded(marks, key, now),
            None,
            "interrupted while waiting to stop it",
        )
    if off:
        since, repaired = off
        line = f"{key} is already switched off (since {switches.describe_since(since)})"
        if repaired:
            line += "; its barrier was missing and stands again"
        _say(STOP, line)
        return ApplyExit.APPLIED_OR_NOTHING
    return _close(engine, marks, ingress, key, services, now)


def _confirmed_off(
    engine: compose.Compose,
    marks: switches.Switches,
    ingress: barrier.Barrier,
    key: str,
    services: tuple[str, ...],
    now: datetime,
) -> tuple[str, bool] | None:
    """(switch time, barrier repaired) of a unit that is off, decided under the lock so no start
    is mid-`up`.

    The marker is rewritten unchanged, so a start waiting at its prompt sees that a stop came in,
    and the barrier is rendered again from it and proven, in case the file was lost.

    An unmarked unit is handed back to the stop unlocked, so the stop records before it waits.

    None when the unit is not switched off, or when anything stops us from being sure: then the
    stop closes it, and reports the record itself.
    """
    try:
        if marks.since(key) is None:
            return None  # nothing is recorded yet: the stop records first, then waits for the lock
        with marks.lock():
            if compose.has_running_container(engine.states(), services):
                return None
            if marks.since(key) is None:
                return None
            marks.record(key, now)
            repaired = ingress.close()
            since = marks.since(key)
            return (since, repaired) if since else None
    except (compose.ComposeError, switches.SwitchError, barrier.BarrierError):
        return None


def _recorded(marks: switches.Switches, key: str, now: datetime) -> switches.SwitchError | None:
    try:
        marks.record(key, now)
    except switches.SwitchError as error:
        return error
    return None


def _close_ingress(ingress: barrier.Barrier, key: str) -> tuple[str | None, str | None]:
    """(barrier failure, stop failure) of closing the ingress; the stop failure is an interrupt."""
    if not ingress.names:
        _say(STOP, f"{key}: no host behind Traefik, so no barrier")
    try:
        ingress.close()
    except barrier.BarrierError as error:
        return str(error), None
    except KeyboardInterrupt:
        return None, "interrupted while closing the ingress"
    return None, None


def _close(
    engine: compose.Compose,
    marks: switches.Switches,
    ingress: barrier.Barrier,
    key: str,
    services: tuple[str, ...],
    now: datetime,
) -> int:
    lock_failure: switches.SwitchError | None = None
    stop_failure: str | None = None
    with ExitStack() as held:
        record_failure = _recorded(marks, key, now)
        try:
            held.enter_context(marks.lock())
            # a start that cleared the marker meanwhile must not undo this
            record_failure = _recorded(marks, key, now)
        except switches.SwitchError as error:
            lock_failure = error
        except KeyboardInterrupt:
            stop_failure = "interrupted while waiting to stop it"
        barrier_failure: str | None = None
        if stop_failure is None:
            barrier_failure, stop_failure = _close_ingress(ingress, key)
        if stop_failure is None:
            try:
                engine.stop(key, services)
                if compose.has_running_container(engine.states(), services):
                    stop_failure = "a container of it still runs"
            except compose.ComposeError as error:
                stop_failure = f"could not stop it, {error}"
            except KeyboardInterrupt:
                stop_failure = "interrupted while stopping it"
    stop_failure = " and ".join(filter(None, [barrier_failure, stop_failure])) or None
    return _report_close(marks, key, services, record_failure, lock_failure, stop_failure)


def _recorded_time(marks: switches.Switches, key: str) -> str:
    try:
        return switches.describe_since(marks.since(key) or switches.UNKNOWN_SWITCH_TIME)
    except switches.SwitchError:
        return switches.UNKNOWN_SWITCH_TIME


def _report_close(
    marks: switches.Switches,
    key: str,
    services: tuple[str, ...],
    record_failure: switches.SwitchError | None,
    lock_failure: switches.SwitchError | None,
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
        _complain(
            STOP,
            f"partial: the switch is recorded (since {_recorded_time(marks, key)}); "
            f"{stop_failure}; run `{STOP} {key}` again",
        )
        return ApplyExit.REFUSED
    if lock_failure:
        _complain(
            STOP,
            f"partial: the switch is recorded and {key} is stopped, but the lock could not be "
            f"taken ({lock_failure}); a start in progress may reopen it; run `{STOP} {key}` again",
        )
        return ApplyExit.REFUSED
    _say(
        STOP,
        f"{key} is switched off (services {', '.join(services)} stopped; volumes stay)",
    )
    return ApplyExit.APPLIED_OR_NOTHING


def start_unit(
    marks: switches.Switches,
    key: str,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    verifier: barrier.Verifier = barrier.PROVEN_OVER_HTTP,
    check: setup.SetupCheck = setup.run_setup_check,
    watch: compose.Watch = compose.SETTLED,
) -> int:
    if terminal.refused_without_terminal(START):
        return ApplyExit.REFUSED
    engine = engine or compose.Compose(repository=repository)
    try:
        planned = _read_unit(START, engine, repository, key)
        if planned is None:
            return ApplyExit.CANNOT_PLAN
        fleet, unit = planned
        services = fleet.profile_services[key]
        ingress = barrier.Barrier.of_unit(fleet.config, services, repository, key, verifier)
        read = marks.mark(key)
    except (compose.ComposeError, switches.SwitchError, barrier.BarrierError) as error:
        _complain(START, f"cannot plan, {error}")
        return ApplyExit.CANNOT_PLAN
    if not unit.enabled:
        _complain(
            START,
            f"refused, {key} is disabled in the catalog; enable it by PR and `{apply.COMMAND}`",
        )
        return ApplyExit.REFUSED
    if not barrier.switched_off(read, ingress):
        _say(START, f"no change, {key} is not switched off")
        return ApplyExit.APPLIED_OR_NOTHING
    try:
        not_set_up = setup.refusal(unit, repository, check, f"{START} {key}")
    except setup.SetupUnclear as error:
        _complain(START, f"cannot start, {error}")
        return ApplyExit.CANNOT_PLAN
    if not_set_up:
        _complain(START, f"refused, {not_set_up}")
        return ApplyExit.REFUSED
    _say(
        START,
        f"{key}: start services {', '.join(services)}; "
        + (
            f"end the switch since {switches.describe_since(read.since)}"
            if read
            else "remove the barrier a stop left behind"
        ),
    )
    if not terminal.confirmed(f"start {key}"):
        _complain(START, "aborted, nothing changed")
        return ApplyExit.REFUSED
    return _open(engine, marks, ingress, key, services, read, fleet.config, watch)


def _switched_off_again_line(key: str, current: switches.Mark | None) -> str:
    """The switch is not the one start read, after the start ran."""
    if current is None:
        return f"partial: {key} was started, but its switch had already been ended meanwhile"
    return (
        f"partial: {key} was switched off again (since {switches.describe_since(current.since)}); "
        "it stays switched off"
    )


def _refused_before_up(key: str, current: switches.Mark | None) -> int:
    """Rule 5: the switch is not the one start read; a stop wins, another start made it moot."""
    if current is None:
        _say(START, f"{key} was started meanwhile")
        return ApplyExit.APPLIED_OR_NOTHING
    _complain(
        START,
        f"partial: {key} was switched off meanwhile "
        f"(since {switches.describe_since(current.since)}); not started",
    )
    return ApplyExit.REFUSED


def _open(
    engine: compose.Compose,
    marks: switches.Switches,
    ingress: barrier.Barrier,
    key: str,
    services: tuple[str, ...],
    read: switches.Mark | None,
    config: dict,
    watch: compose.Watch,
) -> int:
    try:
        with marks.lock():
            if changed := marks.changed(key, read):
                return _refused_before_up(key, changed.current)
            engine.start(key, services)
            if engine.down_while_watched(config, services, watch):
                _complain(START, f"partial, {key} is not on after the start; its switch stays")
                return ApplyExit.REFUSED
            try:
                ingress.open()
            except barrier.BarrierError as error:
                _complain(START, f"partial, {key} is on but {error}; its switch stays")
                return ApplyExit.REFUSED
            if read and not marks.clear(key, read):
                _complain(START, _switched_off_again_line(key, marks.mark(key)))
                return ApplyExit.REFUSED
    except (compose.ComposeError, switches.SwitchError) as error:
        _complain(START, f"partial, could not start {key}, {error}; its switch stays")
        return ApplyExit.REFUSED
    except KeyboardInterrupt:
        _complain(START, f"interrupted while starting {key}; its switch stays")
        return ApplyExit.REFUSED
    _say(START, f"{key} is on")
    return ApplyExit.APPLIED_OR_NOTHING


STOP_ALL = f"{STOP} --all"
START_ALL = f"{START} --all"


def _record_global(
    marks: switches.Switches, now: datetime, *, under_lock: bool
) -> switches.SwitchError | None:
    """Write the global switch; unlocked it never waits for a start that holds `switch.lock`."""
    try:
        with ExitStack() as held:
            if under_lock:
                held.enter_context(marks.lock())
            marks.record_global(now)
    except switches.SwitchError as error:
        return error
    except KeyboardInterrupt:
        return switches.SwitchError("interrupted while waiting for the lock")
    return None


def _global_time(marks: switches.Switches) -> str:
    try:
        mark = marks.global_mark()
    except switches.SwitchError:
        mark = None
    return switches.describe_since(mark.since if mark else switches.UNKNOWN_SWITCH_TIME)


def _named_unit(door: tunnel.Tunnel) -> str:
    """Empty for cloudflared; a probe's throwaway unit is named so the operator can tell."""
    return "" if door.unit == tunnel.CLOUDFLARED_UNIT else f"; unit {door.unit}"


def _ask_to_stop(door: tunnel.Tunnel) -> str | None:
    """Ask the system to stop the tunnel; what happened is read from the observation after it.

    Returns edge's own reason when the stop could not be asked, so the report can keep it.
    """
    # a refused or interrupted stop is judged by the state observed next, never assumed
    try:
        door.stop(may_ask_password=terminal.at_terminal())
    except tunnel.TunnelError as error:
        return str(error)
    except KeyboardInterrupt:
        pass
    return None


def _observed(door: tunnel.Tunnel) -> tuple[bool, bool, str | None]:
    """(stopped, boot protected, why it could not be observed) of the tunnel as it is now."""
    try:
        stopped = door.is_inactive()
        return stopped, door.has_boot_drop_in(), None
    except tunnel.TunnelError as error:
        return False, False, f"its state could not be read, {error}"


def _global_recorded(marks: switches.Switches) -> bool:
    try:
        return marks.global_mark() is not None
    except switches.SwitchError:
        return False


def _tunnel_state(stopped: bool, observation_failure: str | None) -> str:
    """The tunnel as observed; a state nobody could read is never called stopped or running."""
    if observation_failure:
        return f"is not confirmed stopped ({observation_failure})"
    return "is stopped" if stopped else "still runs"


def stop_all(marks: switches.Switches, now: datetime, door: tunnel.Tunnel) -> int:
    """Record the global switch first, stop the tunnel, record again under the lock, then report
    only what is observed.

    The first record is unlocked, so a start holding `switch.lock` through `up` cannot delay the
    stop; the second is under the lock, so a start that cleared the switch meanwhile cannot undo
    it. The units keep running on loopback and their switches are not touched.
    """
    _record_global(marks, now, under_lock=False)
    stop_failure = _ask_to_stop(door)
    lock_failure = _record_global(marks, now, under_lock=True)
    record_failure = lock_failure if not _global_recorded(marks) else None
    stopped, boot_protected, observation_failure = _observed(door)
    unit = door.unit.removesuffix(".service")
    tunnel_state = _tunnel_state(stopped, observation_failure)
    if record_failure:
        report = (
            f"the global switch could not be recorded ({record_failure}); "
            f"the tunnel {tunnel_state}, and a reboot reopens it"
        )
    else:
        report = f"the global switch is recorded, but the tunnel {tunnel_state}"
    if record_failure or not stopped:
        hint = "" if stopped else f"; `sudo systemctl stop {unit}`"
        _complain(STOP_ALL, f"partial: {report}{hint}")
        if stop_failure and not stopped:
            _complain(STOP_ALL, f"stop failed: {stop_failure}")
        return ApplyExit.REFUSED
    if not boot_protected:
        _complain(
            STOP_ALL,
            "incomplete: a reboot would reopen the tunnel (the boot drop-in is not installed "
            "yet; overnightworks/home-tools#9)",
        )
        return ApplyExit.REFUSED
    _say(
        STOP_ALL,
        f"the tunnel is stopped (since {_global_time(marks)}{_named_unit(door)}); "
        "services keep running locally",
    )
    return ApplyExit.APPLIED_OR_NOTHING


def _said_until_unproven(proofs: Iterable[barrier.ActiveProof]) -> str | None:
    """Say each proven barrier, one line each.

    The failure line (the tunnel stays closed) of the first barrier that cannot be proven.
    """
    for proof in proofs:
        match proof:
            case barrier.Proven(unit):
                _say(START_ALL, f"the barrier of {unit} is proven")
            case barrier.NoHost(unit):
                _say(START_ALL, f"{unit}: no host behind Traefik, so no barrier")
            case barrier.Unproven(unit, failure):
                return f"the tunnel stays closed: the barrier of {unit} is not proven ({failure})"
    return None


def _tunnel_closed_clause(door: tunnel.Tunnel) -> str:
    """Stop the tunnel if it runs, then say what was observed; never claim a closed state unseen."""
    try:
        if not door.is_inactive():
            door.stop(may_ask_password=terminal.at_terminal())
        if door.is_inactive():
            return "the tunnel is closed"
    except (tunnel.TunnelError, KeyboardInterrupt):
        pass  # the reply below says it was not observed
    return f"the tunnel is not confirmed closed; check `systemctl is-active {door.unit}`"


def _partial_closed(
    reason: str, recording_failure: switches.SwitchError | None, door: tunnel.Tunnel
) -> int:
    recorded = (
        f"the global switch could not be recorded again ({recording_failure})"
        if recording_failure
        else "the global switch is recorded again"
    )
    _complain(START_ALL, f"partial: {reason}; {recorded}; {_tunnel_closed_clause(door)}")
    return ApplyExit.REFUSED


def _stopped_during_prompt(current: switches.Mark | None) -> int:
    """A stop that came in while the operator was asked wins; another start made this one moot."""
    if current is None:
        _say(START_ALL, "the tunnel was started meanwhile")
        return ApplyExit.APPLIED_OR_NOTHING
    _complain(
        START_ALL,
        f"partial: the tunnel was switched off again meanwhile "
        f"(since {switches.describe_since(current.since)}); not started",
    )
    return ApplyExit.REFUSED


def start_all(
    marks: switches.Switches,
    now: datetime,
    door: tunnel.Tunnel,
    engine: compose.Compose | None = None,
    repository: Path = compose.REPOSITORY,
    verifier: barrier.Verifier = barrier.PROVEN_OVER_HTTP,
) -> int:
    """Lift only the global switch, and only when every barrier stands and the system allows it.

    Order: prove the barriers, ask the operator, get the system's authorization, then (under the
    lock, a stop during the prompt wins) clear the switch, start the tunnel and observe it.
    """
    if terminal.refused_without_terminal(START_ALL):
        return ApplyExit.REFUSED
    engine = engine or compose.Compose(repository=repository)
    fleet = _read_fleet(START_ALL, engine, repository)
    if fleet is None:
        return ApplyExit.CANNOT_PLAN
    try:
        read = marks.global_mark()
        if read is None and door.is_active():
            _say(START_ALL, "no change, the tunnel is not switched off")
            return ApplyExit.APPLIED_OR_NOTHING
        unproven = _said_until_unproven(
            barrier.prove_active(
                fleet.units, fleet.profile_services, fleet.config, marks, repository, verifier
            )
        )
    except (switches.SwitchError, tunnel.TunnelError) as error:
        _complain(START_ALL, f"cannot plan, {error}")
        return ApplyExit.CANNOT_PLAN
    if unproven:
        _complain(START_ALL, unproven)
        return ApplyExit.REFUSED
    _say(START_ALL, "host check not built yet (#50 SW3)")
    if not terminal.confirmed("reopen the tunnel"):
        _complain(START_ALL, "aborted, nothing changed")
        return ApplyExit.REFUSED
    return _reopen(marks, now, door, read)


def _reopen(
    marks: switches.Switches, now: datetime, door: tunnel.Tunnel, read: switches.Mark | None
) -> int:
    try:
        if not door.authorized_to_start(os.getpid()):
            return _partial_closed(
                "the system did not authorize the start",
                _record_global(marks, now, under_lock=True),
                door,
            )
        with marks.lock():
            if changed := marks.global_changed(read):
                return _stopped_during_prompt(changed.current)
            if read:
                marks.clear_global(read)
            started = door.start() and door.is_active()
            failure = None if started else "the tunnel did not start"
    except (switches.SwitchError, tunnel.TunnelError) as error:
        failure = f"could not reopen the tunnel, {error}"
    except KeyboardInterrupt:
        failure = "interrupted while reopening the tunnel"
    if failure:
        # the lock is released here, so recording again may take it
        return _partial_closed(failure, _record_global(marks, now, under_lock=True), door)
    _say(START_ALL, f"the tunnel is open{_named_unit(door)}; unit switches and barriers stay")
    return ApplyExit.APPLIED_OR_NOTHING
