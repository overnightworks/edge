"""Tests of what `home check` prints on the terminal (#493): one row per area, the verdict and the first step."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import REPORT_DISPLAY, RunnerWithCleanLogs, Sandbox, home_check, restarting_containers

from home_tools.layout import Layout


pytestmark = pytest.mark.usefixtures("quiet_pc")


@pytest.fixture
def clean_home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def runner() -> RunnerWithCleanLogs:
    return RunnerWithCleanLogs()


NOTHING_NEW_VERDICT = f"nothing new, no mail · report {REPORT_DISPLAY}"
NOTE_ROW = "  · secrets         1 note         Credentials are still missing"
ACTION_ROW = "  ! services        1 action       edge-a keeps restarting and is therefore unreachable"
OK_ROWS = {
    "services": "  ✓ services        ok",
    "backup": "  ✓ backup          ok",
    "commit hook": "  ✓ commit hook     ok",
    "secrets": "  ✓ secrets         ok",
    "server logs": "  ✓ server logs     ok",
}
FIRST_STEP = 'next: Tell an agent: "edge-a keeps restarting, please look"'


def arrange_clean(home, runner) -> None:
    pass


def arrange_an_action_and_a_note(home, runner) -> None:
    restarting_containers(runner, "edge-a")
    (home / ".secrets" / "gitlab").unlink()


def arrange_two_actions_in_one_area(home, runner) -> None:
    restarting_containers(runner, "edge-a", "edge-b")


def arrange_a_note_only(home, runner) -> None:
    (home / ".secrets" / "gitlab").unlink()


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        pytest.param(arrange_clean, [*OK_ROWS.values(), "✓ all well · 5 areas checked"], id="all well"),
        pytest.param(
            arrange_an_action_and_a_note,
            [
                ACTION_ROW,
                OK_ROWS["backup"],
                OK_ROWS["commit hook"],
                NOTE_ROW,
                OK_ROWS["server logs"],
                f"! 1 action, 1 note · mail sent · report {REPORT_DISPLAY}",
                FIRST_STEP,
            ],
            id="an action and a note",
        ),
        pytest.param(
            arrange_two_actions_in_one_area,
            [
                f"{ACTION_ROW.replace('1 action ', '2 actions')} · +1 more",
                OK_ROWS["backup"],
                OK_ROWS["commit hook"],
                OK_ROWS["secrets"],
                OK_ROWS["server logs"],
                f"! 2 actions · mail sent · report {REPORT_DISPLAY}",
                FIRST_STEP,
            ],
            id="two actions in one area",
        ),
        pytest.param(
            arrange_a_note_only,
            [
                OK_ROWS["services"],
                OK_ROWS["backup"],
                OK_ROWS["commit hook"],
                NOTE_ROW,
                OK_ROWS["server logs"],
                f"· 1 note · {NOTHING_NEW_VERDICT}",
            ],
            id="a note only",
        ),
    ],
)
def test_the_terminal_shows_one_row_per_area_and_the_verdict(clean_home, runner, mail_stub, capsys, arrange, expected) -> None:
    """#493: the blessed rows in the mail's order, then the verdict and the step of the first action."""
    arrange(clean_home, runner)

    home_check(clean_home, runner)

    assert capsys.readouterr().out.splitlines() == expected


def test_the_same_findings_on_the_next_run_say_that_nothing_new_went_out(clean_home, runner, mail_stub, capsys) -> None:
    arrange_an_action_and_a_note(clean_home, runner)
    home_check(clean_home, runner)
    capsys.readouterr()

    home_check(clean_home, runner)

    assert capsys.readouterr().out.splitlines()[-2:] == [f"! 1 action, 1 note · {NOTHING_NEW_VERDICT}", FIRST_STEP]


@pytest.mark.parametrize(
    ("mail_state", "session_active", "notify_enabled", "counts", "phrase"),
    [
        ("ok", True, True, "1 action", "mail sent"),
        ("absent", True, True, "1 action, 1 note", "news sent"),
        ("failing", True, True, "1 action", "mail failed, retries next run"),
        ("absent", False, True, "1 action, 1 note", "no mail set up"),
        ("ok", True, False, "1 action", "notifications off"),
    ],
)
def test_the_verdict_says_what_happened_to_the_news(clean_home, runner, mail_stub, capsys, mail_state, session_active, notify_enabled, counts, phrase) -> None:
    """#493: one phrase per delivery, true to what the channels did (mail that is not set up is a missing secret, so a note)."""
    restarting_containers(runner, "edge-a")
    runner.session_active = session_active
    if mail_state == "failing":
        mail_stub.exit_with(1)
    if mail_state == "absent":
        (Layout(clean_home).secrets_dir / "smtp" / "account.toml").unlink()
    if not notify_enabled:
        Layout(clean_home).config_file.parent.mkdir(parents=True)
        Layout(clean_home).config_file.write_text("[notify]\nenabled = false\n")

    home_check(clean_home, runner)

    assert f"! {counts} · {phrase} · report {REPORT_DISPLAY}" in capsys.readouterr().out.splitlines()
