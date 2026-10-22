"""`hal rename OLD NEW`: moves the clones of a renamed repository on this PC. The repository is
renamed on GitHub first; this moves what the rename leaves behind here: every clone under
`~/git` with its worktrees, its `origin` URL and its Claude project memory, and the `origin` of a
deploy checkout elsewhere. Without `--apply` it only shows. Units, links and uv tools that point
into a moved clone are reported, never rewritten: the repository's own head owns them.

The plan reads the state it finds, so a rerun after a halfway stop finishes the clones that
were moved but not completed."""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from edge import compose
from hal.grammar import Asks, Command, ExitCode, write_error
from home_tools.tidy_home import Layout, registered_worktrees

OWNER = "overnightworks"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")
WORKTREES = "-worktrees"
DEPLOY_PARENTS = ("", "srv")  # under HOME: a deploy checkout sits one level below one of these


@dataclass(frozen=True)
class RunningProcess:
    pid: int
    command: str
    directory: Path


@dataclass(frozen=True)
class DeployCheckout:
    path: Path
    new_url: str


@dataclass
class World:
    """The boundaries to the outside: HOME, GitHub, the process table and uv."""

    home: Path
    github_knows: Callable[[str], bool]
    running_in: Callable[[Sequence[Path]], list[RunningProcess]]
    uv_sync: Callable[[Path], None]
    uv_tools: Callable[[], str]


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path


@dataclass(frozen=True)
class Step:
    description: str
    run: Callable[[], None]


@dataclass(frozen=True)
class Site:
    """A clone as found: where it is now (`here`, the source or already the target), its moves."""

    here: Path
    clone: Move
    worktrees: Move
    memory: Move


@dataclass(frozen=True)
class Relocation:
    """What one clone still needs; a worktree's `source` is the path git has recorded for it."""

    site: Site
    new_url: str | None  # None when `origin` already names the new repository
    linked: tuple[Move, ...]
    outside: tuple[Path, ...]
    venvs: tuple[Path, ...]


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    findings: tuple[str, ...]
    refusals: tuple[str, ...]


@dataclass(frozen=True)
class Halfway:
    failed: Step
    reason: str
    done: tuple[Step, ...]
    pending: tuple[Step, ...]


def _output_of(argv: Sequence[str]) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return subprocess.run(
        argv, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, env=environment
    ).stdout  # fmt: skip


def _git(directory: Path, *arguments: str) -> str:
    return _output_of(["git", "-C", str(directory), *arguments]).strip()


def processes_inside(roots: Sequence[Path]) -> list[RunningProcess]:
    """Every process whose working directory is inside one of `roots`."""
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            working_directory = Path(os.readlink(entry / "cwd"))
            command = (entry / "comm").read_text().strip()
        except OSError:  # another user's process, or one that ended meanwhile
            continue
        if any(working_directory.is_relative_to(root) for root in roots):
            found.append(RunningProcess(int(entry.name), command, working_directory))
    return found


def _github_knows(name: str) -> bool:
    return subprocess.run(
        ["gh", "api", f"repos/{OWNER}/{name}"], capture_output=True, stdin=subprocess.DEVNULL
    ).returncode == 0  # fmt: skip


