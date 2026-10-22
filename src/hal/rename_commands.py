"""`hal rename OLD NEW`: moves the clones of a renamed repository on this PC. The repository is
renamed on GitHub first; this moves what the rename leaves behind here: every clone under
`~/git` with its worktrees, its `origin` URL and its Claude project memory, and the `origin` of a
deploy checkout elsewhere. Without `--apply` it only shows. Units, links and uv tools that point
into a moved clone are reported, never rewritten: the repository's own head owns them."""

import argparse
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from edge import compose
from hal.grammar import Asks, Command, ExitCode, write_error

OWNER = "overnightworks"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")
WORKTREES = "-worktrees"
DEPLOY_PARENTS = ("", "srv")  # under HOME: a deploy checkout sits one level below one of these


@dataclass
class World:
    """The boundaries to the outside: HOME, GitHub, the process table and uv."""

    home: Path
    github_knows: Callable[[str], bool]
    running_in: Callable[[Sequence[Path]], list[str]]
    uv_sync: Callable[[Path], None]
    uv_tools: Callable[[], str]


@dataclass(frozen=True)
class Move:
    source: Path
    target: Path


@dataclass(frozen=True)
class Relocation:
    """One clone under `~/git` and everything that travels with it; paths are the new ones."""

    clone: Move
    worktrees: Move | None
    linked_worktrees: tuple[Path, ...]
    new_url: str | None  # None when `origin` already names the new repository
    memory: Move | None
    venvs: tuple[Path, ...]


@dataclass(frozen=True)
class Plan:
    relocations: tuple[Relocation, ...]
    deploy_urls: tuple[tuple[Path, str], ...]
    findings: tuple[str, ...]
    refusals: tuple[str, ...]


def _git(directory: Path, *arguments: str) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, env=environment,
    ).stdout.strip()  # fmt: skip


def processes_inside(roots: Sequence[Path]) -> list[str]:
    """`<pid> <command>` of every process whose working directory is inside one of `roots`."""
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            working_directory = Path(os.readlink(entry / "cwd"))
            command = (entry / "comm").read_text().strip()
        except OSError:  # another user's process, or one that ended meanwhile
            continue
        if any(working_directory.is_relative_to(root) for root in roots):
            found.append(f"{entry.name} {command}")
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
            ["uv", "tool", "list", "--show-paths"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        ).stdout  # fmt: skip
    except FileNotFoundError:  # no uv, no uv tools
        return ""


def real_world(home: Path) -> World:
    return World(home, _github_knows, processes_inside, _uv_sync, _uv_tools)


def _url_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<=[:/]{OWNER}/){re.escape(name)}(?=(\.git)?/?$)")


def _origin(path: Path) -> str | None:
    try:
        return _git(path, "config", "--get", "remote.origin.url")
    except subprocess.CalledProcessError:
        return None


def _names(url: str | None, *names: str) -> bool:
    return url is not None and any(_url_pattern(name).search(url) for name in names)


def _suffix(directory_name: str, old: str) -> str | None:
    """`-land` for `old-land`, `` for `old`, None for a name that is not a sibling of `old`."""
    rest = directory_name.removeprefix(old)
    if rest == directory_name or (rest and not rest.startswith("-")):
        return None
    return None if directory_name.endswith(WORKTREES) else rest


def _clone_paths(world: World, old: str, new: str) -> list[tuple[Path, str]]:
    """Each clone under `~/git` of the repository (by the name it carries) with its suffix."""
    clones = []
    git_directory = world.home / "git"
    for path in sorted(git_directory.iterdir()) if git_directory.is_dir() else []:
        suffix = _suffix(path.name, old)
        if suffix is not None and (path / ".git").is_dir() and _names(_origin(path), old, new):
            clones.append((path, suffix))
    return clones


def _memory_directory(world: World, clone: Path) -> Path:
    return world.home / ".claude" / "projects" / str(clone).replace("/", "-")


def _worktree_paths(worktrees: Path) -> list[Path]:
    return sorted(path for path in worktrees.iterdir() if (path / ".git").exists())


def _relocation(world: World, clone: Path, suffix: str, old: str, new: str) -> Relocation:
    target = clone.with_name(f"{new}{suffix}")
    source_worktrees = clone.with_name(f"{clone.name}{WORKTREES}")
    target_worktrees = clone.with_name(f"{target.name}{WORKTREES}")
    has_worktrees = source_worktrees.is_dir()
    linked = _worktree_paths(source_worktrees) if has_worktrees else []
    memory = Move(_memory_directory(world, clone), _memory_directory(world, target))
    origin = _origin(clone)
    return Relocation(
        clone=Move(clone, target),
        worktrees=Move(source_worktrees, target_worktrees) if has_worktrees else None,
        linked_worktrees=tuple(target_worktrees / path.name for path in linked),
        new_url=_url_pattern(old).sub(new, origin) if _names(origin, old) else None,
        memory=memory if memory.source.is_dir() and not memory.target.exists() else None,
        venvs=tuple(
            target / path.relative_to(clone) if path == clone else target_worktrees / path.name
            for path in [clone, *linked]
            if (path / "uv.lock").is_file() and (path / ".venv").is_dir()
        ),
    )


