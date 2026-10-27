"""`hal stop <unit>` (#513): one row, the barrier and the stop as it observed them, and hal's exits.
Cells run in process through `hal.cli.main` on the fixtures of `test_hal_stack_commands`."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_edge_apply import (
    PROBE_ENABLED,
    STEP_CA_SERVICES,
    InterruptedWaitForLock,
    at_terminal,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    marker,
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    write_fleet,
)
from test_edge_apply_publish import repository  # noqa: F401 the fixture, requested by name below
from test_edge_external import (
    home,  # noqa: F401 the fixture, requested by name below
    live_checkout,  # noqa: F401 the fixture, requested by name below
)
from test_edge_global_stop import hosts_asked  # noqa: F401 the fixture, used by `fakes`
from test_hal_stack_commands import (
    UNIT,
    Fakes,
    Result,
    fakes,  # noqa: F401 the fixture, requested by name below
    hal,  # noqa: F401 the fixture, requested by name below
    system_commands,  # noqa: F401 the autouse guard that no real system command runs
    systemd,  # noqa: F401 the fixture, used by `fakes`
)

from edge import cli as stack
from edge import switches
from hal.grammar import ExitCode

# --- hal stop <unit> -------------------------------------------------------------------------


def squeezed(text: str) -> str:
    """The text with every run of blanks as one space, so a test names no column width."""
    return " ".join(text.split())


def assert_never_switched_off(result: Result) -> None:
    """The safety rule of a partial stop, at the row level: no `✓` and not the words, anywhere."""
    assert "✓" not in result.text
    assert "switched off" not in result.text


def assert_partial(result: Result, row: str, cause: str) -> None:
    """One `✗ … partial` row on stdout, then the error pair on stderr, and hal's exit 3."""
    assert result.exit_code == ExitCode.FAILURE
    assert squeezed(result.out) == f"✗ {UNIT} partial {row}"
    assert result.err.splitlines()[0].startswith(f"error: {UNIT} is only partly off · ")
    assert cause in result.err
    assert result.err.splitlines()[1:] == [f"next: hal stop {UNIT}"]
    assert_never_switched_off(result)


def probe_running(fakes: Fakes) -> None:  # noqa: F811
    write_fleet(fakes.repository, PROBE_ENABLED)
    fakes.scripted.containers = {"hello": "running"}


def interrupt_the_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stack,
        "switches",
        SimpleNamespace(
            Switches=InterruptedWaitForLock,
            default_state_directory=switches.default_state_directory,
        ),
    )


def unrecordable(directory: Path, unit: str = UNIT) -> None:
    """The switch of `unit` cannot be written, but the lock can be taken."""
    (directory / "stopped" / unit).mkdir(parents=True)


def test_stop_of_a_unit_with_no_host_prints_one_row_and_writes_the_marker(hal, fakes):  # noqa: F811
    """S1"""
    fakes.unit_in("A")

    result = hal("stop", UNIT)

    assert result.exit_code == ExitCode.OK
    assert squeezed(result.out) == (
        f"✓ {UNIT} switched off no host, so no barrier · stopped · volumes kept"
    )
    assert result.err == ""
    assert fakes.marker() is not None
    assert fakes.scripted.containers == dict.fromkeys(STEP_CA_SERVICES, "exited")


def test_stop_of_a_unit_behind_traefik_says_the_barrier_is_up(hal, fakes):  # noqa: F811
    probe_running(fakes)

    result = hal("stop", "probe")

    assert result.exit_code == ExitCode.OK
    assert squeezed(result.out) == "✓ probe switched off barrier up · stopped · volumes kept"
    assert result.err == ""


def test_stop_of_a_switched_off_unit_says_since_when_and_changes_nothing(hal, fakes):  # noqa: F811
    fakes.unit_in("B")

    result = hal("stop", UNIT)

    assert result.exit_code == ExitCode.OK
    assert squeezed(result.out) == f"· {UNIT} already off since 17.10. 22:10"
    assert result.err == ""
    assert fakes.scripted.changing_verbs() == []


def test_stop_of_a_switched_off_unit_names_the_barrier_it_put_back(hal, fakes):  # noqa: F811
    fakes.probe_switched_off_behind_a_barrier()
    fakes.ingress.barrier_file().unlink()

    result = hal("stop", "probe")

    assert result.exit_code == ExitCode.OK
    assert squeezed(result.out) == "✓ probe barrier up again off since 17.10. 22:10"


def test_stop_of_a_disabled_unit_that_nothing_runs_says_so(hal, fakes):  # noqa: F811
    fakes.unit_in("E")

    result = hal("stop", UNIT)

    assert result.exit_code == ExitCode.OK
    assert squeezed(result.out) == f"· {UNIT} disabled nothing of it runs"
    assert result.err == ""


def test_stop_of_an_unknown_unit_cannot_plan_and_writes_no_marker(hal, fakes):  # noqa: F811
    """S2"""
    result = hal("stop", "x")

    assert result.exit_code == ExitCode.FAILURE
    assert result.out == ""
    assert result.err.splitlines()[0].startswith(
        "error: cannot plan, x is not a unit of fleet.yaml"
    )
    assert result.err.splitlines()[1:] == ["next: hal stop x"]
    assert marker(fakes.state, "x") is None


def test_stop_that_fails_after_the_record_is_partial_with_the_switch_kept(hal, fakes):  # noqa: F811
    fakes.unit_in("A")
    fakes.scripted.failing_verb = "stop"

    result = hal("stop", UNIT)

    assert_partial(result, "switch recorded · no host, so no barrier · not stopped", "last line")
    assert fakes.marker() is not None


