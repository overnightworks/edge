"""`hal rename OLD NEW`: moves a renamed repository's clones on this PC. Every test runs under a
temporary HOME with real git repositories; `gh`, the process scan and `uv` are fakes."""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from git_support import committed_main, plain_git

from hal import cli, rename_commands
from hal.grammar import ExitCode
from hal.rename_commands import World

SSH = "git@github.com:overnightworks/{}.git"
HTTPS = "https://github.com/overnightworks/{}.git"


@dataclass
class Fakes:
    """What the fake boundaries answer, and what they were asked."""

    known: set[str] = field(default_factory=lambda: {"fresh"})
    running: list[str] = field(default_factory=list)
    tools: str = ""
    synced: list[Path] = field(default_factory=list)


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


@pytest.fixture
def world(home: Path, fakes: Fakes) -> World:
    return World(
        home=home,
        github_knows=lambda name: name in fakes.known,
        running_in=lambda roots: fakes.running,
        uv_sync=fakes.synced.append,
        uv_tools=lambda: fakes.tools,
    )


def make_clone(path: Path, url: str, worktrees: tuple[str, ...] = ()) -> Path:
    """A clone at `path` whose origin is `url`, with linked worktrees in `<path>-worktrees`."""
    committed_main(path)
    plain_git(path, "remote", "add", "origin", url)
    plain_git(path, "switch", "--detach")
    for name in worktrees:
        plain_git(
            path, "worktree", "add", "-b", name,
            str(path.parent / f"{path.name}-worktrees" / name),
        )  # fmt: skip
    return path


def origin_of(path: Path) -> str:
    return plain_git(path, "config", "--get", "remote.origin.url").stdout.strip()


def memory_of(home: Path, clone: Path) -> Path:
    return home / ".claude" / "projects" / str(clone).replace("/", "-")


def tree_state(home: Path) -> list[str]:
    return sorted(
        str(path.relative_to(home)) for path in home.rglob("*") if ".git/" not in str(path)
    )


def rename(world: World, apply: bool = True, old: str = "old", new: str = "fresh") -> ExitCode:
    return rename_commands.rename(world, old, new, apply)


@pytest.fixture
def repository(home: Path) -> Path:
    """`~/git/old` with two worktrees and a Claude memory directory."""
    clone = make_clone(home / "git" / "old", SSH.format("old"), ("lane", "other"))
    memory = memory_of(home, clone) / "memory"
    memory.mkdir(parents=True)
    (memory / "note.md").write_text("remembered")
    return clone


def test_a_dry_run_prints_the_moves_and_changes_nothing(home, world, repository, capsys):
    before = tree_state(home)

    code = rename(world, apply=False)

    assert code == ExitCode.OK
    assert tree_state(home) == before
    assert origin_of(repository) == SSH.format("old")
    output = capsys.readouterr().out
    assert f"move {repository} -> {home / 'git' / 'fresh'}" in output
    assert "--apply" in output


def test_apply_moves_the_clone_its_worktrees_its_url_and_its_memory(home, world, repository):
    land = make_clone(home / "git" / "old-land", HTTPS.format("old"), ("lane",))
    memory_of(home, land).mkdir(parents=True)

    code = rename(world)

    git = home / "git"
    assert code == ExitCode.OK
    assert {path.name for path in git.iterdir()} == {
        "fresh", "fresh-worktrees", "fresh-land", "fresh-land-worktrees",
    }  # fmt: skip
    assert origin_of(git / "fresh") == SSH.format("fresh")
    assert origin_of(git / "fresh-land") == HTTPS.format("fresh")
    for worktree in (git / "fresh-worktrees" / "lane", git / "fresh-worktrees" / "other"):
        assert plain_git(worktree, "status", "--porcelain").stdout == ""
        assert (
            plain_git(worktree, "rev-parse", "--git-common-dir")
            .stdout.strip()
            .startswith(str(git / "fresh"))
        )
    listed = plain_git(git / "fresh", "worktree", "list", "--porcelain").stdout
    assert str(git / "fresh-worktrees" / "lane") in listed
    assert (memory_of(home, git / "fresh") / "memory" / "note.md").read_text() == "remembered"
    assert not memory_of(home, repository).exists()
    assert memory_of(home, git / "fresh-land").is_dir()


def test_a_second_apply_has_nothing_to_do(home, world, repository, capsys):
    rename(world)
    after_first = tree_state(home)
    capsys.readouterr()

    code = rename(world)

    assert code == ExitCode.OK
    assert tree_state(home) == after_first
    assert "nothing to do" in capsys.readouterr().out


def test_the_way_back_moves_everything_home_again(home, world, repository, fakes):
    fakes.known.add("old")
    rename(world)

    code = rename(world, old="fresh", new="old")

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "old") == SSH.format("old")
    assert (memory_of(home, repository) / "memory" / "note.md").is_file()
    assert plain_git(repository, "worktree", "list").stdout.count("old-worktrees") == 2
    assert plain_git(home / "git" / "old-worktrees" / "lane", "status").returncode == 0


def refusal_cases():
    return ["target-exists", "worktrees-target-exists", "process-inside", "unknown-new-name"]


@pytest.mark.parametrize("case", refusal_cases())
def test_a_refusal_changes_nothing_and_exits_four(home, world, repository, fakes, case, capsys):
    if case == "target-exists":
        (home / "git" / "fresh").mkdir()
    if case == "worktrees-target-exists":
        (home / "git" / "fresh-worktrees").mkdir()
    if case == "process-inside":
        fakes.running = ["4242 claude"]
    if case == "unknown-new-name":
        fakes.known.clear()
    before = tree_state(home)

    code = rename(world)

    assert code == ExitCode.REFUSED
    assert tree_state(home) == before
    assert origin_of(repository) == SSH.format("old")
    assert capsys.readouterr().err.startswith("error: ")


