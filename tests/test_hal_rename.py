"""`hal rename OLD NEW`: moves a renamed repository's clones on this PC. Every test runs under a
temporary HOME with real git repositories; `gh`, the process scan and `uv` are fakes."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from git_support import committed_main, plain_git

from hal import cli, rename_commands
from hal.grammar import ExitCode
from hal.rename_commands import RunningProcess, World

SSH = "git@github.com:overnightworks/{}.git"
HTTPS = "https://github.com/overnightworks/{}.git"


@dataclass
class Fakes:
    """What the fake boundaries answer, and what they were asked."""

    known: set[str] = field(default_factory=lambda: {"fresh"})
    running: list[RunningProcess] = field(default_factory=list)
    tools: str = ""
    uv_calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)


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
        running_in=lambda roots: [
            session
            for session in fakes.running
            if any(session.directory.is_relative_to(root) for root in roots)
        ],
        uv=lambda project, arguments: fakes.uv_calls.append((project, arguments)),
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
    return rename_commands.memory_directory(home, clone)


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


@pytest.mark.parametrize(
    "case",
    [
        "target-exists",
        "worktrees-target-exists",
        "memory-target-exists",
        "dangling-target",
        "dangling-worktrees-target",
        "dangling-memory-target",
        "process-inside",
        "unknown-new-name",
    ],
)
def test_a_refusal_changes_nothing_and_exits_four(home, world, repository, fakes, case, capsys):
    if case == "target-exists":
        (home / "git" / "fresh").mkdir()
    if case == "worktrees-target-exists":
        (home / "git" / "fresh-worktrees").mkdir()
    if case == "memory-target-exists":
        memory_of(home, home / "git" / "fresh").mkdir(parents=True)
    if case == "dangling-target":
        (home / "git" / "fresh").symlink_to(home / "nowhere")
    if case == "dangling-worktrees-target":
        (home / "git" / "fresh-worktrees").symlink_to(home / "nowhere")
    if case == "dangling-memory-target":
        memory_of(home, home / "git" / "fresh").symlink_to(home / "nowhere")
    if case == "process-inside":
        fakes.running = [RunningProcess(4242, "claude", repository)]
    if case == "unknown-new-name":
        fakes.known.clear()
    before = tree_state(home)

    code = rename(world)

    assert code == ExitCode.REFUSED
    assert tree_state(home) == before
    assert origin_of(repository) == SSH.format("old")
    assert capsys.readouterr().err.startswith("error: ")


def test_a_refusal_for_running_processes_names_each_and_says_to_end_the_session(
    world, repository, fakes, capsys
):
    fakes.running = [
        RunningProcess(4242, "claude", repository),
        RunningProcess(4243, "node", repository / "src"),
    ]

    rename(world)

    lines = capsys.readouterr().err.splitlines()
    assert lines[0].endswith(f"pid 4242 (claude) runs in {repository}: end that session first")
    assert lines[1].endswith(
        f"pid 4243 (node) runs in {repository / 'src'}: end that session first"
    )


def test_a_deploy_checkout_outside_git_only_gets_the_new_url(home, world, repository):
    deploy = make_clone(home / "old-live", HTTPS.format("old"))
    served = make_clone(home / "srv" / "old", HTTPS.format("old"))
    unrelated = make_clone(home / "other-live", HTTPS.format("another"))

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(deploy) == HTTPS.format("fresh")
    assert origin_of(served) == HTTPS.format("fresh")
    assert origin_of(unrelated) == HTTPS.format("another")


REFERENCES = [
    (".config/systemd/user/old.service", "link", "{clone}/systemd/old.service"),
    (".config/systemd/user/old.service", "text", "[Service]\nExecStart=%h/git/old/bin/serve\n"),
    (".config/systemd/user/old.service", "text", "WorkingDirectory={clone}\n"),
    (".config/systemd/user/old.service.d/override.conf", "text", "ExecStart=%h/git/old/x\n"),
    (".config/systemd/user/default.target.wants/old.service", "link", "{clone}/systemd/a"),
    (".local/bin/tool", "link", "{clone}/bin/tool"),
    (".local/bin/tool", "link", "../../git/old/bin/tool"),
    ("old-current", "link", "{clone}"),
    ("srv/old-current", "link", "{clone}/bin"),
]


@pytest.mark.parametrize(("path", "kind", "content"), REFERENCES)
def test_a_unit_or_link_into_a_moved_clone_is_reported_and_left_alone(
    home, world, repository, capsys, path, kind, content
):
    reference = home / path
    reference.parent.mkdir(parents=True, exist_ok=True)
    content = content.format(clone=repository)
    if kind == "link":
        reference.symlink_to(content)
    else:
        reference.write_text(content)

    code = rename(world)

    assert code == ExitCode.FINDINGS
    assert f" {reference} points into a moved clone" in capsys.readouterr().out
    assert (str(reference.readlink()) if kind == "link" else reference.read_text()) == content
    assert (home / "git" / "fresh").is_dir()


def test_what_does_not_point_into_a_moved_clone_is_not_reported(home, world, repository, capsys):
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "near.service").write_text(
        f"ExecStart=%h/git/old-extra/run {home / 'git' / 'older'}\n"
    )
    (units / "other.service").write_text("ExecStart=%h/git/another/run\n")
    links = home / ".local" / "bin"
    links.mkdir(parents=True)
    (links / "elsewhere").symlink_to(home / "somewhere")
    (links / "plain").write_text("not a link")

    code = rename(world)

    assert code == ExitCode.OK
    assert "finding" not in capsys.readouterr().out


def test_a_uv_tool_installed_from_a_moved_clone_is_reported(world, repository, fakes, capsys):
    fakes.tools = (
        f"agent-coordination v4.2.0 [required: file://{repository}]\n"
        "- aco\n"
        "ansible-core v2.21.5 [required: ==2.21.5]\n"
    )

    code = rename(world)

    out = capsys.readouterr().out
    assert code == ExitCode.FINDINGS
    assert (
        f"installed from a moved clone: agent-coordination v4.2.0 [required: file://{repository}]"
        in out
    )
    assert "ansible-core" not in out


@pytest.mark.parametrize("name", ["../up", "a/b", "", "-x", "Upper"])
def test_a_name_that_is_not_a_repository_name_is_a_usage_error(name, capsys):
    parser = cli.build_parser(cli.COMMANDS)

    with pytest.raises(SystemExit) as stop:
        parser.parse_args(["rename", name, "fresh"])

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
        assert (sleeper.pid, inside) in [(process.pid, process.directory) for process in found]
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

    assert world.github_knows("known")
    assert not world.github_knows("missing")
    world.uv(project, ("sync", "--locked"))
    assert (tmp_path / "log").read_text().strip() == f"sync --locked in {project}"
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
    assert (home / "git" / "old").is_dir()
    assert (home / "git" / "old-land").is_dir()
    assert origin_of(home / ".hidden") == HTTPS.format("old")


def test_a_clone_that_already_names_the_new_repository_is_moved_without_a_url_change(home, world):
    clone = make_clone(home / "git" / "old", SSH.format("fresh"))

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "fresh") == SSH.format("fresh")
    assert not clone.exists()


def test_a_worktree_is_repaired_wherever_git_lists_it_under_a_moved_tree(home, world, repository):
    nested = repository / ".claude" / "worktrees" / "agent-1"
    deeper = home / "git" / "old-worktrees" / "feature" / "topic"
    land = make_clone(home / "git" / "old-land", HTTPS.format("old"))
    across = home / "git" / "old-worktrees" / "issue-2-land"
    plain_git(repository, "worktree", "add", "-b", "agent-1", str(nested))
    plain_git(repository, "worktree", "add", "-b", "topic", str(deeper))
    plain_git(land, "worktree", "add", "-b", "across", str(across))

    code = rename(world)

    git = home / "git"
    assert code == ExitCode.OK
    for worktree in (
        git / "fresh" / ".claude" / "worktrees" / "agent-1",
        git / "fresh-worktrees" / "feature" / "topic",
        git / "fresh-worktrees" / "issue-2-land",
    ):
        assert plain_git(worktree, "status", "--porcelain").stdout == ""
    assert (git / "fresh-worktrees" / "issue-2-land" / ".git").read_text().count("fresh-land") == 1


def test_a_clone_whose_only_worktree_lies_outside_the_moved_trees_reports_it(
    home, world, repository, tmp_path, capsys
):
    outside = tmp_path / "scratch" / "lane"
    plain_git(repository, "worktree", "add", "-b", "scratch", str(outside))
    for lane in ("lane", "other"):
        plain_git(
            repository, "worktree", "remove", "--force", str(home / "git" / "old-worktrees" / lane)
        )

    code = rename(world)

    assert code == ExitCode.FINDINGS
    assert f"finding: worktree {outside} lies outside the moved trees" in capsys.readouterr().out
    assert plain_git(home / "git" / "fresh", "worktree", "repair", str(outside)).returncode == 0
    assert plain_git(outside, "status", "--porcelain").stdout == ""


def symlinked(home: Path, case: str) -> None:
    elsewhere = home.parent / "elsewhere"
    if case == "clone":
        (home / "git" / "old").rename(elsewhere)
        (home / "git" / "old").symlink_to(elsewhere)
    if case == "metadata":
        (home / "git" / "old" / ".git").rename(elsewhere)
        (home / "git" / "old" / ".git").symlink_to(elsewhere)
    if case == "worktrees":
        (home / "git" / "old-worktrees").rename(elsewhere)
        (home / "git" / "old-worktrees").symlink_to(elsewhere)
    if case == "parent":
        (home / "git").rename(elsewhere)
        (home / "git").symlink_to(elsewhere)
    if case == "venv":
        (home / "git" / "old" / ".venv").symlink_to(elsewhere)
    if case == "nested-worktree":
        group = home / "git" / "old-worktrees" / "group"
        plain_git(home / "git" / "old", "worktree", "add", "-b", "deep", str(group / "lane"))
        group.rename(elsewhere)
        group.symlink_to(elsewhere)
    if case == "recovered-worktree":
        (home / "git" / "old-worktrees").rename(home / "git" / "fresh-worktrees")
        (home / "git" / "fresh-worktrees" / "lane").rename(elsewhere)
        (home / "git" / "fresh-worktrees" / "lane").symlink_to(elsewhere)
    if case == "outside-ancestor":
        scratch = home.parent / "scratch"
        plain_git(home / "git" / "old", "worktree", "add", "-b", "scratch", str(scratch / "lane"))
        (home.parent / "shortcut").symlink_to(scratch)
        listed = home / "git" / "old" / ".git" / "worktrees" / "lane" / "gitdir"
        listed.write_text(str(home.parent / "shortcut" / "lane" / ".git") + "\n")
    if case == "deploy":
        make_clone(elsewhere, HTTPS.format("old"))
        (home / "old-live").symlink_to(elsewhere)
    if case == "deploy-metadata":
        make_clone(home / "old-live", HTTPS.format("old"))
        (home / "old-live" / ".git").rename(elsewhere)
        (home / "old-live" / ".git").symlink_to(elsewhere)
    if case == "deploy-parent":
        make_clone(elsewhere / "old", HTTPS.format("old"))
        (home / "srv").symlink_to(elsewhere)


@pytest.mark.parametrize(
    "case",
    [
        "clone",
        "metadata",
        "worktrees",
        "parent",
        "venv",
        "nested-worktree",
        "recovered-worktree",
        "outside-ancestor",
        "deploy",
        "deploy-metadata",
        "deploy-parent",
    ],
)
def test_a_symbolic_link_in_the_way_is_refused_and_nothing_is_followed(
    home, world, repository, fakes, case, capsys
):
    symlinked(home, case)
    fakes.running = [RunningProcess(4242, "claude", home.parent / "scratch" / "lane")]
    before = sorted(str(path) for path in home.parent.rglob("*") if ".git/" not in str(path))

    code = rename(world)

    assert code == ExitCode.REFUSED
    error = capsys.readouterr().err
    assert "symbolic link" in error
    assert ("pid 4242" in error) == (case == "outside-ancestor")
    assert before == sorted(
        str(path) for path in home.parent.rglob("*") if ".git/" not in str(path)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/overnightworks/old.git",
        "git@example.org:overnightworks/old.git",
        "https://github.com/someone/old.git",
        "https://github.com.evil.example/overnightworks/old.git",
    ],
)
def test_a_remote_that_is_not_the_github_repository_is_not_a_clone_of_it(home, world, url):
    make_clone(home / "git" / "old", url)

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "old") == url


@pytest.mark.parametrize(
    "scheme",
    [
        "ssh://git@github.com/overnightworks/{}",
        "https://user@github.com/overnightworks/{}/",
        "git@github.com:overnightworks/{}",
    ],
)
def test_the_scheme_of_the_remote_is_kept_and_only_the_repository_changes(home, world, scheme):
    make_clone(home / "git" / "old", scheme.format("old"))

    rename(world)

    assert origin_of(home / "git" / "fresh") == scheme.format("fresh")


def test_an_unknown_new_name_refuses_when_only_a_deploy_checkout_would_change(
    home, world, fakes, capsys
):
    deploy = make_clone(home / "old-live", HTTPS.format("old"))
    fakes.known.clear()

    code = rename(world)

    assert code == ExitCode.REFUSED
    assert origin_of(deploy) == HTTPS.format("old")
    assert "GitHub does not know" in capsys.readouterr().err


def test_a_repeat_never_moves_a_clone_twice_when_the_new_name_extends_the_old(
    home, world, fakes, capsys
):
    fakes.known |= {"old", "old-app"}
    make_clone(home / "git" / "old", HTTPS.format("old"), ("lane",))
    make_clone(home / "git" / "old-land", HTTPS.format("old"))

    first = rename(world, new="old-app")
    after_first = tree_state(home)
    second = rename(world, new="old-app")
    assert tree_state(home) == after_first
    back = rename(world, old="old-app", new="old")

    git = home / "git"
    assert (first, second, back) == (ExitCode.OK, ExitCode.OK, ExitCode.OK)
    assert {path.name for path in git.iterdir()} == {"old", "old-worktrees", "old-land"}
    assert origin_of(git / "old") == HTTPS.format("old")
    assert "nothing to do" in capsys.readouterr().out


def test_claude_names_a_memory_directory_by_turning_every_other_character_into_a_dash():
    memory = rename_commands.memory_directory(
        Path("/home/felix-hummert"), Path("/home/felix-hummert/.atelier/my_repo")
    )

    assert memory == Path(
        "/home/felix-hummert/.claude/projects/-home-felix-hummert--atelier-my-repo"
    )


@pytest.fixture
def failing_origin(home, repository):
    """`git remote set-url` cannot write the clone's config, a failure in a middle step."""
    repository.joinpath(".git").chmod(0o555)
    yield
    for clone in (repository, home / "git" / "fresh"):
        if clone.joinpath(".git").is_dir():
            clone.joinpath(".git").chmod(0o755)


