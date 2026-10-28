"""`hal rename OLD NEW` asks one yes: what it shows before, what the answer decides, the plan made
once more after the yes, and the rows of the run. The person at the prompt is `Typist`."""

import pytest
from rename_support import SSH, make_clone, origin_of, tree_state, worktree_registrations

from hal import cli
from hal.grammar import ExitCode
from hal.rename_commands import RunningProcess

PROMPT = "Type yes to apply "


def after_the_prompt(out: str) -> list[str]:
    """The lines the run printed once the yes was typed; the prompt line has no newline."""
    _, prompt, rest = out.partition(PROMPT)
    _, _, run = rest.partition(":")
    return run.strip().splitlines() if prompt else []


def test_the_run_shows_its_plan_asks_one_yes_and_prints_the_rows_of_the_figure(
    everything, through_cli, fakes
):
    fakes.seconds_per_scan = 5
    fakes.seconds_per_uv_call = 7

    code, out, err = through_cli("old", "fresh")

    assert (code, err) == (ExitCode.OK, "")
    assert PROMPT + "7 changes: " in out
    assert after_the_prompt(out) == [
        "✓ clone moved ~/git/fresh",
        "✓ worktrees moved ~/git/fresh-worktrees",
        "✓ origin url set git@github.com:overnightworks/fresh.git",
        "✓ worktrees repaired 2 in ~/git/fresh-worktrees",
        "✓ memory moved Claude project memory of the clone",
        "✓ origin url set https://github.com/overnightworks/fresh.git · deploy checkout ~/old-live",
        "✓ venv synced in ~/git/fresh · 14s",
        "✓ rename done · 7 changed · 19s",
    ]


def test_a_single_step_reads_one_change(locked, through_cli):
    through_cli("old", "fresh")

    _, out, _ = through_cli("old", "fresh")

    assert PROMPT + "1 change: " in out


def test_a_finding_is_shown_once_and_does_not_count_as_a_change(home, repository, through_cli):
    link = home / ".local" / "bin" / "tool"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "tool")

    code, out, _ = through_cli("old", "fresh")

    assert code == ExitCode.FINDINGS
    assert out.count("! link check") == 1
    assert out.index("! link check") < out.index(PROMPT)
    assert PROMPT + "5 changes: " in out


def test_nothing_moves_before_the_yes(home, everything, repository, through_cli, fakes):
    deploy_checkout = home / "old-live"
    seen = []

    def snapshot():
        return (
            tree_state(home),
            origin_of(repository),
            origin_of(deploy_checkout),
            worktree_registrations(repository),
            list(fakes.uv_calls),
        )

    def look():
        seen.append(snapshot())

    before = snapshot()

    through_cli("old", "fresh", while_waiting=look)

    assert before[3]
    assert seen == [before]


@pytest.mark.parametrize("answer", ["no", "Yes", "y", " yes", "", "yes please", None])
def test_anything_but_an_exact_yes_declines_and_changes_nothing(
    home, repository, through_cli, answer
):
    before = tree_state(home)

    code, out, err = through_cli("old", "fresh", answer=answer)

    assert code == ExitCode.FINDINGS
    assert err == "error: aborted, nothing changed"
    assert after_the_prompt(out) == []
    assert tree_state(home) == before


def test_ctrl_c_at_the_prompt_ends_130_without_an_error_and_changes_nothing(
    home, repository, through_cli
):
    before = tree_state(home)

    code, _, err = through_cli("old", "fresh", answer=KeyboardInterrupt)

    assert (code, err) == (ExitCode.INTERRUPTED, "")
    assert tree_state(home) == before


@pytest.mark.parametrize("terminal", [True, False])
def test_nothing_to_change_asks_nothing_and_needs_no_terminal(home, through_cli, fakes, terminal):
    fakes.terminal = terminal

    code, out, err = through_cli("old", "fresh", answer=AssertionError)

    assert (code, out, err) == (ExitCode.OK, "· nothing to do, no clone of old to move", "")


def test_without_a_terminal_the_plan_is_shown_and_nothing_is_asked_or_moved(
    home, repository, through_cli, fakes
):
    fakes.terminal = False
    before = tree_state(home)

    code, out, err = through_cli("old", "fresh", answer=AssertionError)

    assert code == ExitCode.REFUSED
    assert "~ clone move ~/git/old → ~/git/fresh" in out
    assert PROMPT not in out
    assert err.splitlines() == [
        "error: rename asks yes at a terminal · nothing changed",
        "next: hal rename old fresh in your own terminal window",
    ]
    assert tree_state(home) == before


