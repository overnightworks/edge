#!/usr/bin/env python3
"""tidy-home keeps the home directory in the shape ~/README.md describes.

Without --apply it is a dry run: every finding is printed, nothing changes.
With --apply the CLEAN findings are executed. AUDIT findings are never acted
on automatically; they name what a person has to decide.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

PASTED_IMAGE_GLOB = "Eingefügtes Bild*.png"
PASTED_IMAGE_MAX_AGE = timedelta(days=7)
TRASH_MAX_AGE = timedelta(days=30)
DOWNLOADS_MAX_AGE = timedelta(days=14)
CODEX_SESSION_MAX_AGE = timedelta(days=90)
DOCKER_STOPPED_CONTAINER_MAX_AGE = timedelta(days=7)
DOCKER_BUILD_CACHE_KEEP = "20GB"
LARGEST_ENTRIES_SHOWN = 10

WORKTREE_ISSUE_PATTERN = re.compile(r"issue-(\d+)")
TRASHINFO_DATE_PATTERN = re.compile(r"^DeletionDate=(.+)$", re.MULTILINE)
DOCKER_CREATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"

# Everything ~/README.md lists as belonging to the home root.
KNOWN_HOME_ENTRIES = frozenset(
    {
        "README.md",
        "git",
        "Dokumente",
        "Bilder",
        "Downloads",
        "Schreibtisch",
        "backups",
        "snap",
        ".secrets",
        ".ssh",
        ".gnupg",
        ".docker",
        ".songmaker",
        ".cloudflared",
        ".claude",
        ".claude.json",
        ".codex",
        ".grok",
        ".opencode",
        ".local",
        ".config",
        ".cache",
        ".vscode",
        ".vscode-shared",
        ".npm",
        ".nv",
        ".pki",
        ".bashrc",
        ".bash_profile",
        ".bash_logout",
        ".bash_history",
        ".profile",
        ".zshrc",
        ".gitconfig",
        ".nvidia-settings-rc",
        ".sudo_as_admin_successful",
    }
)


class Action(Enum):
    DELETE = "delete"
    RUN = "run"
    REPORT = "report"


@dataclass(frozen=True)
class Finding:
    rule: str
    action: Action
    subject: str
    detail: str = ""
    command: tuple[str, ...] = ()

    @property
    def is_cleanup(self) -> bool:
        return self.action is not Action.REPORT


@dataclass(frozen=True)
class Layout:
    """Every path the rules touch, derived from one home directory."""

    home: Path

    @property
    def git(self) -> Path:
        return self.home / "git"

    @property
    def downloads(self) -> Path:
        return self.home / "Downloads"

    @property
    def codex_sessions(self) -> Path:
        return self.home / ".codex" / "sessions"

    @property
    def pasted_image_dirs(self) -> tuple[Path, ...]:
        return (self.home, self.git, self.home / "Dokumente", self.home / "Schreibtisch")

    @property
    def trash_dirs(self) -> tuple[Path, ...]:
        gnome = self.home / ".local" / "share" / "Trash"
        vscode_snap = sorted((self.home / "snap" / "code").glob("*/.local/share/Trash"))
        return (gnome, *vscode_snap)

    @property
    def report_file(self) -> Path:
        return self.home / ".local" / "state" / "tidy-home" / "last-report.md"


IssueStateLookup = Callable[[Path, int], str | None]
CommandRunner = Callable[[Sequence[str]], str | None]


def run_command(argv: Sequence[str]) -> str | None:
    """Run an external command; None when the tool is missing or fails."""
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def github_issue_state(repo: Path, number: int, run: CommandRunner = run_command) -> str | None:
    remote = run(["git", "-C", str(repo), "remote", "get-url", "origin"])
    if not remote:
        return None
    slug = re.sub(r"^.*github\.com[:/]", "", remote.strip()).removesuffix(".git")
    state = run(["gh", "issue", "view", str(number), "-R", slug, "--json", "state", "-q", ".state"])
    return state.strip() if state else None


def _age(path: Path, now: datetime) -> timedelta:
    return now - datetime.fromtimestamp(path.stat().st_mtime)


def _older_than(path: Path, max_age: timedelta, now: datetime) -> bool:
    return _age(path, now) > max_age


def _days(delta: timedelta) -> str:
    return f"{delta.days} d"


# --- CLEAN rules: safe to apply -------------------------------------------


def pasted_images(layout: Layout, now: datetime) -> list[Finding]:
    """Terminal paste drops 'Eingefügtes Bild (n).png' into the cwd; a week later it is litter."""
    return [
        Finding("pasted-images", Action.DELETE, str(image), _days(_age(image, now)))
        for directory in layout.pasted_image_dirs
        if directory.is_dir()
        for image in sorted(directory.glob(PASTED_IMAGE_GLOB))
        if _older_than(image, PASTED_IMAGE_MAX_AGE, now)
    ]


def _trash_deletion_date(info_file: Path) -> datetime | None:
    match = TRASHINFO_DATE_PATTERN.search(info_file.read_text(errors="replace"))
    return datetime.fromisoformat(match.group(1).strip()) if match else None


def expired_trash(layout: Layout, now: datetime) -> list[Finding]:
    """freedesktop trash keeps a .trashinfo per item; items past TRASH_MAX_AGE go."""
    findings: list[Finding] = []
    for trash in layout.trash_dirs:
        for info_file in sorted((trash / "info").glob("*.trashinfo")):
            deleted_at = _trash_deletion_date(info_file)
            if deleted_at is None or now - deleted_at <= TRASH_MAX_AGE:
                continue
            item = trash / "files" / info_file.name.removesuffix(".trashinfo")
            for path in (item, info_file):
                if path.exists():
                    findings.append(Finding("trash", Action.DELETE, str(path), _days(now - deleted_at)))
    return findings


def _docker_stopped_containers(run: CommandRunner, now: datetime) -> list[str]:
    listing = run(["docker", "container", "ls", "-a", "--format", "{{.Names}}\t{{.CreatedAt}}\t{{.State}}"])
    stale: list[str] = []
    for line in (listing or "").splitlines():
        name, created_at, state = line.split("\t")
        if state not in {"exited", "created"}:
            continue
        created = datetime.strptime(created_at[: len("2026-09-05 15:47:49")], DOCKER_CREATED_AT_FORMAT)
        if now - created > DOCKER_STOPPED_CONTAINER_MAX_AGE:
            stale.append(name)
    return stale


def _docker_usage(run: CommandRunner) -> dict[str, str]:
    """Map docker's usage types ('Images', 'Build Cache', ...) to 'size, reclaimable'."""
    output = run(["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"]) or ""
    usage: dict[str, str] = {}
    for line in output.splitlines():
        kind, size, reclaimable = line.split("\t")
        usage[kind] = f"{size}, reclaimable {reclaimable}"
    return usage