def test_a_failure_in_a_middle_step_names_it_and_what_is_done_and_not_done(
    home, world, repository, failing_origin, capsys
):
    unit = home / ".config" / "systemd" / "user" / "old.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("ExecStart=%h/git/old/bin/serve\n")

    code = rename(world)

    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert code == ExitCode.FAILURE
    assert lines[0].startswith("error: stopped halfway at `origin of ")
    assert f"done: move {repository} -> {home / 'git' / 'fresh'}" in lines
    assert any(line.startswith("not done: repair 2 worktrees") for line in lines)
    assert any(line.startswith("not done: move ") and "projects" in line for line in lines)
    assert lines[-1] == "next: hal rename fresh old --apply"
    assert f"finding: unit {unit} points into a moved clone" in captured.out
    assert origin_of(home / "git" / "fresh") == SSH.format("old")


def test_a_rerun_finishes_a_clone_that_a_halfway_stop_left_moved_but_not_completed(
    home, world, repository
):
    (repository / ".git").chmod(0o555)
    rename(world)
    (home / "git" / "fresh" / ".git").chmod(0o755)

    code = rename(world)

    assert code == ExitCode.OK
    clone = home / "git" / "fresh"
    assert origin_of(clone) == SSH.format("fresh")
    assert (
        plain_git(clone / ".." / "fresh-worktrees" / "lane", "status", "--porcelain").stdout == ""
    )
    assert (memory_of(home, clone) / "memory" / "note.md").read_text() == "remembered"
    assert not memory_of(home, repository).exists()


