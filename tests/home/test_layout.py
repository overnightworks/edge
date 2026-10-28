from __future__ import annotations

import sys

import pytest

from hal.workstation import layout
from hal.workstation.layout import run_command


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([sys.executable, "-c", "print('bereit')"], "bereit\n"),
        ([sys.executable, "-c", "raise SystemExit(3)"], None),
        (["home-no-such-tool"], None),
    ],
    ids=["a tool that succeeds", "a tool that fails", "a tool that is not installed"],
)
def test_only_a_tool_that_succeeds_hands_back_output(argv: list[str], expected: str | None) -> None:
    assert run_command(argv) == expected


def test_a_tool_that_hangs_is_killed_and_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "COMMAND_TIMEOUT_SECONDS", 0.2)

    assert run_command([sys.executable, "-c", "import time; time.sleep(30)"]) is None
