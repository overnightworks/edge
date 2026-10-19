"""bin/edge runs its own checkout's edge with that checkout's locked dependencies, from anywhere."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "bin/edge"

FAKE_UV = """#!/bin/sh
printf '%s\\0' "$@" > "$FAKE_LOG/uv.args"
printf '%s' "${VIRTUAL_ENV-unset} ${UV_PROJECT_ENVIRONMENT-unset}" > "$FAKE_LOG/uv.env"
if [ -n "${FAKE_UV_FAILS-}" ]; then
    echo "uv: lock file is out of date" >&2
    exit 1
fi
"""

FAKE_EDGE = """#!/bin/sh
printf '%s\\0' "$@" > "$FAKE_LOG/edge.args"
printf '%s' "${VIRTUAL_ENV-unset} ${UV_PROJECT_ENVIRONMENT-unset}" > "$FAKE_LOG/edge.env"
printf '%s' "$PPID" > "$FAKE_LOG/edge.ppid"
exit "${FAKE_EDGE_EXIT-0}"
"""


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@dataclass(frozen=True)
class Machine:
    checkout: Path
    elsewhere: Path
    log: Path
    path: str

    def run(
        self, *args: str, launcher: Path | None = None, **env: str
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(launcher or self.checkout / "bin/edge"), *args],
            cwd=self.elsewhere,
            env={"PATH": self.path, "FAKE_LOG": str(self.log), **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def logged(self, name: str) -> str:
        return (self.log / name).read_text()

    def logged_args(self, name: str) -> list[str]:
        return self.logged(name).split("\0")[:-1]


@pytest.fixture
def machine(tmp_path) -> Machine:
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, checkout / "bin/edge")
    _executable(checkout / ".venv/bin/edge", FAKE_EDGE)
    fake_bin = tmp_path / "fake-bin"
    _executable(fake_bin / "uv", FAKE_UV)
    (tmp_path / "log").mkdir()
    (tmp_path / "elsewhere").mkdir()
    return Machine(
        checkout=checkout,
        elsewhere=tmp_path / "elsewhere",
        log=tmp_path / "log",
        path=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    )


def test_syncs_the_checkout_locked_and_hands_edge_its_arguments_verbatim(machine):
    """#102 rule 1, cell L1: the checkout syncs locked; edge gets every argument unchanged."""
    arguments = ["external", "plan", "two words", "", "--flag=$HOME"]

    result = machine.run(*arguments)

    assert result.returncode == 0, result.stderr
    assert machine.logged_args("uv.args") == [
        "sync",
        "--locked",
        "--inexact",
        "--no-dev",
        "--quiet",
        "--project",
        str(machine.checkout),
    ]
    assert machine.logged_args("edge.args") == arguments


def test_exits_with_edge_exit_code(machine):
    """#102 rule 1, cell L2: the launcher exits with edge's own exit code."""
    assert machine.run("external", "apply", FAKE_EDGE_EXIT="3").returncode == 3


def test_failed_sync_exits_2_with_one_line_and_never_runs_edge(machine):
    """#102 rule 2, cell L3: a failed sync exits 2 with one closing line, and edge never runs."""
    result = machine.run("--help", FAKE_UV_FAILS="1")

    assert result.returncode == 2
    assert "uv: lock file is out of date" in result.stderr
    assert result.stderr.splitlines()[-1] == (
        "edge: cannot sync the live checkout's dependencies; "
        f"run `uv sync --locked` in {machine.checkout}"
    )
    assert not (machine.log / "edge.args").exists()


def test_edge_replaces_the_launcher_instead_of_running_under_a_shell(machine):
    """#102 rule 1, cell L4: edge replaces the launcher process instead of running under a shell."""
    machine.run("--help")

    assert machine.logged("edge.ppid") == str(os.getpid())


def test_another_projects_environment_reaches_neither_uv_nor_edge(machine, tmp_path):
    """#102 rule 1, cell L5: another project's virtualenv reaches neither uv nor edge."""
    other = str(tmp_path / "other-project/.venv")

    machine.run("--help", VIRTUAL_ENV=other, UV_PROJECT_ENVIRONMENT=other)

    assert machine.logged("uv.env") == "unset unset"
    assert machine.logged("edge.env") == "unset unset"


def test_finds_its_checkout_through_a_symlink_in_another_directory(machine, tmp_path):
    """#102 rule 1, cell L6: a symlink elsewhere still finds its own checkout."""
    link = tmp_path / "home/.local/bin/edge"
    link.parent.mkdir(parents=True)
    link.symlink_to(machine.checkout / "bin/edge")

    result = machine.run("--help", launcher=link)

    assert result.returncode == 0, result.stderr
    assert machine.logged_args("uv.args")[-1] == str(machine.checkout)
    assert machine.logged_args("edge.args") == ["--help"]


def test_runs_the_command_it_is_called_by_through_a_symlink_named_home(machine, tmp_path):
    """#142 rule 5, cell L8: a symlink named home starts the checkout's home, not its edge."""
    (machine.checkout / ".venv/bin/edge").unlink()
    _executable(machine.checkout / ".venv/bin/home", FAKE_EDGE)
    link = tmp_path / "tools/home"
    link.parent.mkdir()
    link.symlink_to(machine.checkout / "bin/edge")

    result = machine.run("tidy", launcher=link)

    assert result.returncode == 0, result.stderr
    assert machine.logged_args("edge.args") == ["tidy"]
