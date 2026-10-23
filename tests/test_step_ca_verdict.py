"""The step-ca probe's verdict (#346 P13): a cell that could not run is not held."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probe"))
sys.path.insert(0, str(ROOT / "probe" / "step-ca"))
sys.path.insert(0, str(ROOT / "src"))

import mcp_networks  # noqa: E402


def test_a_probe_with_a_cell_not_driven_is_incomplete_and_not_passed(
    capsys: pytest.CaptureFixture[str],
):
    exit_code = mcp_networks.print_verdict(["P13 (the image edge-agent-service is absent)"])

    output = capsys.readouterr().out
    assert exit_code == mcp_networks.INCOMPLETE_EXIT_CODE != 0
    assert "PROBE INCOMPLETE" in output
    assert "P13" in output
    assert "PROBE PASSED" not in output


def test_a_probe_with_every_cell_driven_passes(capsys: pytest.CaptureFixture[str]):
    assert mcp_networks.print_verdict([]) == 0
    assert "PROBE PASSED" in capsys.readouterr().out
