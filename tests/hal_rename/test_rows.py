"""`hal rename OLD NEW` through `hal.cli.main`: the rows it prints for its plan, its apply, its
findings and its refusals."""

import pytest
from terminal_support import put_stdout_on_a_terminal, type_at_prompt

from hal import cli
from hal.grammar import ExitCode


@pytest.mark.parametrize(
    "row",
    [
        "~ clone move ~/git/old → ~/git/fresh",
        "~ worktrees move ~/git/old-worktrees → ~/git/fresh-worktrees",
        "~ worktrees repair 2 in ~/git/fresh-worktrees",
        "~ origin set url git@github.com:overnightworks/fresh.git",
        "~ origin set url https://github.com/overnightworks/fresh.git · deploy checkout ~/old-live",
        "~ memory move Claude project memory of the clone",
        "~ venv uv sync in ~/git/fresh",
    ],
)
def test_the_plan_shows_one_row_for_each_kind_of_step(everything, through_cli, row):
    code, out, _ = through_cli("old", "fresh", "--dry-run")

    assert code == ExitCode.OK
    assert row in out.splitlines()


def test_the_plan_rows_follow_the_order_the_steps_run(everything, through_cli):
    _, out, _ = through_cli("old", "fresh", "--dry-run")

    assert [line.split()[1] for line in out.splitlines()[:-1]] == [
        "clone",
        "worktrees",
        "origin",
        "worktrees",
        "memory",
        "origin",
        "venv",
    ]


def test_a_dry_run_ends_with_its_verdict_and_the_command_that_runs_the_plan(
    everything, through_cli
):
    code, out, _ = through_cli("old", "fresh", "--dry-run")

    assert code == ExitCode.OK
    assert out.splitlines()[-1] == "· plan only, nothing changed · run hal rename old fresh"


def test_a_repository_without_a_clone_has_nothing_to_do(home, through_cli):
    code, out, err = through_cli("old", "fresh")

    assert (code, out, err) == (ExitCode.OK, "· nothing to do, no clone of old to move", "")


def test_a_finding_is_a_row_with_its_subject_and_leaves_the_exit_at_findings(
    home, repository, through_cli
):
    link = home / ".local" / "bin" / "tool"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "tool")

    code, out, _ = through_cli("old", "fresh", "--dry-run")

    assert code == ExitCode.FINDINGS
    assert "! link check ~/.local/bin/tool points into a moved clone" in out.splitlines()


def test_the_yes_is_bold_in_the_prompt_on_a_terminal(everything, cli_world, monkeypatch):
    out = put_stdout_on_a_terminal(monkeypatch)
    type_at_prompt(monkeypatch)

    cli.main(["rename", "old", "fresh"])

    assert "Type \x1b[1myes\x1b[0m to apply 7 changes: " in out.getvalue()


def test_the_yes_is_plain_without_colour(everything, cli_world, monkeypatch, capsys):
    type_at_prompt(monkeypatch)

    cli.main(["rename", "old", "fresh"])

    assert "Type yes to apply 7 changes: " in capsys.readouterr().out


def test_a_run_with_a_finding_ends_with_a_needs_you_verdict_that_counts_it(
    home, repository, through_cli
):
    link = home / ".local" / "bin" / "tool"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "tool")

    code, out, _ = through_cli("old", "fresh")

    assert code == ExitCode.FINDINGS
    assert out.splitlines()[-1] == "! rename done · 5 changed, 1 finding · <1s"


def test_a_refusal_says_why_nothing_changed_and_offers_no_next_step(repository, fakes, through_cli):
    fakes.known.clear()

    code, out, err = through_cli("old", "fresh")

    assert code == ExitCode.REFUSED
    assert out == ""
    assert (
        err == "error: cannot rename, GitHub does not know overnightworks/fresh · nothing changed"
    )