def test_a_worktree_whose_directory_is_gone_is_left_for_git_to_prune(
    home, world, repository, capsys
):
    shutil.rmtree(home / "git" / "old-worktrees" / "other")

    code = rename(world)

    assert code == ExitCode.OK
    assert "repair 1 worktrees of" in capsys.readouterr().out


def test_a_rerun_refuses_a_session_inside_a_clone_that_is_already_moved(
    home, world, repository, failing_origin, fakes, capsys
):
    rename(world)
    (home / "git" / "fresh" / ".git").chmod(0o755)
    session = RunningProcess(4242, "claude", home / "git" / "fresh" / "src")
    fakes.running = [session]

    code = rename(world)

    assert code == ExitCode.REFUSED
    assert f"pid 4242 (claude) runs in {session.directory}" in capsys.readouterr().err
    assert origin_of(home / "git" / "fresh") == SSH.format("old")


def test_a_clone_whose_memory_directory_is_the_same_for_both_names_keeps_it(
    home, world, fakes, capsys
):
    fakes.known = {"a-b"}
    clone = make_clone(home / "git" / "a.b", SSH.format("a.b"))
    memory = memory_of(home, clone)
    memory.mkdir(parents=True)
    (memory / "note.md").write_text("remembered")

    code = rename(world, old="a.b", new="a-b")

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "a-b") == SSH.format("a-b")
    assert (memory / "note.md").read_text() == "remembered"


