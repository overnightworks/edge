"""The containment probe's counting of B2 (c) and its diagnostics (agent-runtime#440), on
synthetic service logs: the probe's cells themselves run live under `unit.py --cell containment`."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE_MODULES = ("containment", "egress", "issuer", "refusals", "smoke")

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