def test_a_refusal_for_a_running_process_names_it(world, repository, fakes, capsys):
    fakes.running = ["4242 claude"]

    rename(world)

    assert "4242 claude" in capsys.readouterr().err


def test_a_deploy_checkout_outside_git_only_gets_the_new_url(home, world, repository):
    deploy = make_clone(home / "old-live", HTTPS.format("old"))
    served = make_clone(home / "srv" / "old", HTTPS.format("old"))
    unrelated = make_clone(home / "other-live", HTTPS.format("another"))

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(deploy) == HTTPS.format("fresh")
    assert origin_of(served) == HTTPS.format("fresh")
    assert origin_of(unrelated) == HTTPS.format("another")


def test_a_unit_or_link_into_a_moved_clone_is_reported_and_left_alone(
    home, world, repository, capsys
):
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    unit = units / "old.service"
    unit.write_text(f"[Service]\nExecStart={repository}/bin/serve\n")
    unrelated = units / "other.service"
    unrelated.write_text(f"ExecStart={home / 'git' / 'old-extra-thing'}/run\n")
    link = home / ".local" / "bin" / "hal"
    link.parent.mkdir(parents=True)
    link.symlink_to(repository / "bin" / "hal")
    (link.parent / "elsewhere").symlink_to(home / "somewhere")
    (link.parent / "plain").write_text("not a link")
    fakes_tools = f"hal v0.1.0 ({repository})\n"
    world.uv_tools = lambda: fakes_tools

    code = rename(world)

    output = capsys.readouterr().out
    assert code == ExitCode.FINDINGS
    assert "old.service" in output
    assert "other.service" not in output
    assert f"link {link}" in output
    assert f"uv tool installed from a moved clone: hal v0.1.0 ({repository})" in output
    assert f"ExecStart={repository}/bin/serve" in unit.read_text()
    assert link.readlink() == repository / "bin" / "hal"
    assert (home / "git" / "fresh").is_dir()


def test_a_moved_venv_is_synced_again(home, world, repository, fakes):
    (repository / "uv.lock").write_text("")
    (repository / ".venv").mkdir()
    (repository / ".gitignore").write_text(".venv/\n")

    rename(world)

    assert fakes.synced == [home / "git" / "fresh"]


def test_a_memory_directory_at_the_target_is_kept(home, world, repository, capsys):
    kept = memory_of(home, home / "git" / "fresh")
    kept.mkdir(parents=True)

    code = rename(world)

    assert code == ExitCode.OK
    assert memory_of(home, repository).is_dir()
    assert list(kept.iterdir()) == []


def test_a_failing_step_ends_with_failure_and_says_it_stopped_halfway(world, repository, capsys):
    (repository / "uv.lock").write_text("")
    (repository / ".venv").mkdir()

    def broken(path):
        raise subprocess.CalledProcessError(1, "uv")

    world.uv_sync = broken

    code = rename(world)

    assert code == ExitCode.FAILURE
    assert "stopped halfway" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../up", "a/b", "", "-x", "Upper"])
def test_a_name_that_is_not_a_repository_name_is_a_usage_error(name, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.build_parser(cli.COMMANDS).parse_args(["rename", name, "fresh"])

    assert stop.value.code == ExitCode.USAGE


def test_the_command_is_registered_without_json_and_without_asking():
    command = next(command for command in cli.COMMANDS if command.name == "rename")

    assert (command.json, command.asks.value) == (False, "never")


def test_the_process_scan_finds_a_process_running_inside_a_clone(tmp_path):
    inside = tmp_path / "clone"
    inside.mkdir()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=inside)
    try:
        found = rename_commands.processes_inside([inside])
        assert any(line.startswith(f"{sleeper.pid} ") for line in found)
        assert rename_commands.processes_inside([tmp_path / "elsewhere"]) == []
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_real_boundaries_call_gh_and_uv(tmp_path, monkeypatch):
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name, body in (
        ("gh", 'case "$2" in repos/overnightworks/known) exit 0;; *) exit 1;; esac'),
        ("uv", 'echo "$@ in $PWD" >> "$LOG"; [ "$1" = tool ] && echo "tool listing"; exit 0'),
    ):
        script = bin_directory / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LOG", str(tmp_path / "log"))
    project = tmp_path / "project"
    project.mkdir()

    world = rename_commands.real_world(tmp_path)

    assert world.github_knows("known") and not world.github_knows("missing")
    world.uv_sync(project)
    assert (tmp_path / "log").read_text().strip() == f"sync --locked --reinstall in {project}"
    assert world.uv_tools() == "tool listing\n"


def test_without_uv_there_are_no_uv_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    assert rename_commands.real_world(tmp_path).uv_tools() == ""


def test_a_clone_without_origin_or_with_another_repository_stays_put(home, world):
    committed_main(home / "git" / "old")
    make_clone(home / "git" / "old-land", HTTPS.format("another"))
    make_clone(home / ".hidden", HTTPS.format("old"))

    code = rename(world)

    assert code == ExitCode.OK
    assert (home / "git" / "old").is_dir() and (home / "git" / "old-land").is_dir()
    assert origin_of(home / ".hidden") == HTTPS.format("old")


def test_a_clone_that_already_names_the_new_repository_is_moved_without_a_url_change(home, world):
    clone = make_clone(home / "git" / "old", SSH.format("fresh"))

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "fresh") == SSH.format("fresh")
    assert not clone.exists()