def test_stop_with_a_container_still_running_is_partial_and_keeps_the_marker(hal, fakes):  # noqa: F811
    """S4"""
    fakes.unit_in("A")
    fakes.scripted.verb_stop = lambda *_: ""

    result = hal("stop", UNIT)

    assert_partial(
        result,
        "switch recorded · no host, so no barrier · not stopped",
        "a container of it still runs",
    )
    assert fakes.marker() is not None


def test_stop_that_could_record_nothing_and_stop_nothing_is_partial(hal, fakes):  # noqa: F811
    fakes.unit_in("A")
    fakes.scripted.failing_verb = "stop"
    unrecordable(fakes.state)

    result = hal("stop", UNIT)

    assert_partial(
        result, "switch not recorded · no host, so no barrier · not stopped", "last line"
    )


def test_stop_that_stopped_but_could_not_record_says_apply_would_start_it_again(hal, fakes):  # noqa: F811
    fakes.unit_in("A")
    unrecordable(fakes.state)

    result = hal("stop", UNIT)

    assert_partial(
        result,
        "switch not recorded · no host, so no barrier · stopped, hal apply would start it again",
        "cannot read the switch of step-ca",
    )
    assert fakes.scripted.containers == dict.fromkeys(STEP_CA_SERVICES, "exited")


def test_stop_whose_lock_cannot_be_taken_is_partial_and_does_not_blame_the_record(hal, fakes):  # noqa: F811
    fakes.unit_in("A")
    (fakes.state / "switch.lock").mkdir(parents=True)

    result = hal("stop", UNIT)

    assert_partial(
        result,
        "switch recorded · no host, so no barrier · stopped · "
        "lock not taken, a start in progress may reopen it",
        "cannot take the switch lock",
    )


def test_stop_whose_barrier_traefik_rejects_is_partial_and_still_stops_the_unit(hal, fakes):  # noqa: F811
    probe_running(fakes)
    fakes.ingress.rejecting = True

    result = hal("stop", "probe")

    assert result.exit_code == ExitCode.FAILURE
    assert squeezed(result.out) == "✗ probe partial switch recorded · barrier not proven · stopped"
    assert "Traefik did not answer the barrier's 404" in result.err
    assert result.err.splitlines()[-1] == "next: hal stop probe"
    assert_never_switched_off(result)
    assert fakes.scripted.containers == {"hello": "exited"}


def test_stop_whose_barrier_is_not_proven_and_record_failed_says_both(hal, fakes):  # noqa: F811
    probe_running(fakes)
    fakes.ingress.down = True
    unrecordable(fakes.state, "probe")

    result = hal("stop", "probe")

    assert squeezed(result.out) == (
        "✗ probe partial switch not recorded · barrier not proven · "
        "stopped, hal apply would start it again"
    )
    assert_never_switched_off(result)


def test_stop_whose_barrier_is_not_proven_and_stop_failed_names_each_part(hal, fakes):  # noqa: F811
    probe_running(fakes)
    fakes.ingress.rejecting = True
    fakes.scripted.failing_verb = "stop"

    result = hal("stop", "probe")

    assert squeezed(result.out) == (
        "✗ probe partial switch recorded · barrier not proven · not stopped"
    )
    assert "Traefik did not answer the barrier's 404" in result.err
    assert "last line" in result.err
    assert_never_switched_off(result)


def test_stop_interrupted_while_waiting_for_a_switched_off_unit_keeps_the_marker(
    hal,  # noqa: F811
    fakes,  # noqa: F811
    monkeypatch,
):
    fakes.unit_in("B")
    interrupt_the_lock_wait(monkeypatch)

    result = hal("stop", UNIT)

    assert_partial(
        result, "interrupted while waiting · switch recorded", "interrupted while waiting"
    )
    assert fakes.marker() is not None


def test_stop_interrupted_while_waiting_for_the_switch_lock_keeps_the_marker(
    hal,  # noqa: F811
    fakes,  # noqa: F811
    monkeypatch,
):
    """S3 (E2)"""
    fakes.unit_in("A")
    interrupt_the_lock_wait(monkeypatch)

    result = hal("stop", UNIT)

    assert_partial(
        result, "interrupted while locking · switch recorded", "interrupted while locking"
    )
    assert "waiting to stop it" not in result.text
    assert fakes.marker() is not None


def test_stop_interrupted_while_raising_the_barrier_does_not_say_the_barrier_is_up(hal, fakes):  # noqa: F811
    probe_running(fakes)

    def interrupt() -> None:
        raise KeyboardInterrupt

    fakes.ingress.on_fetch = interrupt

    result = hal("stop", "probe")

    assert result.exit_code == ExitCode.FAILURE
    assert squeezed(result.out) == (
        "✗ probe partial interrupted while raising the barrier · switch recorded"
    )
    assert "barrier up" not in result.text
    assert_never_switched_off(result)


def test_stop_interrupted_while_stopping_keeps_what_was_proven(hal, fakes):  # noqa: F811
    fakes.unit_in("A")

    def interrupt(*_):
        raise KeyboardInterrupt

    fakes.scripted.verb_stop = interrupt

    result = hal("stop", UNIT)

    assert_partial(
        result,
        "interrupted while stopping · switch recorded · no host, so no barrier",
        "interrupted while stopping",
    )


def test_stop_without_a_terminal_switches_off_and_never_reads_stdin(hal, fakes):  # noqa: F811
    """S5: `hal` gives `UnreadableStdin` to a run without answers."""
    fakes.unit_in("A")

    result = hal("stop", UNIT)

    assert result.exit_code == ExitCode.OK
    assert "✓ step-ca" in result.out
    assert fakes.marker() is not None