def docker_leftovers(run: CommandRunner, now: datetime) -> list[Finding]:
    """Stopped probe containers, dangling images and the build cache grow without bound."""
    if run(["docker", "info", "--format", "{{.ServerVersion}}"]) is None:
        return []
    until = f"{DOCKER_STOPPED_CONTAINER_MAX_AGE // timedelta(hours=1)}h"
    usage = _docker_usage(run)
    stale_containers = _docker_stopped_containers(run, now)
    return [
        Finding(
            "docker",
            Action.RUN,
            "stopped containers older than " + until,
            ", ".join(stale_containers) or "none",
            ("docker", "container", "prune", "-f", "--filter", f"until={until}"),
        ),
        Finding("docker", Action.RUN, "dangling images", usage.get("Images", ""), ("docker", "image", "prune", "-f")),
        Finding(
            "docker",
            Action.RUN,
            f"build cache beyond {DOCKER_BUILD_CACHE_KEEP}",
            usage.get("Build Cache", ""),
            ("docker", "builder", "prune", "-f", f"--keep-storage={DOCKER_BUILD_CACHE_KEEP}"),
        ),
    ]


def package_caches(run: CommandRunner) -> list[Finding]:
    findings: list[Finding] = []
    if run(["uv", "--version"]):
        findings.append(Finding("caches", Action.RUN, "uv cache", "", ("uv", "cache", "prune")))
    if run(["npm", "--version"]):
        findings.append(Finding("caches", Action.RUN, "npm cache", "", ("npm", "cache", "verify")))
    return findings


# --- AUDIT rules: report only ---------------------------------------------


def unknown_home_entries(layout: Layout) -> list[Finding]:
    """Anything in the home root that ~/README.md does not know is either new or litter."""
    return [
        Finding("unknown-home-entry", Action.REPORT, str(entry), "not listed in ~/README.md")
        for entry in sorted(layout.home.iterdir())
        if entry.name not in KNOWN_HOME_ENTRIES
    ]


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_root_litter(layout: Layout) -> list[Finding]:
    """git/ holds repos and <repo>-worktrees, nothing else."""
    findings: list[Finding] = []
    for entry in sorted(layout.git.iterdir()) if layout.git.is_dir() else []:
        if _is_git_repo(entry):
            continue
        owner = layout.git / entry.name.removesuffix("-worktrees")
        if entry.name.endswith("-worktrees") and _is_git_repo(owner):
            continue
        findings.append(Finding("git-root-litter", Action.REPORT, str(entry), "neither a repo nor a <repo>-worktrees dir"))
    return findings


def _registered_worktrees(repo: Path, run: CommandRunner) -> set[Path]:
    listing = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]) or ""
    return {Path(line.removeprefix("worktree ")) for line in listing.splitlines() if line.startswith("worktree ")}