def _deploy_urls(world: World, old: str, new: str) -> tuple[tuple[Path, str], ...]:
    found = []
    for parent in DEPLOY_PARENTS:
        directory = world.home / parent
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            origin = _origin(path) if _is_deploy_candidate(world, path) else None
            if _names(origin, old):
                found.append((path, _url_pattern(old).sub(new, origin)))
    return tuple(found)


def _is_deploy_candidate(world: World, path: Path) -> bool:
    return (
        not path.name.startswith(".")
        and path != world.home / "git"
        and path.is_dir()
        and (path / ".git").is_dir()
    )


def _mentions(text: str, roots: Iterable[Path]) -> bool:
    return any(re.search(rf"{re.escape(str(root))}(?![\w.-])", text) for root in roots)


def _findings(world: World, roots: Sequence[Path]) -> tuple[str, ...]:
    found = []
    units = world.home / ".config" / "systemd" / "user"
    for unit in sorted(units.glob("*")) if units.is_dir() else []:
        if unit.is_file() and _mentions(unit.read_text(errors="replace"), roots):
            found.append(f"unit {unit} points into a moved clone")
    links = world.home / ".local" / "bin"
    for link in sorted(links.glob("*")) if links.is_dir() else []:
        if link.is_symlink() and _mentions(os.readlink(link), roots):
            found.append(f"link {link} points into a moved clone")
    found.extend(
        f"uv tool installed from a moved clone: {line.strip()}"
        for line in world.uv_tools().splitlines()
        if _mentions(line, roots)
    )
    return tuple(found)


def _refusals(world: World, relocations: Sequence[Relocation], new: str, busy: bool) -> list[str]:
    refusals = [
        f"{target} already exists"
        for relocation in relocations
        for target in (
            relocation.clone.target,
            relocation.worktrees and relocation.worktrees.target,
        )
        if target and target.exists()
    ]
    if busy:
        refusals.append("a process still runs inside a clone to move")
    if relocations and not world.github_knows(new):
        refusals.append(f"GitHub does not know {OWNER}/{new}")
    return refusals


def make_plan(world: World, old: str, new: str) -> Plan:
    relocations = tuple(
        _relocation(world, clone, suffix, old, new)
        for clone, suffix in _clone_paths(world, old, new)
    )
    deploy_urls = _deploy_urls(world, old, new)
    roots = [
        move.source
        for relocation in relocations
        for move in (relocation.clone, relocation.worktrees)
        if move
    ]
    running = world.running_in(roots) if roots else []
    refusals = _refusals(world, relocations, new, bool(running))
    if running:
        refusals.append("processes: " + ", ".join(running))
    findings = _findings(world, roots) if roots else ()
    return Plan(relocations, deploy_urls, findings, tuple(refusals))


def _lines_of(plan: Plan) -> list[str]:
    lines = []
    for relocation in plan.relocations:
        lines += [f"move {move.source} -> {move.target}" for move in (relocation.clone,)]
        if relocation.worktrees:
            lines.append(f"move {relocation.worktrees.source} -> {relocation.worktrees.target}")
        if relocation.new_url:
            lines.append(f"origin of {relocation.clone.target} -> {relocation.new_url}")
        if relocation.linked_worktrees:
            lines.append(
                f"repair {len(relocation.linked_worktrees)} worktrees of {relocation.clone.target}"
            )
        if relocation.memory:
            lines.append(f"move {relocation.memory.source} -> {relocation.memory.target}")
        lines += [f"uv sync in {venv}" for venv in relocation.venvs]
    lines += [f"origin of {path} -> {url}" for path, url in plan.deploy_urls]
    return lines


def _carry_out(relocation: Relocation) -> None:
    relocation.clone.source.rename(relocation.clone.target)
    if relocation.worktrees:
        relocation.worktrees.source.rename(relocation.worktrees.target)
    if relocation.new_url:
        _git(relocation.clone.target, "remote", "set-url", "origin", relocation.new_url)
    if relocation.linked_worktrees:
        _git(
            relocation.clone.target,
            "worktree", "repair", *map(str, relocation.linked_worktrees),
        )  # fmt: skip
    if relocation.memory:
        relocation.memory.source.rename(relocation.memory.target)


def _apply(world: World, plan: Plan) -> None:
    for relocation in plan.relocations:
        _carry_out(relocation)
    for path, url in plan.deploy_urls:
        _git(path, "remote", "set-url", "origin", url)
    for relocation in plan.relocations:
        for venv in relocation.venvs:
            world.uv_sync(venv)


def _say(line: str) -> None:
    print(compose.printable(line))


def rename(world: World, old: str, new: str, apply: bool) -> ExitCode:
    plan = make_plan(world, old, new)
    if plan.refusals:
        for reason in plan.refusals:
            write_error(f"refusing, nothing changed: {reason}")
        return ExitCode.REFUSED
    lines = _lines_of(plan)
    if not lines:
        _say(f"nothing to do: no clone of {old} to move")
        return ExitCode.OK
    for line in lines:
        _say(line)
    if apply:
        try:
            _apply(world, plan)
        except (subprocess.CalledProcessError, OSError) as error:
            write_error(f"stopped halfway ({type(error).__name__}); the moves printed above ran")
            return ExitCode.FAILURE
    else:
        _say("nothing changed; --apply does the above")
    for finding in plan.findings:
        _say(f"finding: {finding}")
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
    return rename(real_world(Path.home()), arguments.old, arguments.new, arguments.apply)


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
