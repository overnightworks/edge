from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import check_root_layout

ALLOWLIST = check_root_layout.REPOSITORY_ALLOWLIST
GATE = Path(check_root_layout.__file__).resolve()


def commit_everything(repository: Path, message: str) -> None:
    """Track what the tree holds, so the gate reads it as a tracked path.

    The identity is passed per command because the fixture pins the git
    configuration to the null device; a machine's user.name never reaches here.
    """
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=root layout gate test",
            "-c",
            "user.email=gate@example.invalid",
            "commit",
            "--quiet",
            "--message",
            message,
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def allowlisted_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository the gate is happy with, installed as the gate's own root.

    Its files are committed, because tracked and untracked paths reach the gate
    through separate git calls and a fixture that only writes would leave the
    tracked half unasserted.

    The git configuration is pinned to the null device for this test and every
    process it starts: the gate asks git which untracked files are ignored, so
    a machine-wide ignore matching a probe file would otherwise turn a red path
    green on that machine only.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("a repository the gate is happy with\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".coverage\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True, capture_output=True)
    commit_everything(repository, "the allowlisted tree")
    monkeypatch.setattr(check_root_layout, "REPOSITORY_ROOT", repository)
    return repository


def test_the_gate_passes_on_an_allowlisted_tree(allowlisted_repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_root_layout.main() == 0
    assert "passed" in capsys.readouterr().out


def test_the_gate_leaves_a_root_file_git_ignores_alone(allowlisted_repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A coverage run drops .coverage at the root; the gate judges the tree, not the workspace."""
    (allowlisted_repository / ".coverage").write_text("a build artefact\n", encoding="utf-8")

    assert check_root_layout.main() == 0
    assert "passed" in capsys.readouterr().out


@pytest.mark.parametrize("working_directory", [".", ".."], ids=["from the repository", "from elsewhere"])
def test_the_gate_names_a_stray_root_file_and_fails(
    allowlisted_repository: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    working_directory: str,
) -> None:
    """The gate judges the repository that carries it, whatever the caller's cwd."""
    (allowlisted_repository / "NOTES.md").write_text("a stray at the root\n", encoding="utf-8")
    monkeypatch.chdir(allowlisted_repository / working_directory)

    assert check_root_layout.main() == 1
    assert "NOTES.md" in capsys.readouterr().err


def test_the_gate_names_a_committed_stray_root_file(allowlisted_repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A stray that already landed is as red as one that never was tracked."""
    (allowlisted_repository / "tidy_home.py").write_text("the module left at the root\n", encoding="utf-8")
    commit_everything(allowlisted_repository, "a stray at the root")

    assert check_root_layout.main() == 1
    assert "tidy_home.py" in capsys.readouterr().err


def test_the_gate_exits_non_zero_as_a_command(allowlisted_repository: Path) -> None:
    """CI runs the gate as a script, so its exit code is the contract."""
    (allowlisted_repository / "stray.txt").write_text("a stray at the root\n", encoding="utf-8")
    (allowlisted_repository / "scripts").mkdir()
    shutil.copy(GATE, allowlisted_repository / "scripts" / GATE.name)

    completed = subprocess.run(
        [sys.executable, str(allowlisted_repository / "scripts" / GATE.name)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "stray.txt" in completed.stderr


def test_an_allowlisted_listing_carries_no_problem() -> None:
    listing = [
        ".gitignore",
        "AGENTS.md",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        ".aco/board.toml",
        ".github/workflows/ci.yml",
        "scripts/check_root_layout.py",
        "src/home_tools/tidy_home.py",
        "systemd/tidy-home.service",
        "tests/scripts/test_check_root_layout.py",
    ]

    assert check_root_layout.root_layout_problems(listing, ALLOWLIST) == ()


@pytest.mark.parametrize(
    ("stray", "home"),
    [
        ("NOTES.md", check_root_layout.HOME_BY_SUFFIX[".md"]),
        ("tidy_home.py", check_root_layout.HOME_BY_SUFFIX[".py"]),
        ("tidy-home.service", check_root_layout.HOME_BY_SUFFIX[".service"]),
        ("tidy-home.timer", check_root_layout.HOME_BY_SUFFIX[".timer"]),
        ("config.toml", check_root_layout.DEFAULT_HOME),
    ],
)
def test_a_stray_root_file_is_named_with_the_sentence_where_it_belongs(stray: str, home: str) -> None:
    problems = check_root_layout.root_layout_problems(["README.md", stray], ALLOWLIST)

    assert problems == (f"{stray}: {home}",)


def test_a_stray_root_directory_is_named_with_the_sentence_where_it_belongs() -> None:
    problems = check_root_layout.root_layout_problems(["README.md", "tooling/helper.py"], ALLOWLIST)

    assert problems == (f"tooling/: {check_root_layout.DIRECTORY_HOME}",)