@pytest.fixture
def locked(repository):
    (repository / "uv.lock").write_text("")
    (repository / ".gitignore").write_text(".venv/\n")
    (repository / ".venv").mkdir()
    return repository


def failing_uv(project: Path, arguments: tuple[str, ...]) -> None:
    raise subprocess.CalledProcessError(1, "uv")


def rewrites(project: Path) -> list[tuple[Path, tuple[str, ...]]]:
    """What a run asks uv for in a project whose `.venv` is a directory."""
    return [
        (project, ("venv", "--allow-existing")),
        (project, ("sync", "--locked", "--reinstall")),
    ]


def test_a_failed_sync_exits_with_failure_and_names_the_project(home, world, locked, capsys):
    world.uv = failing_uv

    code = rename(world)

    assert code == ExitCode.FAILURE
    assert f"stopped halfway at `uv sync in {home / 'git' / 'fresh'}`" in capsys.readouterr().err


def test_a_rerun_after_a_failed_sync_runs_the_sync_again_and_succeeds(home, world, locked, fakes):
    world.uv = failing_uv
    rename(world)
    world.uv = lambda project, arguments: fakes.uv_calls.append((project, arguments))

    code = rename(world)

    assert code == ExitCode.OK
    assert fakes.uv_calls == rewrites(home / "git" / "fresh")


