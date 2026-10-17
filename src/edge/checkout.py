"""The live checkout: `~/git/edge` on `main` is the tree the live stack runs from (P6).

`edge apply` binds the compose project `edge` to the tree its code runs from, so it runs only
from the live checkout (#79 rule 1). Whether a tree is that checkout is read from paths alone,
without git: it is `$HOME/git/edge` once both are resolved, and it is a main worktree, whose `.git`
is a directory where a linked worktree has a `.git` file.
"""

from pathlib import Path

LIVE_CHECKOUT = "~/git/edge"


def live_checkout(home: Path) -> Path:
    return home / "git" / "edge"


def is_live_checkout(tree: Path, home: Path) -> bool:
    return tree.resolve() == live_checkout(home).resolve() and (tree / ".git").is_dir()