@pytest.mark.parametrize("terminal", [True, False])
def test_a_dry_run_shows_the_plan_and_neither_asks_nor_moves(
    home, repository, through_cli, fakes, terminal
):
    fakes.terminal = terminal
    before = tree_state(home)

    code, out, err = through_cli("old", "fresh", "--dry-run", answer=AssertionError)

    assert (code, err) == (ExitCode.OK, "")
    assert out.splitlines()[-1] == "· plan only, nothing changed · run hal rename old fresh"
    assert PROMPT not in out
    assert tree_state(home) == before


@pytest.mark.parametrize("flags", [("--apply",), ("--dry-run", "--apply")])
def test_apply_is_an_unrecognized_argument_and_moves_nothing(home, repository, through_cli, flags):
    before = tree_state(home)

    code, out, err = through_cli("old", "fresh", *flags)

    assert (code, out) == (ExitCode.USAGE, "")
    assert err.splitlines() == [
        "error: unrecognized arguments: --apply",
        "next: hal rename --help",
    ]
    assert tree_state(home) == before


def test_a_refusal_comes_before_the_terminal_check_and_the_prompt(repository, fakes, through_cli):
    fakes.terminal = False
    fakes.known.clear()

    code, _, err = through_cli("old", "fresh", answer=AssertionError)

    assert code == ExitCode.REFUSED
    assert (
        err == "error: cannot rename, GitHub does not know overnightworks/fresh · nothing changed"
    )


def test_a_session_that_starts_in_a_clone_while_the_prompt_waits_is_refused(
    home, repository, through_cli, fakes
):
    before = tree_state(home)

    def start_session():
        fakes.running = [RunningProcess(4242, "claude", repository / "src")]

    code, out, err = through_cli("old", "fresh", while_waiting=start_session)

    assert code == ExitCode.REFUSED
    assert "pid 4242 (claude) runs in " in err
    assert err.endswith("end that session first · nothing changed")
    assert after_the_prompt(out) == []
    assert tree_state(home) == before


def test_a_clone_that_appears_while_the_prompt_waits_changes_the_plan_and_moves_nothing(
    home, repository, through_cli
):
    def another_clone_appears():
        make_clone(home / "git" / "old-land", SSH.format("old"))

    code, out, err = through_cli("old", "fresh", while_waiting=another_clone_appears)

    assert code == ExitCode.REFUSED
    assert err == "error: cannot rename, the plan changed since it was shown · nothing changed"
    assert after_the_prompt(out) == []
    assert (home / "git" / "old").is_dir()
    assert origin_of(repository) == SSH.format("old")


def test_a_target_that_appears_while_the_prompt_waits_is_refused_as_existing(
    home, repository, through_cli
):
    def target_appears():
        (home / "git" / "fresh").mkdir()

    code, _, err = through_cli("old", "fresh", while_waiting=target_appears)

    assert code == ExitCode.REFUSED
    assert err.endswith(f"{home / 'git' / 'fresh'} already exists · nothing changed")
    assert (home / "git" / "old").is_dir()


def test_findings_that_change_while_the_prompt_waits_change_the_plan(
    home, repository, through_cli, fakes
):
    def tool_appears():
        fakes.tools = f"agent v1 [required: file://{repository}]"

    code, out, err = through_cli("old", "fresh", while_waiting=tool_appears)

    assert code == ExitCode.REFUSED
    assert err == "error: cannot rename, the plan changed since it was shown · nothing changed"
    assert after_the_prompt(out) == []
    assert (home / "git" / "old").is_dir()


def test_inputs_that_change_while_the_prompt_waits_change_the_plan_though_its_rows_do_not(
    home, locked, repository, through_cli, fakes
):
    pyvenv = locked / ".venv" / "pyvenv.cfg"
    pyvenv.write_text("version_info = 3.12.1\n")

    def python_changes():
        pyvenv.write_text("version_info = 3.13.0\n")

    code, out, err = through_cli("old", "fresh", while_waiting=python_changes)

    assert code == ExitCode.REFUSED
    assert err == "error: cannot rename, the plan changed since it was shown · nothing changed"
    assert after_the_prompt(out) == []
    assert fakes.uv_calls == []
    assert (home / "git" / "old").is_dir()
    assert origin_of(repository) == SSH.format("old")


def test_the_help_offers_dry_run_not_apply_and_does_not_name_sudo(capsys):
    with pytest.raises(SystemExit):
        cli.main(["rename", "--help"])

    help_text = capsys.readouterr().out
    assert "--dry-run" in help_text
    assert "--apply" not in help_text
    assert "sudo" not in help_text


def test_the_command_summary_names_the_yes(capsys):
    cli.main([])

    assert "asks yes" in " ".join(capsys.readouterr().out.split())


def test_hal_setup_help_still_says_that_sudo_checks_the_system_under_dry_run(capsys):
    with pytest.raises(SystemExit):
        cli.main(["setup", "--help"])

    assert "sudo" in capsys.readouterr().out
