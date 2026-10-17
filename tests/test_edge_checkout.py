"""The live checkout `$HOME/git/edge` as a main worktree, judged from paths alone: every tree is
built under `tmp_path`, and the HOME is a parameter, never the real one."""

from pathlib import Path

import pytest

from edge import checkout


def tree(path: Path, git: str = "directory") -> Path:
    """A tree at `path` whose `.git` is a directory (a main worktree) or a file (a linked one)."""
    path.mkdir(parents=True)
    if git == "directory":
        (path / ".git").mkdir()
    else:
        (path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/issue-9-y\n")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_the_main_worktree_at_home_git_edge_is_the_live_checkout(home):
    """#79 cell 1: `$HOME/git/edge` with a `.git` directory proceeds."""
    assert checkout.is_live_checkout(tree(home / "git" / "edge"), home)


def test_a_linked_worktree_of_edge_is_not_the_live_checkout(home, tmp_path):
    """#79 cell 2: a linked worktree (`.git` file) is refused while `$HOME/git/edge` exists."""
    tree(home / "git" / "edge")
    worktree = tree(tmp_path / "edge-worktrees" / "issue-9-y", git="file")

    assert not checkout.is_live_checkout(worktree, home)


def test_another_copy_is_not_the_live_checkout(home, tmp_path):
    """#79 cell 3: a main worktree elsewhere, e.g. `/tmp/x/edge`, is refused."""
    tree(home / "git" / "edge")

    assert not checkout.is_live_checkout(tree(tmp_path / "x" / "edge"), home)


def test_the_live_checkout_reached_through_a_symlinked_home_is_the_live_checkout(tmp_path):
    """#79 cell 4: a HOME that is a symlink to the home holding the code tree proceeds."""
    code_tree = tree(tmp_path / "real-home" / "git" / "edge")
    home = tmp_path / "home"
    home.symlink_to(tmp_path / "real-home")

    assert checkout.is_live_checkout(code_tree, home)


def test_a_home_git_edge_symlink_to_the_code_tree_is_the_live_checkout(home, tmp_path):
    """#79 cell 4: `$HOME/git/edge` as a symlink that resolves to the code tree proceeds."""
    code_tree = tree(tmp_path / "disk" / "edge")
    (home / "git").mkdir(parents=True)
    (home / "git" / "edge").symlink_to(code_tree)

    assert checkout.is_live_checkout(code_tree, home)


def test_home_git_edge_as_a_linked_worktree_is_not_the_live_checkout(home):
    """#79 cell 5: `$HOME/git/edge` whose `.git` is a file is refused."""
    assert not checkout.is_live_checkout(tree(home / "git" / "edge", git="file"), home)


def test_a_tree_without_git_is_not_the_live_checkout(home):
    """#79 rule 1: the live checkout is a main worktree, so a bare copy of files is refused."""
    code_tree = home / "git" / "edge"
    code_tree.mkdir(parents=True)

    assert not checkout.is_live_checkout(code_tree, home)
