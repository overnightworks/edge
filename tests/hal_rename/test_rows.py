"""`hal rename OLD NEW` through `hal.cli.main`: the rows it prints for its plan, its apply, its
findings and its refusals."""

import io
import sys

import pytest
from rename_support import HTTPS, make_clone, squeezed

from hal import cli
from hal.grammar import ExitCode


@pytest.fixture
def through_cli(cli_world, capsys):
    """`hal rename` through `hal.cli.main`, returning the exit code and the two streams."""

    def run(*arguments: str):
        code = cli.main(["rename", *arguments])
        captured = capsys.readouterr()
        return code, squeezed(captured.out), squeezed(captured.err)

    return run


@pytest.fixture
def everything(home, locked) -> None:
    """The clone with two worktrees, memory and a lock file, and a deploy checkout."""
    make_clone(home / "old-live", HTTPS.format("old"))


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
    code, out, _ = through_cli("old", "fresh")

    assert code == ExitCode.OK
    assert row in out.splitlines()


def test_the_plan_rows_follow_the_order_the_steps_run(everything, through_cli):
    _, out, _ = through_cli("old", "fresh")

    assert [line.split()[1] for line in out.splitlines()[:-2]] == [
        "clone",
        "worktrees",
        "origin",
        "worktrees",
        "memory",
        "origin",
        "venv",
    ]


def test_the_plan_ends_with_its_verdict_and_the_command_that_applies_it(everything, through_cli):
    code, out, _ = through_cli("old", "fresh")

    assert code == ExitCode.OK
    assert out.splitlines()[-2:] == [
        "· plan only, nothing changed",
        "next: hal rename old fresh --apply",
    ]


def test_a_repository_without_a_clone_has_nothing_to_do(home, through_cli):
    code, out, err = through_cli("old", "fresh")

    assert (code, out, err) == (ExitCode.OK, "· nothing to do, no clone of old to move", "")


def test_a_finding_is_a_row_with_its_subject_and_leaves_the_exit_at_findings(
    home, repository, through_cli
):
    link = home / ".local" / "bin" / "tool"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "tool")

    code, out, _ = through_cli("old", "fresh")

    assert code == ExitCode.FINDINGS
    assert "! link check ~/.local/bin/tool points into a moved clone" in out.splitlines()


class Terminal(io.StringIO):
    """A stream a person watches."""

    def isatty(self) -> bool:
        return True


def test_the_next_line_of_a_plan_is_coloured_like_every_command_on_a_terminal(
    everything, cli_world, monkeypatch
):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    out = Terminal()
    monkeypatch.setattr(sys, "stdout", out)

    cli.main(["rename", "old", "fresh"])

    assert out.getvalue().splitlines()[-1] == "\x1b[36mnext:\x1b[0m hal rename old fresh --apply"


def test_an_apply_with_a_finding_ends_with_a_needs_you_verdict_that_counts_it(
    home, repository, through_cli
):
    link = home / ".local" / "bin" / "tool"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "tool")

    code, out, _ = through_cli("old", "fresh", "--apply")

    assert code == ExitCode.FINDINGS
    assert out.splitlines()[-1].startswith("! renamed old → fresh · ")
    assert out.splitlines()[-1].endswith(", 1 finding")


def test_an_apply_prints_each_step_as_done_and_ends_with_the_count(everything, through_cli):
    code, out, err = through_cli("old", "fresh", "--apply")

    assert code == ExitCode.OK
    assert out.splitlines() == [
        "✓ clone moved ~/git/old → ~/git/fresh",
        "✓ worktrees moved ~/git/old-worktrees → ~/git/fresh-worktrees",
        "✓ origin url set git@github.com:overnightworks/fresh.git",
        "✓ worktrees repaired 2 in ~/git/fresh-worktrees",
        "✓ memory moved Claude project memory of the clone",
        "✓ origin url set https://github.com/overnightworks/fresh.git · deploy checkout ~/old-live",
        "✓ venv uv synced in ~/git/fresh",
        "✓ renamed old → fresh · 7 steps",
    ]
    assert err == ""


def test_a_refusal_says_why_nothing_changed_and_offers_no_next_step(repository, fakes, through_cli):
    fakes.known.clear()

    code, out, err = through_cli("old", "fresh", "--apply")

    assert code == ExitCode.REFUSED
    assert out == ""
    assert (
        err == "error: cannot rename, GitHub does not know overnightworks/fresh · nothing changed"
    )
