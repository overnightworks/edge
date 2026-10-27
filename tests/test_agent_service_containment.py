"""The containment probe's counting of B2 (c), its B3 and B4 verdicts and the transparent
wrapper (agent-runtime#440), on synthetic service logs and stub tools:
the probe's cells themselves run live under `unit.py --cell containment`."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE_MODULES = (
    "census_client",
    "containment",
    "egress",
    "issuer",
    "refusals",
    "smoke",
    "tree_census",
)

OWNER_FAILED = "The owner of the relay of run r1 failed: CapabilityReflected at 3\n"
RUNNER_FAILED = "The runner of run r1 failed: CapabilityReflected at 3\n"
NOT_CONNECTED = "A CLI run's start announcement was refused (RELAY_NOT_CONNECTED)\n"
OTHER_REFUSAL = "A CLI run's start announcement was refused (TOOLS_UNCHANGED)\n"
LOGIN_REFUSED = "run r1 failed: CliLoginRefusedError\n"


@pytest.fixture(scope="module")
def containment() -> Iterator[ModuleType]:
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    sys.path[:0] = [str(ROOT / "probe" / "agent-service"), str(ROOT / "agent-service")]
    try:
        import containment as module

        yield module
    finally:
        sys.path[:] = path_before
        for name in PROBE_MODULES:
            if name not in modules_before:
                sys.modules.pop(name, None)


def b2_verdicts(containment: ModuleType, log: str) -> dict[str, bool]:
    seen = containment.Observed(
        ["event: failed", "provider_error"], [], containment.LogCounts.of(log)
    )
    return {cell: held for cell, (held, _) in containment.b2_verdicts(seen).items()}


@pytest.mark.parametrize(
    "log",
    [OWNER_FAILED + RUNNER_FAILED, OWNER_FAILED + RUNNER_FAILED + NOT_CONNECTED],
    ids=["the observed run", "a tolerated RELAY_NOT_CONNECTED line"],
)
def test_b2_holds_when_the_runner_fails_on_the_reflected_capability(containment, log):
    assert b2_verdicts(containment, log)["B2 (c)"]


@pytest.mark.parametrize(
    "log",
    [
        OWNER_FAILED + RUNNER_FAILED + LOGIN_REFUSED,
        OWNER_FAILED,
        OWNER_FAILED + RUNNER_FAILED + OTHER_REFUSAL,
        OWNER_FAILED + RUNNER_FAILED + RUNNER_FAILED,
    ],
    ids=["reaches the login", "no runner line", "another verdict", "two runner lines"],
)
def test_b2_fails_outside_the_expected_run(containment, log):
    assert not b2_verdicts(containment, log)["B2 (c)"]


def test_counts_cover_only_lines_after_the_earlier_log(containment):
    earlier = containment.LogCounts.of(OWNER_FAILED + RUNNER_FAILED)
    later = containment.LogCounts.of(OWNER_FAILED + RUNNER_FAILED + LOGIN_REFUSED)
    assert later.since(earlier).login_refused == 1
    assert later.since(earlier).runner_failed == 0


def test_evidence_names_counts_and_never_the_smoke_lines(containment):
    canary = "canary-value-not-to-be-printed"
    seen = containment.Observed([canary], [], containment.LogCounts.of(""))
    evidence = [text for _, text in containment.b2_verdicts(seen).values()]
    evidence += [text for _, text in containment.b1_verdicts(seen).values()]
    assert not any(canary in text for text in evidence)


@pytest.mark.parametrize("reader", ["service_log", "app_lines", "wait_until_listening"])
def test_a_failed_log_read_reports_no_output(containment, reader):
    marker = "SECRET-LOG-LINE-MARKER"
    probe = object.__new__(containment.ContainmentProbe)
    probe.container = lambda service: "container-id"

    def failed_run(command, *, check=True, **_):
        if check:  # as egress.Probe.run does: the output's last line goes into the message
            raise containment.ProbeFailed(f"{command[:5]} exited 1: {[marker]}")
        return subprocess.CompletedProcess(command, 1, stdout=marker + "\n", stderr=marker + "\n")

    probe.run = failed_run
    read = getattr(probe, reader)
    with pytest.raises(containment.ProbeFailed) as raised:
        read()
    assert marker not in str(raised.value)
    assert "exited 1" in str(raised.value)


SLEEP = "n-5ee9"
SERVICE_PID = 7
CLI_PID = 40


def process(containment, pid, ppid, pgid, sid, name=SLEEP):
    return containment.Process(pid, ppid, pgid, sid, name)


def snapshot(containment, *processes, working_root=0, temporary=0):
    return containment.Snapshot(tuple(processes), SLEEP, working_root, temporary)


def planted(containment, a_parent=CLI_PID, b_parent=CLI_PID):
    """The CLI (its own group), marker A (in its group) and marker B (in a session of its own)."""
    return (
        process(containment, CLI_PID, SERVICE_PID, CLI_PID, CLI_PID, "n-c11c"),
        process(containment, 41, a_parent, CLI_PID, CLI_PID),
        process(containment, 42, b_parent, 42, 42),
    )


def b3(containment, window, frame, later):
    verdicts = containment.b3_verdicts(window, frame, later)
    return {cell: held for cell, (held, _) in verdicts.items()}


def test_b3_holds_when_a_dies_with_the_run_and_b_lives_on_under_init(containment):
    window = snapshot(containment, *planted(containment))
    frame = snapshot(containment, process(containment, 42, CLI_PID, 42, 42), working_root=3)
    later = snapshot(containment, process(containment, 42, 1, 42, 42))
    assert b3(containment, window, frame, later) == {
        "B3 (during)": True,
        "B3 (frame)": True,
        "B3 (6 s)": True,
    }


@pytest.mark.parametrize(
    ("survivors", "working_root"),
    [
        ([(41, CLI_PID, CLI_PID, CLI_PID), (42, 1, 42, 42)], 0),
        ([(42, 1, 42, 42)], 2),
        ([], 0),
        ([(42, CLI_PID, 42, 42)], 0),
        ([(42, 1, 42, 42), (43, 1, 43, 43)], 0),
        ([(41, 1, CLI_PID, CLI_PID), (42, 1, 42, 42)], 0),
    ],
    ids=[
        "A survives",
        "the run tree survives",
        "B is dead",
        "B keeps the CLI as parent",
        "two Bs",
        "A survives orphaned under init",
    ],
)
def test_b3_fails_six_seconds_after_the_frame_when_the_run_left_something_else(
    containment, survivors, working_root
):
    window = snapshot(containment, *planted(containment))
    later = snapshot(
        containment,
        *(process(containment, *record) for record in survivors),
        working_root=working_root,
    )
    assert not b3(containment, window, snapshot(containment), later)["B3 (6 s)"]


def test_b3_at_the_frame_fails_only_when_b_is_dead(containment):
    window = snapshot(containment, *planted(containment))
    alive = snapshot(containment, process(containment, 42, 1, 42, 42))
    verdicts = b3(containment, window, alive, alive)
    assert verdicts["B3 (frame)"]
    assert not b3(containment, window, snapshot(containment), alive)["B3 (frame)"]


def test_b3_is_not_driven_when_no_marker_was_seen(containment):
    nothing_seen = snapshot(containment)
    with pytest.raises(containment.NotDriven):
        containment.expect_planted(nothing_seen)


@pytest.mark.parametrize("missing", ["A", "B"])
def test_b3_is_not_driven_when_one_marker_was_not_seen(containment, missing):
    seen = [r for r in planted(containment) if (r.pid == 41) != (missing == "A")]
    window = snapshot(containment, *seen)
    with pytest.raises(containment.NotDriven, match=f"marker {missing}"):
        containment.expect_planted(window)


@pytest.mark.parametrize(
    "marker_a",
    [(41, CLI_PID, 7, 7), (41, CLI_PID, 41, CLI_PID)],
    ids=["A in the service's group", "A in a group of its own"],
)
def test_b3_is_not_driven_when_a_is_not_in_the_clis_group(containment, marker_a):
    cli = process(containment, CLI_PID, 7, 7, 7, "n-c11c")
    seen = (cli, process(containment, *marker_a), process(containment, 42, CLI_PID, 42, 42))
    window = snapshot(containment, *seen)
    with pytest.raises(containment.NotDriven, match="marker A"):
        containment.expect_planted(window)


def test_a_marker_is_told_from_the_cli_by_its_name(containment):
    """B3: only the opaque marker name counts as a marker."""
    other = process(containment, 50, CLI_PID, CLI_PID, CLI_PID, "n-0000")
    assert snapshot(containment, other).stray_markers() == ()


def b4(containment, dies=1, before=0, after=1, host=0, restarted=None):
    restarted = restarted or snapshot(containment)
    verdicts = containment.b4_verdicts(dies, before, after, host, restarted)
    return {cell: held for cell, (held, _) in verdicts.items()}


def test_b4_holds_when_the_service_died_once_and_left_no_marker_or_file(containment):
    assert b4(containment) == {"B4 (die)": True, "B4 (gone)": True, "B4 (empty)": True}


@pytest.mark.parametrize(
    ("cell", "arguments"),
    [
        ("B4 (die)", {"dies": 0}),
        ("B4 (die)", {"after": 0}),
        ("B4 (die)", {"before": 1, "after": 3}),
        ("B4 (gone)", {"host": 1}),
        ("B4 (gone)", {"restarted": "marker"}),
        ("B4 (empty)", {"restarted": "working root"}),
        ("B4 (empty)", {"restarted": "temporary"}),
    ],
    ids=[
        "no die event",
        "RestartCount unchanged",
        "restarted twice",
        "host marker",
        "container marker",
        "working root not empty",
        "/tmp not empty",
    ],
)
def test_b4_fails_on_each_condition_alone(containment, cell, arguments):
    restarted = {
        "marker": snapshot(containment, process(containment, 9, 1, 9, 9)),
        "working root": snapshot(containment, working_root=1),
        "temporary": snapshot(containment, temporary=2),
    }
    if "restarted" in arguments:
        arguments = {"restarted": restarted[arguments["restarted"]]}
    verdicts = b4(containment, **arguments)
    assert [name for name, held in verdicts.items() if not held] == [cell]


def test_the_service_is_the_one_process_below_init(containment):
    """B4: the service's pid is the one process below init."""
    init = process(containment, 1, 0, 1, 1, "n-1")
    service = process(containment, SERVICE_PID, 1, SERVICE_PID, SERVICE_PID, "n-5")
    child = process(containment, 30, SERVICE_PID, SERVICE_PID, SERVICE_PID, "n-6")
    assert snapshot(containment, init, service, child).service_pids() == (SERVICE_PID,)