def test_a_dry_run_lists_the_syncs_and_runs_none(home, world, locked, fakes, capsys):
    code = rename(world, apply=False)

    assert code == ExitCode.OK
    assert f"uv sync in {home / 'git' / 'fresh'}" in capsys.readouterr().out
    assert fakes.uv_calls == []


def test_a_repeat_run_syncs_again_and_says_so_instead_of_nothing_to_do(
    home, world, locked, fakes, capsys
):
    rename(world)
    capsys.readouterr()

    code = rename(world)

    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert fakes.uv_calls == rewrites(home / "git" / "fresh") * 2
    assert out.splitlines()[-1] == "nothing to move; ran 1 uv syncs"
    assert "nothing to do" not in out


def test_a_session_in_a_worktree_under_another_clones_directory_refuses_the_run(
    home, world, repository, fakes, capsys
):
    land = make_clone(home / "git" / "old-land", HTTPS.format("old"))
    stray = home / "git" / "old-worktrees" / "stray"
    plain_git(land, "worktree", "add", "-b", "stray", str(stray))
    session = RunningProcess(4242, "claude", stray / "src")
    fakes.running = [session]

    code = rename(world)

    assert code == ExitCode.REFUSED
    assert f"pid 4242 (claude) runs in {session.directory}" in capsys.readouterr().err
    assert (home / "git" / "old").is_dir()


def test_a_moved_clone_gets_a_venv_rewrite_and_a_reinstall_and_nothing_is_deleted(
    home, world, locked, fakes
):
    (locked / ".venv" / "bin").mkdir()
    (locked / ".venv" / "bin" / "marker").write_text("kept")

    rename(world)

    fresh = home / "git" / "fresh"
    assert fakes.uv_calls == rewrites(fresh)
    assert (fresh / ".venv" / "bin" / "marker").read_text() == "kept"


@pytest.mark.parametrize(
    ("config", "python"),
    [("version_info = 3.12.13\n", ("--python", "3.12")), ("home = /uv/python/bin\n", ())],
)
def test_uv_is_told_the_python_version_the_venv_was_made_with(
    home, world, locked, fakes, config, python
):
    (locked / ".venv" / "pyvenv.cfg").write_text(config)

    rename(world)

    assert fakes.uv_calls[0] == (home / "git" / "fresh", ("venv", "--allow-existing", *python))


def test_a_failed_uv_step_exits_with_failure_and_the_rerun_succeeds(home, world, locked, fakes):
    world.uv = failing_uv
    assert rename(world) == ExitCode.FAILURE
    world.uv = lambda project, arguments: fakes.uv_calls.append((project, arguments))

    code = rename(world)

    assert code == ExitCode.OK
    assert fakes.uv_calls == rewrites(home / "git" / "fresh")


def test_a_link_into_the_new_path_of_a_moved_clone_is_not_a_deploy_checkout(home, world, capsys):
    make_clone(home / "git" / "fresh", SSH.format("old"))
    (home / "current").symlink_to(home / "git" / "fresh")

    code = rename(world)

    assert code == ExitCode.OK
    assert origin_of(home / "git" / "fresh") == SSH.format("fresh")
    assert "current" not in capsys.readouterr().err