def _uv_sync(project: Path) -> None:
    subprocess.run(
        ["uv", "sync", "--locked", "--reinstall"],
        cwd=project, check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip


def _uv_tools() -> str:
    try:
        return subprocess.run(
            ["uv", "tool", "list", "--show-version-specifiers"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        ).stdout  # fmt: skip
    except FileNotFoundError:  # no uv, no uv tools
        return ""


def real_world(home: Path) -> World:
    return World(home, _github_knows, processes_inside, _uv_sync, _uv_tools)


def _remote_pattern(name: str) -> re.Pattern[str]:
    """A GitHub remote of `OWNER/name`, https or ssh; `before` and `after` keep its spelling."""
    host = r"(?:https://(?:[\w.-]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
    return re.compile(rf"(?P<before>{host}{OWNER}/){re.escape(name)}(?P<after>(?:\.git)?/?)")


def _renamed_url(url: str | None, old: str, new: str) -> str | None:
    match = _remote_pattern(old).fullmatch(url) if url else None
    return f"{match['before']}{new}{match['after']}" if match else None


def _origin(path: Path) -> str | None:
    try:
        return _git(path, "config", "--get", "remote.origin.url")
    except subprocess.CalledProcessError:
        return None


def _names(url: str | None, *names: str) -> bool:
    return url is not None and any(_remote_pattern(name).fullmatch(url) for name in names)


def _suffix(directory_name: str, prefix: str) -> str | None:
    """`-land` for `prefix-land`, `` for `prefix`, None for a name that is not a sibling."""
    rest = directory_name.removeprefix(prefix)
    if rest == directory_name or (rest and not rest.startswith("-")):
        return None
    return None if directory_name.endswith(WORKTREES) else rest


def _suffix_of(directory_name: str, old: str, new: str) -> str | None:
    """The longer of the two names wins, so `demo-app` is `demo-app` and not `demo` + `-app`
    when `demo` becomes `demo-app`, and a repeat never takes a moved clone for a source."""
    longer, shorter = sorted((old, new), key=len, reverse=True)
    found = _suffix(directory_name, longer)
    return found if found is not None else _suffix(directory_name, shorter)


def memory_directory(home: Path, clone: Path) -> Path:
    """Claude Code names a project directory by turning every non-alphanumeric character of the
    path into `-`."""
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(clone))


def _worktrees_of(clone: Path) -> Path:
    return clone.with_name(f"{clone.name}{WORKTREES}")


def _sites(world: World, old: str, new: str) -> list[Site]:
    git_directory = Layout(world.home).git
    names = [path.name for path in git_directory.iterdir()] if git_directory.is_dir() else []
    suffixes = {_suffix_of(name, old, new) for name in names} - {None}
    sites = []
    for suffix in sorted(suffixes):
        clone = Move(git_directory / f"{old}{suffix}", git_directory / f"{new}{suffix}")
        here = next(
            (
                path
                for path in (clone.source, clone.target)
                if (path / ".git").is_dir() and _names(_origin(path), old, new)
            ),
            None,
        )
        if here:
            sites.append(
                Site(
                    here,
                    clone,
                    Move(_worktrees_of(clone.source), _worktrees_of(clone.target)),
                    Move(
                        memory_directory(world.home, clone.source),
                        memory_directory(world.home, clone.target),
                    ),
                )
            )
    return sites


def _relocated(path: Path, moves: Iterable[Move]) -> Path | None:
    """Where `path` lies after the moves, None when it lies outside every moved tree."""
    for move in moves:
        for root in (move.source, move.target):
            if path.is_relative_to(root):
                return move.target / path.relative_to(root)
    return None


def _current(move: Move) -> Path:
    return move.source if os.path.lexists(move.source) else move.target


def _venv_names_a_path_from_before(venv: Path, old_roots: Sequence[Path]) -> bool:
    """Whether an entry-point script of the venv still starts with a path from before the move."""
    for script in sorted((venv / "bin").glob("*")):
        if script.is_file() and not script.is_symlink():
            with script.open("rb") as stream:
                if _mentions(stream.readline(512).decode(errors="replace"), old_roots):
                    return True
    return False


def _venv_is_stale(project: Move, old_roots: Sequence[Path]) -> bool:
    """A venv still to rebuild: its project has not moved yet, or its scripts still name a path
    from before the move."""
    current = _current(project)
    if not ((current / "uv.lock").is_file() and (current / ".venv").is_dir()):
        return False
    return current != project.target or _venv_names_a_path_from_before(current / ".venv", old_roots)


def _relocation(site: Site, moves: Sequence[Move], old: str, new: str) -> Relocation:
    linked, outside = [], []
    for path in sorted(registered_worktrees(site.here, _output_of) - {site.here.resolve()}):
        relocated = _relocated(path, moves)
        if relocated is None and path.exists():
            outside.append(path)
        elif relocated is not None and (path.exists() or relocated.exists()):
            linked.append(Move(path, relocated))
    old_roots = [move.source for move in moves]
    return Relocation(
        site=site,
        new_url=_renamed_url(_origin(site.here), old, new),
        linked=tuple(linked),
        outside=tuple(outside),
        venvs=tuple(
            project.target
            for project in (site.clone, *linked)
            if _venv_is_stale(project, old_roots)
        ),
    )


def _deploy_checkouts(world: World, old: str, new: str) -> tuple[DeployCheckout, ...]:
    found = []
    for parent in DEPLOY_PARENTS:
        directory = world.home / parent
        for path in sorted(directory.iterdir()) if directory.is_dir() else []:
            if _is_deploy_candidate(world, path) and (url := _renamed_url(_origin(path), old, new)):
                found.append(DeployCheckout(path, url))
    return tuple(found)


def _is_deploy_candidate(world: World, path: Path) -> bool:
    return (
        not path.name.startswith(".")
        and path != Layout(world.home).git
        and path.is_dir()
        and (path / ".git").is_dir()
    )


def _mentions(text: str, roots: Iterable[Path], home: Path | None = None) -> bool:
    """Whether `text` names a root, absolute or (with `home`) as the unit specifier `%h/<path>`."""
    spellings = [str(root) for root in roots]
    if home:
        spellings += [f"%h/{root.relative_to(home)}" for root in roots if root.is_relative_to(home)]
    return any(re.search(rf"{re.escape(spelling)}(?![\w.-])", text) for spelling in spellings)


def _points_into(link: Path, roots: Iterable[Path]) -> bool:
    target = Path(os.path.normpath(link.parent / os.readlink(link)))
    return any(target.is_relative_to(root) for root in roots)


def _entries(directory: Path) -> list[Path]:
    return sorted(directory.glob("*")) if directory.is_dir() else []


def _unit_findings(directory: Path, roots: Sequence[Path], home: Path) -> list[str]:
    found = []
    for entry in sorted(directory.rglob("*")):
        if entry.is_symlink() and _points_into(entry, roots):
            found.append(f"unit link {entry} points into a moved clone")
        elif entry.is_file() and _mentions(entry.read_text(errors="replace"), roots, home):
            found.append(f"unit {entry} points into a moved clone")
    return found


def _findings(world: World, roots: Sequence[Path], relocations: Sequence[Relocation]) -> list[str]:
    found = _unit_findings(world.home / ".config" / "systemd" / "user", roots, world.home)
    found += [
        f"link {link} points into a moved clone"
        for link in _entries(world.home / ".local" / "bin")
        if link.is_symlink() and _points_into(link, roots)
    ]
    found += [
        f"uv tool installed from a moved clone: {line.strip()}"
        for line in world.uv_tools().splitlines()
        if _mentions(line, roots)
    ]
    found += [
        f"worktree {path} lies outside the moved trees and still points at the old clone: "
        f"git -C {relocation.site.clone.target} worktree repair {path}"
        for relocation in relocations
        for path in relocation.outside
    ]
    return found


def _both_exist(move: Move) -> bool:
    return os.path.lexists(move.source) and os.path.lexists(move.target)


def _refusals(
    world: World, relocations: Sequence[Relocation], has_work: bool, new: str
) -> list[str]:
    git_directory = Layout(world.home).git
    sites = [relocation.site for relocation in relocations]
    refusals = [
        f"{move.target} already exists"
        for site in sites
        for move in (site.clone, site.worktrees, site.memory)
        if _both_exist(move)
    ]
    links = [git_directory] if git_directory.is_symlink() else []
    for site in sites:
        for move in (site.clone, site.worktrees):
            links += [path for path in (move.source, move.target) if path.is_symlink()]
        if (site.here / ".git").is_symlink():
            links.append(site.here / ".git")
    refusals += [f"{path} is a symbolic link, which is never followed" for path in links]
    if has_work and not world.github_knows(new):
        refusals.append(f"GitHub does not know {OWNER}/{new}")
    return refusals


def _process_refusals(world: World, sites: Sequence[Site]) -> list[str]:
    roots = [
        move.source
        for site in sites
        for move in (site.clone, site.worktrees)
        if os.path.lexists(move.source)
    ]
    running = world.running_in(roots) if roots else []
    return [
        f"pid {process.pid} ({process.command}) runs in {process.directory}: end that session first"
        for process in running
    ]


def _move_step(move: Move) -> Step:
    return Step(f"move {move.source} -> {move.target}", lambda: move.source.rename(move.target))


def _origin_step(path: Path, url: str) -> Step:
    return Step(
        f"origin of {path} -> {url}", lambda: _git(path, "remote", "set-url", "origin", url)
    )


def _repair_step(relocation: Relocation) -> Step:
    clone = relocation.site.clone.target
    worktrees = [str(move.target) for move in relocation.linked]
    return Step(
        f"repair {len(worktrees)} worktrees of {clone}",
        lambda: _git(clone, "worktree", "repair", *worktrees),
    )


def _steps(
    world: World, relocations: Sequence[Relocation], deploy: Sequence[DeployCheckout]
) -> tuple[Step, ...]:
    """The work still to do, in the order that is safe: every move first, because a worktree
    of one clone can lie inside another clone's `-worktrees`."""
    sites = [relocation.site for relocation in relocations]
    steps = [
        _move_step(move)
        for site in sites
        for move in (site.clone, site.worktrees)
        if os.path.lexists(move.source)
    ]
    steps += [
        _origin_step(relocation.site.clone.target, relocation.new_url)
        for relocation in relocations
        if relocation.new_url
    ]
    steps += [
        _repair_step(relocation)
        for relocation in relocations
        if any(move.source != move.target for move in relocation.linked)
    ]
    steps += [_move_step(site.memory) for site in sites if site.memory.source.is_dir()]
    steps += [_origin_step(checkout.path, checkout.new_url) for checkout in deploy]
    steps += [
        Step(f"uv sync in {venv}", lambda venv=venv: world.uv_sync(venv))
        for relocation in relocations
        for venv in relocation.venvs
    ]
    return tuple(steps)


def make_plan(world: World, old: str, new: str) -> Plan:
    sites = _sites(world, old, new)
    moves = [move for site in sites for move in (site.clone, site.worktrees)]
    relocations = [_relocation(site, moves, old, new) for site in sites]
    steps = _steps(world, relocations, _deploy_checkouts(world, old, new))
    refusals = _refusals(world, relocations, bool(steps), new)
    refusals += _process_refusals(world, sites)
    roots = [move.source for move in moves]
    findings = _findings(world, roots, relocations) if steps else []
    return Plan(steps, tuple(findings), tuple(refusals))


def _reason(error: subprocess.CalledProcessError | OSError) -> str:
    stderr = error.stderr if isinstance(error, subprocess.CalledProcessError) else None
    return " / ".join((stderr or "").strip().splitlines()) or str(error)


def _execute(steps: Sequence[Step]) -> Halfway | None:
    for index, step in enumerate(steps):
        try:
            step.run()
        except (subprocess.CalledProcessError, OSError) as error:
            return Halfway(step, _reason(error), tuple(steps[:index]), tuple(steps[index + 1 :]))
    return None


def _say(line: str) -> None:
    print(compose.printable(line))


def _report_halfway(halfway: Halfway, old: str, new: str) -> None:
    write_error(
        f"stopped halfway at `{halfway.failed.description}`: {halfway.reason}; "
        f"fix the cause and run `hal rename {old} {new} --apply` again to finish"
    )
    for label, steps in (("done", halfway.done), ("not done", halfway.pending)):
        for step in steps:
            print(compose.printable(f"{label}: {step.description}"), file=sys.stderr)
    print(f"next: hal rename {new} {old} --apply", file=sys.stderr)


def rename(world: World, old: str, new: str, apply: bool) -> ExitCode:
    plan = make_plan(world, old, new)
    if plan.refusals:
        for reason in plan.refusals:
            write_error(f"refusing, nothing changed: {reason}")
        return ExitCode.REFUSED
    if not plan.steps:
        _say(f"nothing to do: no clone of {old} to move")
        return ExitCode.OK
    for step in plan.steps:
        _say(step.description)
    halfway = _execute(plan.steps) if apply else None
    if not apply:
        _say("nothing changed; --apply does the above")
    for finding in plan.findings:
        _say(f"finding: {finding}")
    if halfway:
        _report_halfway(halfway, old, new)
        return ExitCode.FAILURE
    return ExitCode.FINDINGS if plan.findings else ExitCode.OK


def _repository_name(text: str) -> str:
    if not NAME_PATTERN.fullmatch(text):
        raise argparse.ArgumentTypeError(f"not a repository name: {compose.printable(text)}")
    return text


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("old", type=_repository_name, help="the repository's former name")
    parser.add_argument("new", type=_repository_name, help="its name on GitHub now")
    parser.add_argument(
        "--apply", action="store_true", help="move and change instead of only showing"
    )


def _run(arguments: argparse.Namespace) -> ExitCode:
    return rename(real_world(Path.home().resolve()), arguments.old, arguments.new, arguments.apply)


COMMANDS = (
    Command(
        "rename",
        "move the clones of a repository renamed on GitHub (shows first, --apply moves)",
        _run,
        _configure,
        json=False,
        asks=Asks.NEVER,
    ),
)