def test_the_configuration_runs_the_wrapper_in_place_of_the_cli(containment):
    """#438 transparent wrapper: B1/B2 run through it, in place of the CLI."""
    text = '[cli.claude]\nbinary = "/opt/claude/claude"\nmcp_origin = "https://x"\n'
    assert f'binary = "{containment.WRAPPER}"' in containment.with_containment(text)


def wrapper(containment, tmp_path, *, cli_body):
    directory = tmp_path / "wrapper"
    directory.mkdir()
    cli = tmp_path / "cli"
    cli.write_text("#!/bin/sh\n" + cli_body, encoding="utf-8")
    cli.chmod(0o755)
    program = directory / "claude"
    program.write_text(containment.wrapper_text(str(cli)), encoding="utf-8")
    program.chmod(0o755)
    return program, directory


def stub_tools(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (("sleep", 'echo "sleep $*" > "$LOG"'), ("setsid", 'exec "$@"')):
        (tools / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (tools / name).chmod(0o755)
    return tools


@pytest.mark.parametrize(
    "arguments",
    [["--version"], ["-p", "--model", "opus"]],
    ids=["version", "a run without the flag file"],
)
def test_the_wrapper_passes_the_arguments_on_and_plants_nothing_without_the_flag_file(
    containment, tmp_path, arguments
):
    """#438 transparent wrapper: arguments pass on and nothing is planted without the flag."""
    program, _ = wrapper(containment, tmp_path, cli_body='echo "$@" > "$OUT"\n')
    out = tmp_path / "out"
    environment = {
        "PATH": f"{stub_tools(tmp_path)}:/usr/bin:/bin",
        "OUT": str(out),
        "LOG": str(tmp_path / "unused"),
    }
    subprocess.run([str(program), *arguments], env=environment, check=True, timeout=30)
    assert out.read_text(encoding="utf-8").split() == arguments
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize(
    ("arguments", "planted_count"),
    [(["-p", "--model", "opus"], 2), (["--version"], 0)],
    ids=["a run", "the version probe"],
)
def test_the_wrapper_plants_two_markers_on_a_run_only_when_the_flag_file_is_there(
    containment, tmp_path, arguments, planted_count
):
    """#438 transparent wrapper: `-p` plants A and B; `--version` plants nothing, proven by a
    flag file that blocks any reader."""
    cli_body = (
        'exec 3<>"$LOG"\n'
        'for _ in $(seq "$WAIT"); do read -r line <&3; echo "$line" >> "$OUT"; done\n'
    )
    program, directory = wrapper(containment, tmp_path, cli_body=cli_body)
    flag = directory / containment.PLANT_FLAG
    if planted_count:
        flag.write_text("3000.11 3000.22\n", encoding="utf-8")
    else:
        os.mkfifo(flag)
    log = tmp_path / "log"
    os.mkfifo(log)
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    environment = {
        "PATH": f"{stub_tools(tmp_path)}:/usr/bin:/bin",
        "OUT": str(out),
        "LOG": str(log),
        "WAIT": str(planted_count),
    }
    subprocess.run([str(program), *arguments], env=environment, check=True, timeout=30)
    assert (
        sorted(out.read_text(encoding="utf-8").split("\n")[:-1])
        == (["sleep 3000.11", "sleep 3000.22"][:planted_count])
    )