def stale_worktrees(layout: Layout, issue_state: IssueStateLookup, run: CommandRunner) -> list[Finding]:
    """A worktree whose issue is closed, or that git no longer knows, is a leftover of a landing."""
    findings: list[Finding] = []
    for container in sorted(layout.git.glob("*-worktrees")) if layout.git.is_dir() else []:
        repo = layout.git / container.name.removesuffix("-worktrees")
        registered = _registered_worktrees(repo, run)
        for worktree in sorted(p for p in container.iterdir() if p.is_dir()):
            if worktree.resolve() not in registered:
                findings.append(Finding("stale-worktree", Action.REPORT, str(worktree), "not registered in git worktree list"))
                continue
            match = WORKTREE_ISSUE_PATTERN.search(worktree.name)
            if match and issue_state(repo, int(match.group(1))) == "CLOSED":
                findings.append(Finding("stale-worktree", Action.REPORT, str(worktree), f"issue #{match.group(1)} is closed"))
    return findings


def old_downloads(layout: Layout, now: datetime) -> list[Finding]:
    """Downloads is an inbox: what sits there for two weeks needs a home or the bin."""
    if not layout.downloads.is_dir():
        return []
    return [
        Finding("downloads-inbox", Action.REPORT, str(entry), _days(_age(entry, now)))
        for entry in sorted(layout.downloads.iterdir())
        if _older_than(entry, DOWNLOADS_MAX_AGE, now)
    ]


def old_codex_sessions(layout: Layout, now: datetime) -> list[Finding]:
    """Codex never prunes its session history; report how much is past the age limit."""
    if not layout.codex_sessions.is_dir():
        return []
    old = [p for p in layout.codex_sessions.rglob("*") if p.is_file() and _older_than(p, CODEX_SESSION_MAX_AGE, now)]
    if not old:
        return []
    size = sum(p.stat().st_size for p in old)
    return [
        Finding(
            "codex-sessions",
            Action.REPORT,
            str(layout.codex_sessions),
            f"{len(old)} files older than {CODEX_SESSION_MAX_AGE.days} d, {size / 2**30:.1f} GiB",
        )
    ]


def largest_entries(layout: Layout, run: CommandRunner) -> list[Finding]:
    output = run(["du", "-s", "--block-size=1M", *(str(p) for p in layout.home.iterdir())]) or ""
    sized = sorted(
        ((int(size), path) for size, path in (line.split("\t", 1) for line in output.splitlines())),
        reverse=True,
    )
    return [Finding("largest-entries", Action.REPORT, path, f"{size / 1024:.1f} GiB") for size, path in sized[:LARGEST_ENTRIES_SHOWN]]


# --- orchestration --------------------------------------------------------


@dataclass
class Options:
    apply: bool = False
    sizes: bool = False


def collect_findings(
    layout: Layout,
    now: datetime,
    options: Options,
    run: CommandRunner = run_command,
    issue_state: IssueStateLookup | None = None,
) -> list[Finding]:
    lookup = issue_state or (lambda repo, number: github_issue_state(repo, number, run))
    findings = [
        *pasted_images(layout, now),
        *expired_trash(layout, now),
        *docker_leftovers(run, now),
        *package_caches(run),
        *unknown_home_entries(layout),
        *git_root_litter(layout),
        *stale_worktrees(layout, lookup, run),
        *old_downloads(layout, now),
        *old_codex_sessions(layout, now),
    ]
    if options.sizes:
        findings.extend(largest_entries(layout, run))
    return findings


def apply_finding(finding: Finding, run: CommandRunner = run_command) -> str:
    if finding.action is Action.DELETE:
        path = Path(finding.subject)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return "deleted"
    if finding.action is Action.RUN:
        output = run(finding.command)
        return "failed" if output is None else (output.strip().splitlines() or ["ok"])[-1]
    return ""


def render_report(findings: Iterable[Finding], now: datetime, applied: dict[Finding, str] | None = None) -> str:
    applied = applied or {}
    mode = "apply" if applied else "dry run"
    lines = [f"# tidy-home {now:%Y-%m-%d %H:%M} ({mode})", ""]
    by_rule: dict[str, list[Finding]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule, []).append(finding)
    if not by_rule:
        lines.append("Nothing to do.")
    for rule, group in by_rule.items():
        kind = "clean" if group[0].is_cleanup else "audit"
        lines.append(f"## {rule} ({kind}, {len(group)})")
        for finding in group:
            outcome = f" -> {applied[finding]}" if finding in applied else ""
            detail = f" [{finding.detail}]" if finding.detail else ""
            command = f" `{' '.join(finding.command)}`" if finding.command else ""
            lines.append(f"- {finding.subject}{detail}{command}{outcome}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(prog="tidy-home", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="execute the clean findings (default: dry run)")
    parser.add_argument("--sizes", action="store_true", help="also list the largest home entries (slow)")
    namespace = parser.parse_args(argv)
    return Options(apply=namespace.apply, sizes=namespace.sizes)


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    layout = Layout(Path.home())
    now = datetime.now()
    findings = collect_findings(layout, now, options)
    applied = {f: apply_finding(f) for f in findings if f.is_cleanup} if options.apply else {}
    report = render_report(findings, now, applied)
    layout.report_file.parent.mkdir(parents=True, exist_ok=True)
    layout.report_file.write_text(report)
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
