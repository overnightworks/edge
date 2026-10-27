"""The containment probe's counting of B2 (c), its B3, B4 and B5 verdicts, the log classes and the
transparent wrapper (agent-runtime#440), on synthetic service logs and stub tools:
the probe's cells themselves run live under `unit.py --cell containment`."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from types import ModuleType

import pytest
from probe_support import probe_imports

OWNER_FAILED = "The owner of the relay of run r1 failed: CapabilityReflected at 3\n"
RUNNER_FAILED = "The runner of run r1 failed: CapabilityReflected at 3\n"
NOT_CONNECTED = "A CLI run's start announcement was refused (RELAY_NOT_CONNECTED)\n"
OTHER_REFUSAL = "A CLI run's start announcement was refused (TOOLS_UNCHANGED)\n"
LOGIN_REFUSED = "run r1 failed: CliLoginRefusedError\n"


@pytest.fixture(scope="module")
def containment() -> Iterator[ModuleType]:
    with probe_imports():
        import containment as module

        yield module


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


RUNS = 100
INIT = (1, 0, 1, 1, "n-init")
SERVICE = (SERVICE_PID, 1, SERVICE_PID, SERVICE_PID, "n-svc")
CENSUS = (90, 0, 90, 90, "n-py")  # `docker exec` starts the census with no parent in the namespace


def runner_lines(first: int = 0, count: int = RUNS) -> str:
    return "".join(
        f"ERROR agent_runtime_service.run_events The runner of run r{number} failed: "
        f"CliLoginRefusedError at 3\n"
        for number in range(first, first + count)
    )


def mount(containment, *paths, skips=None):
    return containment.Mount(
        tuple((path, "file") for path in paths), tuple(sorted((skips or {}).items()))
    )


def look(containment, *, processes=(INIT, SERVICE, CENSUS), mounts=None, listeners=3, log=""):
    mounts = (
        mounts
        if mounts is not None
        else {"/work": mount(containment), "/tmp": mount(containment, "a")}
    )
    return containment.Look(
        tuple(containment.Process(*record) for record in processes),
        mounts,
        listeners,
        containment.classify_log(log),
    )


def roots(containment):
    return containment.Roots(working="/work", temporary="/tmp")


def b5(containment, before, after, terminals=RUNS):
    verdicts = containment.b5_verdicts(before, after, terminals, roots(containment))
    return {cell: held for cell, (held, _) in verdicts.items()}


def after_the_runs(containment, **changes):
    return look(containment, log=runner_lines(), **changes)


def test_b5_holds_when_a_hundred_runs_leave_nothing_and_log_one_class(containment):
    verdicts = b5(containment, look(containment), after_the_runs(containment))
    assert verdicts == {
        "B5 (terminals)": True,
        "B5 (processes)": True,
        "B5 (mounts)": True,
        "B5 (listeners)": True,
        "B5 (log)": True,
    }


@pytest.mark.parametrize(
    ("cell", "changes"),
    [
        ("B5 (processes)", {"processes": (INIT, SERVICE, CENSUS, (41, 7, 41, 41, "n-x"))}),
        ("B5 (processes)", {"processes": (INIT, (8, 1, 8, 8, "n-svc"), CENSUS)}),
        ("B5 (listeners)", {"listeners": 4}),
        ("B5 (listeners)", {"listeners": 2}),
    ],
    ids=["a process remains", "the service pid changed", "a listener added", "a listener gone"],
)
def test_b5_fails_on_each_observation_alone(containment, cell, changes):
    verdicts = b5(containment, look(containment), after_the_runs(containment, **changes))
    assert [name for name, held in verdicts.items() if not held] == [cell]


def test_b5_working_root_or_tmp_difference_fails(containment):
    leftover = {"/work": mount(containment, "run"), "/tmp": mount(containment, "a")}
    changed_tmp = {
        "/work": mount(containment),
        "/tmp": mount(containment, "a", "cc-socks-1000"),
    }
    for mounts in (leftover, changed_tmp):
        verdicts = b5(containment, look(containment), after_the_runs(containment, mounts=mounts))
        assert not verdicts["B5 (mounts)"]


def test_b5_other_mount_difference_is_reported_not_failed(containment):
    before = look(
        containment,
        mounts={
            "/work": mount(containment),
            "/tmp": mount(containment, "a"),
            "/dev/shm": mount(containment),
        },
    )
    after = after_the_runs(
        containment,
        mounts={
            "/work": mount(containment),
            "/tmp": mount(containment, "a"),
            "/dev/shm": mount(containment, "n-1"),
        },
    )
    assert all(b5(containment, before, after).values())
    changed = containment.changed_mounts(before, after, roots(containment))
    assert changed == {"/dev/shm": (("n-1", "file"),)}


def test_b5_reports_cc_socks_in_tmp(containment):
    after = after_the_runs(
        containment,
        mounts={"/work": mount(containment), "/tmp": mount(containment, "a", "cc-socks-1000")},
    )
    verdicts = containment.b5_verdicts(look(containment), after, RUNS, roots(containment))
    assert (
        "cc-socks-1000 in /tmp after the runs: present (reported, not asserted)"
        in (verdicts["B5 (mounts)"][1])
    )


def test_b5_fails_when_a_run_reached_no_terminal(containment):
    held, evidence = containment.b5_terminals(37)["B5 (terminals)"]
    assert not held
    assert "37 of 100" in evidence


@pytest.mark.parametrize(
    "log",
    [
        runner_lines(count=99),
        runner_lines(count=101),
        runner_lines(count=99) + runner_lines(first=0, count=1),
        runner_lines() + "ERROR agent_runtime_service.quarantine The close step failed\n",
        runner_lines() + "ERROR: uvicorn trouble\n",
        runner_lines() + "CRITICAL agent_runtime_service.x gone\n",
        runner_lines() + "WARNING agent_runtime_service.x " + OTHER_REFUSAL,
        "bare line escaped logging\n" + runner_lines(),
    ],
    ids=[
        "99 runner lines",
        "101 runner lines",
        "a run id twice",
        "another ERROR",
        "a uvicorn ERROR",
        "a CRITICAL",
        "a refused announcement",
        "a bare line continuing no record",
    ],
)
def test_b5_log_fails_outside_the_contract(containment, log):
    verdicts = b5(containment, look(containment), look(containment, log=log))
    assert not verdicts["B5 (log)"]


def test_b5_log_counts_continuations_and_other_warnings_without_failing(containment):
    log = (
        runner_lines()
        + "WARNING agent_runtime_service.provider Claude run failed (rc=1)\n"
        + "Traceback (most recent call last):\n  File x\n"
        + "WARNING uvicorn.error something\n"
        + "WARNING:  uvicorn's own warning\n"
        + "WARNING third.party hello\n"
        + "INFO:     127.0.0.1 - GET /health 200\n"
    )
    after = look(containment, log=log)
    assert b5(containment, look(containment), after)["B5 (log)"]
    classes = after.log
    assert (classes.agent_warnings, classes.uvicorn_warnings, classes.other_warnings) == (1, 2, 1)
    assert classes.escaped == 0


def test_b5_run_ids_come_from_new_text_only(containment):
    earlier = look(containment, log=runner_lines(first=1000, count=1))
    later = look(containment, log=runner_lines(first=1000, count=1) + runner_lines())
    assert b5(containment, earlier, later)["B5 (log)"]


def test_b5_log_lines_follow_the_probes_handler_format(containment):
    import io
    import logging

    from service_log import LOG_FORMAT

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger = logging.Logger("agent_runtime_service.run_events")
    logger.addHandler(handler)
    logger.error("The runner of run %s failed: CliLoginRefusedError at %s", "r1", "3")
    assert containment.classify_log(stream.getvalue()).run_ids == ("r1",)


@pytest.mark.parametrize(
    ("names", "count"),
    [
        (["127.0.0.11:53", "[::1]:80", "[::ffff:127.0.0.1]:81"], 3),
        (["0.0.0.0:8000", "192.168.1.5:80", "[::]:8000", "[fe80::1]:1"], 0),
        (["127.255.0.1:9", "10.0.0.127:9"], 1),
    ],
    ids=["loopback in three forms", "no loopback", "all of 127/8 only"],
)
def test_loopback_listeners_counts_127_and_ipv6_loopback(containment, names, count):
    assert containment.loopback_listeners(names) == count


def test_b5_is_not_driven_when_the_working_root_or_tmp_skipped_something(containment):
    working_roots = roots(containment)
    for reason in ("replaced", "refused", "login excluded", "too deep"):
        skipped = {"/work": mount(containment, skips={reason: 1}), "/tmp": mount(containment)}
        seen = look(containment, mounts=skipped)
        with pytest.raises(containment.NotDriven):
            containment.expect_comparable(seen, seen, working_roots)


def test_b5_is_not_driven_when_any_skip_changed(containment):
    base = {"/work": mount(containment), "/tmp": mount(containment)}
    before = look(containment, mounts={**base, "/dev/shm": mount(containment)})
    after = look(
        containment, mounts={**base, "/dev/shm": mount(containment, skips={"vanished": 1})}
    )
    working_roots = roots(containment)
    with pytest.raises(containment.NotDriven):
        containment.expect_comparable(before, after, working_roots)


@pytest.mark.parametrize(
    "changes",
    [
        {"mounts": {"/work": ("run",), "/tmp": ()}},
        {"processes": (INIT, SERVICE, CENSUS, (41, 7, 41, 41, "n-x"))},
    ],
    ids=["working root not empty", "an extra process"],
)
def test_b5_is_not_driven_from_an_unclean_before_state(containment, changes):
    unclean = look(
        containment,
        **{
            key: (
                {m: mount(containment, *paths) for m, paths in value.items()}
                if key == "mounts"
                else value
            )
            for key, value in changes.items()
        },
    )
    working_roots = roots(containment)
    with pytest.raises(containment.NotDriven):
        containment.expect_clean_before(unclean, working_roots)


def test_b5_refused_token_is_not_driven(containment):
    output = "person token ok\nrefused with 401 invalid_token: expired\n"
    with pytest.raises(containment.NotDriven, match=r"refused with 401"):
        containment.reached_terminal(output, 41)


@pytest.mark.parametrize(
    ("output", "ended"),
    [
        ("event: started\nevent: failed\nprovider_error\n", True),
        ("event: succeeded\n", True),
        ("event: cancelled\n", True),
        ("event: started\n", False),
    ],
    ids=["failed", "succeeded", "cancelled", "no terminal"],
)
def test_b5_a_terminal_is_one_of_the_smokes_end_events(containment, output, ended):
    assert containment.reached_terminal(output, 1) is ended


def test_b5_is_not_driven_when_the_fake_app_saw_too_few_listings(containment):
    containment.expect_relay_attached(["m"] * 200, per_run=2, listing="m")
    with pytest.raises(containment.NotDriven):
        containment.expect_relay_attached(["m"] * 199, per_run=2, listing="m")


def test_every_minted_token_is_in_the_s1_list(containment):
    probe = object.__new__(containment.ContainmentProbe)
    probe.shown = ["output holding minted-late\n"]
    probe.minted = [
        containment.Tokens("minted-early", "w1"),
        containment.Tokens("minted-late", "w2"),
    ]
    probe.held = []
    probe.service_log = lambda: ""
    probe.app_lines = lambda: []
    with pytest.raises(containment.ProbeFailed, match="S1"):
        probe.expect_no_token("dummy")


def test_b5_prints_what_lets_a_reader_check_the_census(containment):
    before = look(containment)
    after = look(
        containment,
        log=runner_lines(first=1000, count=3),
        mounts={"/work": mount(containment), "/tmp": mount(containment, "a")},
    )
    verdicts = containment.b5_verdicts(before, after, RUNS, roots(containment))
    assert "first run r1000" in verdicts["B5 (log)"][1]
    assert "last run r1002" in verdicts["B5 (log)"][1]
    assert "/tmp before [a (file)], after [a (file)]" in verdicts["B5 (mounts)"][1]
    assert "(n-init, n-svc)" in verdicts["B5 (processes)"][1]
