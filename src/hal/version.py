"""`hal version`: which code runs. The commit and branch of the checkout this code lives in, whether
its tree is clean (as `edge apply` means it), and how it stands to the `origin/main` the last fetch
left. It never fetches, so it answers offline and changes nothing in the checkout."""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from edge import checkout, compose
from hal.grammar import Command, ExitCode, write_error

# The tree this code lives in, looked up when the command runs: an installed wheel has none.
CODE_TREE = Path(__file__).resolve().parents[2]
NO_CHECKOUT = "no checkout (installed from a wheel)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each git call on stderr"
    )


def run(arguments: argparse.Namespace) -> ExitCode:
    tree = CODE_TREE
    if not (tree / ".git").exists():
        print(NO_CHECKOUT)
        return ExitCode.OK
    runner = _echoing(checkout.run_git) if arguments.verbose else checkout.run_git
    try:
        lines = describe(checkout.Git(tree, runner))
    except checkout.GitError as error:
        write_error(
            f"cannot read the checkout {compose.printable(str(tree))}, {error}", "hal version -v"
        )
        return ExitCode.FAILURE
    print("\n".join(lines))
    return ExitCode.OK


def describe(git: checkout.Git) -> list[str]:
    """The lines `hal version` prints for a checkout; raises GitError."""
    commit = git.short()
    branch = git.branch()
    return [
        f"commit: {commit}",
        f"branch: {compose.printable(branch) if branch else f'detached at {commit}'}",
        f"tree: {_tree(git.changed_paths())}",
        f"origin/{checkout.MAIN}: {_standing(git)}",
    ]


def _tree(changed: Sequence[str]) -> str:
    if not changed:
        return "clean"
    return f"{len(changed)} changed path{'s' if len(changed) > 1 else ''}"


def _standing(git: checkout.Git) -> str:
    if not git.has_fetched_main():
        return "never fetched"
    ahead, behind = git.divergence(checkout.ORIGIN_MAIN)
    standing = checkout.standing_of(ahead, behind)
    if standing is checkout.Standing.CURRENT:
        return "current"
    if standing is checkout.Standing.BEHIND:
        return f"{behind} behind"
    if standing is checkout.Standing.AHEAD:
        return f"{ahead} ahead"
    return f"diverged ({ahead} ahead, {behind} behind)"


def _echoing(runner: checkout.Runner) -> checkout.Runner:
    """`runner` that first prints the git call it makes on stderr, never its environment."""

    def echo(
        tree: Path, arguments: Sequence[str], profile: checkout.Profile
    ) -> subprocess.CompletedProcess[bytes]:
        print(f"git {compose.printable(' '.join(arguments))}", file=sys.stderr)
        return runner(tree, arguments, profile)

    return echo


COMMAND = Command("version", "name the code that runs", run, configure)
