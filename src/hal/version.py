"""`hal version`: which code runs. The commit and branch of the checkout this code lives in, whether
its tree is clean (as `edge apply` means it), and how it stands to the `origin/main` the last fetch
left. It never fetches, so it answers offline and changes nothing in the checkout."""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from edge import checkout
from hal.grammar import Asks, Command, ExitCode
from hal_output import printable, write_error
from hal_output.command import PROGRAM, Verb, command

# The tree this code lives in, looked up when the command runs: an installed wheel has none.
CODE_TREE = Path(__file__).resolve().parents[2]
ORIGIN = f"origin/{checkout.MAIN}"
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
        line = describe(checkout.Git(tree, runner))
    except checkout.GitError as error:
        write_error(
            f"cannot read the checkout {printable(str(tree))}, {error}", command(Verb.VERSION, "-v")
        )
        return ExitCode.FAILURE
    print(printable(line))
    return ExitCode.OK


def describe(git: checkout.Git) -> str:
    """The line `hal version` prints for a checkout; raises GitError."""
    commit = git.short()
    branch = git.branch()
    where = branch if branch else f"detached at {commit}"
    return " · ".join([f"{PROGRAM} {commit}", where, _tree(git.changed_paths()), _standing(git)])


def _tree(changed: Sequence[str]) -> str:
    return f"{len(changed)} changed" if changed else "clean"


def _standing(git: checkout.Git) -> str:
    if not git.has_fetched_main():
        return f"{ORIGIN} never fetched"
    ahead, behind = git.divergence(checkout.ORIGIN_MAIN)
    standing = checkout.standing_of(ahead, behind)
    if standing is checkout.Standing.CURRENT:
        return f"up to date with {ORIGIN}"
    if standing is checkout.Standing.BEHIND:
        return f"{behind} behind {ORIGIN}"
    if standing is checkout.Standing.AHEAD:
        return f"{ahead} ahead of {ORIGIN}"
    return f"diverged from {ORIGIN} ({ahead} ahead, {behind} behind)"


def _echoing(runner: checkout.Runner) -> checkout.Runner:
    """`runner` that first prints the git call it makes on stderr, never its environment."""

    def echo(
        tree: Path, arguments: Sequence[str], profile: checkout.Profile
    ) -> subprocess.CompletedProcess[bytes]:
        print(f"git {printable(' '.join(arguments))}", file=sys.stderr)
        return runner(tree, arguments, profile)

    return echo


COMMAND = Command(
    Verb.VERSION, "name the code that runs", run, configure, json=False, asks=Asks.NEVER
)
