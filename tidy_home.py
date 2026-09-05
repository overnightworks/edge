#!/usr/bin/env python3
"""tidy-home keeps the home directory in the shape ~/README.md describes.

Without --apply it is a dry run: every finding is printed, nothing changes.
With --apply the CLEAN findings are executed. AUDIT findings are never acted
on automatically; they name what a person has to decide. Package-cache
housekeeping (uv, npm) also runs under --apply, but it is maintenance, not a
finding: it is never counted, never blocks, never affects the exit code.

Exit code: 0 when the home is clean, 1 when any finding exists (dry run or
apply — not a failure, it is the signal the timer notifies on), 2 when the
configuration file itself is invalid.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

PASTED_IMAGE_GLOB = "Eingefügtes Bild*.png"
LARGEST_ENTRIES_SHOWN = 10
LARGEST_ENTRY_MIN_GIB = 5

WORKTREE_ISSUE_PATTERN = re.compile(r"issue-(\d+)")
TRASHINFO_DATE_PATTERN = re.compile(r"^DeletionDate=(.+)$", re.MULTILINE)
DOCKER_CREATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
DOCKER_CREATED_AT_PREFIX_LENGTH = len("2026-09-05 15:47:49")  # docker appends "+0200 CEST"
DOCKER_RULE = "docker"

BACKTICK_TOKEN_PATTERN = re.compile(r"`([^`]+)`")
REMOVED_SECTION_HEADING = "## Was hier nicht mehr liegt"

SIZE_PATTERN = re.compile(r"([\d.]+)\s*(TB|GB|MB|kB|B)")
BYTES_PER_UNIT = {"TB": 1_000_000_000_000, "GB": 1_000_000_000, "MB": 1_000_000, "kB": 1_000, "B": 1}
GIGABYTE = BYTES_PER_UNIT["GB"]

HOUSEKEEPING_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "cache", "prune"),
    ("npm", "cache", "verify"),
)
HOUSEKEEPING_DRY_RUN_NOTE = "Wartung mit --apply: " + ", ".join(" ".join(command) for command in HOUSEKEEPING_COMMANDS)


class Action(Enum):
    DELETE = "delete"
    RUN = "run"
    REPORT = "report"


class Verdict(Enum):
    """What the operator does with a finding: fix it, hand-fix it, or judge it."""

    DELETE = "löschen"
    REMOVE = "entfernen"
    REVIEW = "prüfen"


@dataclass(frozen=True)
class Finding:
    rule: str
    action: Action
    subject: str
    verdict: Verdict
    recommendation: str
    detail: str = ""
    command: tuple[str, ...] = ()
    reclaimable_bytes: int = 0

    @property
    def is_cleanup(self) -> bool:
        return self.action is not Action.REPORT


def clean_finding(
    rule: str,
    action: Action,
    subject: str,
    *,
    detail: str = "",
    command: tuple[str, ...] = (),
    reclaimable_bytes: int = 0,
) -> Finding:
    """A finding --apply is allowed to execute; its verdict is always 'löschen'."""
    return Finding(rule, action, subject, Verdict.DELETE, "--apply", detail, command, reclaimable_bytes)


@dataclass(frozen=True)
class Limits:
    """Operator-tunable thresholds, loaded from ~/.config/tidy-home/config.toml."""

    pasted_image_days: int = 7
    trash_days: int = 30
    downloads_days: int = 14
    codex_session_days: int = 90
    docker_stopped_container_days: int = 7
    docker_build_cache_keep: str = "20GB"
    downloads_ignore: tuple[str, ...] = ()

    @property
    def pasted_image_max_age(self) -> timedelta:
        return timedelta(days=self.pasted_image_days)

    @property
    def trash_max_age(self) -> timedelta:
        return timedelta(days=self.trash_days)

    @property
    def downloads_max_age(self) -> timedelta:
        return timedelta(days=self.downloads_days)

    @property
    def codex_session_max_age(self) -> timedelta:
        return timedelta(days=self.codex_session_days)

    @property
    def docker_stopped_container_max_age(self) -> timedelta:
        return timedelta(days=self.docker_stopped_container_days)


DAY_LIMIT_KEYS = (
    "pasted_image_days",
    "trash_days",
    "downloads_days",
    "codex_session_days",
    "docker_stopped_container_days",
)


def _require_non_negative_int(key: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer, got {value!r}")


def _require_str(key: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {value!r}")


def _require_parseable_size(key: str, value: object) -> None:
    """Zero is a legitimate limit; only a size no unit matches is a config error."""
    _require_str(key, value)
    if not SIZE_PATTERN.search(value):
        raise ValueError(f"{key} must be a size like '20GB', got {value!r}")


def _require_str_list(key: str, value: object) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings, got {value!r}")


def _validate_limit_values(raw: dict[str, object]) -> None:
    for key in DAY_LIMIT_KEYS:
        if key in raw:
            _require_non_negative_int(key, raw[key])
    if "docker_build_cache_keep" in raw:
        _require_parseable_size("docker_build_cache_keep", raw["docker_build_cache_keep"])
    if "downloads_ignore" in raw:
        _require_str_list("downloads_ignore", raw["downloads_ignore"])


def load_limits(config_file: Path) -> Limits:
    """Defaults apply when the file is absent; an unknown key or a bad value is a config error."""
    if not config_file.is_file():
        return Limits()
    with config_file.open("rb") as handle:
        raw = tomllib.load(handle)
    known_keys = {field.name for field in fields(Limits)}
    unknown_keys = sorted(set(raw) - known_keys)
    if unknown_keys:
        raise ValueError(f"unknown tidy-home config key(s): {', '.join(unknown_keys)}")
    _validate_limit_values(raw)
    if "downloads_ignore" in raw:
        raw = {**raw, "downloads_ignore": tuple(raw["downloads_ignore"])}
    return Limits(**raw)


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
    def readme(self) -> Path:
        return self.home / "README.md"

    @property
    def config_file(self) -> Path:
        return self.home / ".config" / "tidy-home" / "config.toml"

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
    return now - datetime.fromtimestamp(path.lstat().st_mtime)


def _older_than(path: Path, max_age: timedelta, now: datetime) -> bool:
    return _age(path, now) > max_age


def _days(delta: timedelta) -> str:
    return f"{delta.days} d"


# --- CLEAN rules: safe to apply -------------------------------------------


def pasted_images(layout: Layout, now: datetime, limits: Limits) -> list[Finding]:
    """Terminal paste drops 'Eingefügtes Bild (n).png' into the cwd; a week later it is litter."""
    return [
        clean_finding("pasted-images", Action.DELETE, str(image), detail=_days(_age(image, now)))
        for directory in layout.pasted_image_dirs
        if directory.is_dir()
        for image in sorted(directory.glob(PASTED_IMAGE_GLOB))
        if _older_than(image, limits.pasted_image_max_age, now)
    ]


def _trash_deletion_date(info_file: Path) -> datetime | None:
    match = TRASHINFO_DATE_PATTERN.search(info_file.read_text(errors="replace"))
    return datetime.fromisoformat(match.group(1).strip()) if match else None


def expired_trash(layout: Layout, now: datetime, limits: Limits) -> list[Finding]:
    """freedesktop trash keeps a .trashinfo per item; items past the trash limit go."""
    findings: list[Finding] = []
    for trash in layout.trash_dirs:
        files_dir = trash / "files"
        for info_file in sorted((trash / "info").glob("*.trashinfo")):
            deleted_at = _trash_deletion_date(info_file)
            if deleted_at is None or now - deleted_at <= limits.trash_max_age:
                continue
            name = info_file.name.removesuffix(".trashinfo")
            if name.strip() in {"", ".", ".."} or "/" in name:
                continue
            item = files_dir / name
            if item.parent != files_dir:
                continue
            for path in (item, info_file):
                if path.exists():
                    findings.append(clean_finding("trash", Action.DELETE, str(path), detail=_days(now - deleted_at)))
    return findings


def _docker_available(run: CommandRunner) -> bool:
    return run(["docker", "info", "--format", "{{.ServerVersion}}"]) is not None


def _docker_stopped_containers(run: CommandRunner, now: datetime, max_age: timedelta) -> list[str]:
    listing = run(["docker", "container", "ls", "-a", "--format", "{{.Names}}\t{{.CreatedAt}}\t{{.State}}"])
    stale: list[str] = []
    for line in (listing or "").splitlines():
        name, created_at, state = line.split("\t")
        if state not in {"exited", "created"}:
            continue
        created = datetime.strptime(created_at[:DOCKER_CREATED_AT_PREFIX_LENGTH], DOCKER_CREATED_AT_FORMAT)
        if now - created > max_age:
            stale.append(name)
    return stale


@dataclass(frozen=True)
class DockerUsage:
    size: str
    reclaimable: str


def _docker_usage(run: CommandRunner) -> dict[str, DockerUsage]:
    """Map docker's usage types ('Images', 'Build Cache', ...) to their size and reclaimable text."""
    output = run(["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"]) or ""
    usage: dict[str, DockerUsage] = {}
    for line in output.splitlines():
        kind, size, reclaimable = line.split("\t")
        usage[kind] = DockerUsage(size, reclaimable)
    return usage


def _dangling_image_sizes(run: CommandRunner) -> list[int]:
    """One size per dangling image; shared layers appear in each of them, so the sum over-states them."""
    listing = run(["docker", "image", "ls", "-f", "dangling=true", "--format", "{{.Size}}"]) or ""
    return [_bytes_from_size(line) for line in listing.splitlines() if line.strip()]


def _bytes_from_size(text: str) -> int:
    match = SIZE_PATTERN.search(text)
    return int(float(match.group(1)) * BYTES_PER_UNIT[match.group(2)]) if match else 0


def _format_gb(total_bytes: int) -> str:
    return f"{total_bytes / GIGABYTE:.1f}".replace(".", ",") + " GB"


def _docker_findings(run: CommandRunner, now: datetime, limits: Limits) -> list[Finding]:
    """Assumes docker is available; emits only what --apply would actually change."""
    max_age = limits.docker_stopped_container_max_age
    until = f"{max_age // timedelta(hours=1)}h"
    usage = _docker_usage(run)
    stale_containers = _docker_stopped_containers(run, now, max_age)
    findings: list[Finding] = []

    if stale_containers:
        findings.append(
            clean_finding(
                DOCKER_RULE,
                Action.RUN,
                "stopped containers older than " + until,
                detail=", ".join(stale_containers),
                command=("docker", "container", "prune", "-f", "--filter", f"until={until}"),
            )
        )

    dangling_images = _dangling_image_sizes(run)
    if dangling_images:
        images = usage.get("Images")
        # The per-image sizes double-count shared layers, so docker's own accounting is the ceiling.
        dangling_bytes = min(sum(dangling_images), _bytes_from_size(images.reclaimable) if images else 0)
        findings.append(
            clean_finding(
                DOCKER_RULE,
                Action.RUN,
                "dangling images",
                detail=f"{len(dangling_images)} dangling, bis zu {_format_gb(dangling_bytes)}",
                command=("docker", "image", "prune", "-f"),
                reclaimable_bytes=dangling_bytes,
            )
        )

    cache = usage.get("Build Cache")
    if cache:
        cache_size = _bytes_from_size(cache.size)
        cache_reclaimable = _bytes_from_size(cache.reclaimable)
        keep = _bytes_from_size(limits.docker_build_cache_keep)
        if cache_size > keep and cache_reclaimable > 0:
            findings.append(
                clean_finding(
                    DOCKER_RULE,
                    Action.RUN,
                    f"build cache beyond {limits.docker_build_cache_keep}",
                    detail=f"{cache.size}, reclaimable {cache.reclaimable}",
                    command=("docker", "builder", "prune", "-f", f"--keep-storage={limits.docker_build_cache_keep}"),
                    reclaimable_bytes=min(cache_reclaimable, max(0, cache_size - keep)),
                )
            )

    return findings


def docker_leftovers(run: CommandRunner, now: datetime, limits: Limits) -> list[Finding]:
    """Stopped probe containers, dangling images and the build cache grow without bound."""
    if not _docker_available(run):
        return []
    return _docker_findings(run, now, limits)


def run_housekeeping(run: CommandRunner) -> str:
    """uv/npm cache maintenance: runs under --apply, never a Fund, never affects the exit code."""
    outcomes: list[str] = []
    for command in HOUSEKEEPING_COMMANDS:
        output = run(command)
        if output is None:
            continue
        lines = output.strip().splitlines()
        outcomes.append(lines[-1] if lines else "ok")
    return "; ".join(outcomes)


# --- AUDIT rules: report only ---------------------------------------------


def _looks_like_home_path(token: str) -> bool:
    return token.startswith("~/") or token.startswith(".") or "/" in token


def _known_home_entry_names(readme_text: str) -> set[str]:
    text = readme_text.split(REMOVED_SECTION_HEADING, 1)[0]
    names = {"README.md"}
    for token in BACKTICK_TOKEN_PATTERN.findall(text):
        if not _looks_like_home_path(token):
            continue
        names.add(token.removeprefix("~/").split("/", 1)[0])
    return names


def unknown_home_entries(layout: Layout) -> list[Finding]:
    """Anything in the home root that ~/README.md does not know is either new or litter."""
    if not layout.readme.is_file():
        return [Finding("unknown-home-entry", Action.REPORT, "README.md fehlt", verdict=Verdict.REVIEW, recommendation="anlegen")]
    known = _known_home_entry_names(layout.readme.read_text(errors="replace"))
    return [
        Finding(
            "unknown-home-entry",
            Action.REPORT,
            str(entry),
            verdict=Verdict.REVIEW,
            recommendation="Symlink-Ziel prüfen" if entry.is_symlink() else "in ~/README.md eintragen oder löschen",
        )
        for entry in sorted(layout.home.iterdir())
        if entry.name not in known
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
        findings.append(
            Finding(
                "git-root-litter",
                Action.REPORT,
                str(entry),
                verdict=Verdict.REVIEW,
                recommendation="in ein Repo verschieben oder löschen",
                detail="neither a repo nor a <repo>-worktrees dir",
            )
        )
    return findings


def _registered_worktrees(repo: Path, run: CommandRunner) -> set[Path]:
    listing = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]) or ""
    return {Path(line.removeprefix("worktree ")).resolve() for line in listing.splitlines() if line.startswith("worktree ")}


def stale_worktrees(layout: Layout, issue_state: IssueStateLookup, run: CommandRunner) -> list[Finding]:
    """A worktree whose issue is closed, or that git no longer knows, is a leftover of a landing."""
    findings: list[Finding] = []
    recommendation = "Claim freigeben, dann `git worktree remove`"
    for container in sorted(layout.git.glob("*-worktrees")) if layout.git.is_dir() else []:
        repo = layout.git / container.name.removesuffix("-worktrees")
        registered = _registered_worktrees(repo, run)
        for worktree in sorted(p for p in container.iterdir() if p.is_dir()):
            if worktree.resolve() not in registered:
                findings.append(
                    Finding(
                        "stale-worktree",
                        Action.REPORT,
                        str(worktree),
                        verdict=Verdict.REMOVE,
                        recommendation=recommendation,
                        detail="not registered in git worktree list",
                    )
                )
                continue
            match = WORKTREE_ISSUE_PATTERN.search(worktree.name)
            if match and issue_state(repo, int(match.group(1))) == "CLOSED":
                findings.append(
                    Finding(
                        "stale-worktree",
                        Action.REPORT,
                        str(worktree),
                        verdict=Verdict.REMOVE,
                        recommendation=recommendation,
                        detail=f"issue #{match.group(1)} is closed",
                    )
                )
    return findings


def old_downloads(layout: Layout, now: datetime, limits: Limits) -> list[Finding]:
    """Downloads is an inbox: what sits there past the age limit needs a home or the bin."""
    if not layout.downloads.is_dir():
        return []
    return [
        Finding(
            "downloads-inbox",
            Action.REPORT,
            str(entry),
            verdict=Verdict.REVIEW,
            recommendation="nach Dokumente/ einsortieren oder löschen",
            detail=_days(_age(entry, now)),
        )
        for entry in sorted(layout.downloads.iterdir())
        if entry.name not in limits.downloads_ignore and _older_than(entry, limits.downloads_max_age, now)
    ]


def old_codex_sessions(layout: Layout, now: datetime, limits: Limits) -> list[Finding]:
    """Codex never prunes its session history; report how much is past the age limit."""
    if not layout.codex_sessions.is_dir():
        return []
    old = [p for p in layout.codex_sessions.rglob("*") if p.is_file() and _older_than(p, limits.codex_session_max_age, now)]
    if not old:
        return []
    size = sum(p.stat().st_size for p in old)
    days = limits.codex_session_days
    return [
        Finding(
            "codex-sessions",
            Action.REPORT,
            str(layout.codex_sessions),
            verdict=Verdict.REVIEW,
            recommendation=f"Sessions älter als {days} d löschen: `find ~/.codex/sessions -type f -mtime +{days} -delete`",
            detail=f"{len(old)} files older than {days} d, {size / 2**30:.1f} GiB",
        )
    ]


def _parse_largest_entries(du_output: str) -> list[Finding]:
    sized = sorted(
        ((int(size), path) for size, path in (line.split("\t", 1) for line in du_output.splitlines())),
        reverse=True,
    )
    return [
        Finding(
            "largest-entries",
            Action.REPORT,
            path,
            verdict=Verdict.REVIEW,
            recommendation="mit `du -h --max-depth=1 <Pfad>` aufschlüsseln",
            detail=f"{size / 1024:.1f} GiB",
        )
        for size, path in sized[:LARGEST_ENTRIES_SHOWN]
        if size / 1024 >= LARGEST_ENTRY_MIN_GIB
    ]


# --- orchestration --------------------------------------------------------


@dataclass(frozen=True)
class Options:
    apply: bool = False
    sizes: bool = False


class RuleKind(Enum):
    CLEAN = "clean"
    AUDIT = "audit"


class RuleStatus(Enum):
    CLEAN = "clean"
    FOUND = "found"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class RuleOutcome:
    findings: tuple[Finding, ...]
    status: RuleStatus
    reason: str = ""


def _ran(findings: list[Finding]) -> RuleOutcome:
    return RuleOutcome(tuple(findings), RuleStatus.FOUND if findings else RuleStatus.CLEAN)


def _not_applicable(reason: str) -> RuleOutcome:
    return RuleOutcome((), RuleStatus.NOT_APPLICABLE, reason)


@dataclass(frozen=True)
class RuleContext:
    layout: Layout
    now: datetime
    limits: Limits
    run: CommandRunner
    issue_state: IssueStateLookup


@dataclass(frozen=True)
class Rule:
    name: str
    kind: RuleKind
    check: Callable[[RuleContext], RuleOutcome]


def _rule_docker(ctx: RuleContext) -> RuleOutcome:
    if not _docker_available(ctx.run):
        return _not_applicable("nicht installiert")
    return _ran(_docker_findings(ctx.run, ctx.now, ctx.limits))


def _rule_largest_entries(ctx: RuleContext) -> RuleOutcome:
    output = ctx.run(["du", "-s", "--block-size=1M", *(str(p) for p in ctx.layout.home.iterdir())])
    if output is None:
        return _not_applicable("du fehlgeschlagen")
    return _ran(_parse_largest_entries(output))


RULES: tuple[Rule, ...] = (
    Rule("pasted-images", RuleKind.CLEAN, lambda ctx: _ran(pasted_images(ctx.layout, ctx.now, ctx.limits))),
    Rule("trash", RuleKind.CLEAN, lambda ctx: _ran(expired_trash(ctx.layout, ctx.now, ctx.limits))),
    Rule(DOCKER_RULE, RuleKind.CLEAN, _rule_docker),
    Rule("unknown-home-entry", RuleKind.AUDIT, lambda ctx: _ran(unknown_home_entries(ctx.layout))),
    Rule("git-root-litter", RuleKind.AUDIT, lambda ctx: _ran(git_root_litter(ctx.layout))),
    Rule("stale-worktree", RuleKind.AUDIT, lambda ctx: _ran(stale_worktrees(ctx.layout, ctx.issue_state, ctx.run))),
    Rule("downloads-inbox", RuleKind.AUDIT, lambda ctx: _ran(old_downloads(ctx.layout, ctx.now, ctx.limits))),
    Rule("codex-sessions", RuleKind.AUDIT, lambda ctx: _ran(old_codex_sessions(ctx.layout, ctx.now, ctx.limits))),
)
LARGEST_ENTRIES_RULE = Rule("largest-entries", RuleKind.AUDIT, _rule_largest_entries)


@dataclass(frozen=True)
class Findings:
    """Clean findings may be applied; audit findings only ever reach the report."""

    clean: tuple[Finding, ...]
    audit: tuple[Finding, ...]
    checked_clean: tuple[str, ...] = ()
    not_applicable: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        misfiled = [f for f in self.clean if not f.is_cleanup] + [f for f in self.audit if f.is_cleanup]
        if misfiled:
            raise ValueError(f"finding filed under the wrong kind: {misfiled}")
        mismatched_verdict = [f for f in self.clean if f.verdict is not Verdict.DELETE] + [
            f for f in self.audit if f.verdict is Verdict.DELETE
        ]
        if mismatched_verdict:
            raise ValueError(f"finding verdict does not match its kind: {mismatched_verdict}")

    def __iter__(self) -> Iterator[Finding]:
        yield from self.clean
        yield from self.audit


def collect_findings(
    layout: Layout,
    now: datetime,
    options: Options,
    limits: Limits,
    run: CommandRunner = run_command,
    issue_state: IssueStateLookup | None = None,
) -> Findings:
    lookup = issue_state or (lambda repo, number: github_issue_state(repo, number, run))
    ctx = RuleContext(layout, now, limits, run, lookup)
    rules = RULES + ((LARGEST_ENTRIES_RULE,) if options.sizes else ())
    outcomes = {rule.name: rule.check(ctx) for rule in rules}

    clean = tuple(f for rule in rules if rule.kind is RuleKind.CLEAN for f in outcomes[rule.name].findings)
    audit = tuple(f for rule in rules if rule.kind is RuleKind.AUDIT for f in outcomes[rule.name].findings)
    checked_clean = tuple(rule.name for rule in rules if outcomes[rule.name].status is RuleStatus.CLEAN)
    not_applicable = tuple(
        (rule.name, outcomes[rule.name].reason) for rule in rules if outcomes[rule.name].status is RuleStatus.NOT_APPLICABLE
    )
    return Findings(clean, audit, checked_clean, not_applicable)


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


def _relative_to_home(subject: str, home: Path) -> str:
    try:
        return f"~/{Path(subject).relative_to(home)}"
    except ValueError:
        return subject


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(_escape_cell(cell) or "-" for cell in row) + " |" for row in rows)
    return lines


def _recommendation_cell(finding: Finding) -> str:
    """A RUN finding shows the exact command --apply executes, not a paraphrase of it."""
    if finding.action is Action.RUN:
        return f"--apply → `{' '.join(finding.command)}`"
    return finding.recommendation


def _maintenance_footer(apply: bool, housekeeping: str) -> str:
    if apply:
        return f"Wartung: {housekeeping}" if housekeeping else ""
    return HOUSEKEEPING_DRY_RUN_NOTE


def render_report(
    findings: Findings,
    now: datetime,
    home: Path,
    apply: bool = False,
    applied: dict[Finding, str] | None = None,
    housekeeping: str = "",
) -> str:
    applied = applied or {}
    all_findings = list(findings)
    mode = "Apply" if apply else "Dry-Run"
    reclaimable = sum(f.reclaimable_bytes for f in all_findings if f.rule == DOCKER_RULE)
    reclaim_clause = f" | docker meldet ~{_format_gb(reclaimable)} wiederverwendbar" if reclaimable else ""
    header = f"# tidy-home {now:%d.%m.%Y %H:%M}  {mode} | {len(all_findings)} Funde{reclaim_clause}"

    footnotes = []
    if findings.checked_clean:
        footnotes.append("Geprüft, nichts gefunden: " + ", ".join(findings.checked_clean))
    if findings.not_applicable:
        footnotes.append("Nicht geprüft: " + ", ".join(f"{name} ({reason})" for name, reason in findings.not_applicable))
    footer = _maintenance_footer(apply, housekeeping)
    if footer:
        footnotes.append(footer)

    if not all_findings:
        lines = [header, "", "Nichts zu tun."]
        for footnote in footnotes:
            lines += ["", footnote]
        return "\n".join(lines).rstrip() + "\n"

    headers = ["Regel", "Fund", "Alter/Größe", "Urteil", "Empfehlung"]
    if apply:
        headers.append("Ergebnis")
    rows = []
    for finding in all_findings:
        row = [
            finding.rule,
            _relative_to_home(finding.subject, home),
            finding.detail,
            finding.verdict.value,
            _recommendation_cell(finding),
        ]
        if apply:
            row.append(applied.get(finding, ""))
        rows.append(row)

    lines = [header, "", *_markdown_table(headers, rows)]
    for footnote in footnotes:
        lines += ["", footnote]
    return "\n".join(lines).rstrip() + "\n"


def notify(report: str, run: CommandRunner) -> None:
    """Desktop nudge for the timer; a no-op (via run_command) when notify-send is missing."""
    run(["notify-send", report.splitlines()[0]])


def exit_code(findings: Findings) -> int:
    """The timer treats 1 as success (SuccessExitStatus=1): findings are news, not failure."""
    return 1 if findings.clean or findings.audit else 0


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(prog="tidy-home", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="execute the clean findings (default: dry run)")
    parser.add_argument("--sizes", action="store_true", help="also list the largest home entries (slow)")
    namespace = parser.parse_args(argv)
    return Options(apply=namespace.apply, sizes=namespace.sizes)


def main(
    argv: Sequence[str] | None = None,
    home: Path | None = None,
    run: CommandRunner = run_command,
) -> int:
    options = parse_args(argv)
    layout = Layout(home or Path.home())
    try:
        limits = load_limits(layout.config_file)
    except (ValueError, OSError) as error:  # tomllib.TOMLDecodeError is a ValueError subclass
        print(f"tidy-home: {error}", file=sys.stderr)
        return 2

    now = datetime.now()
    findings = collect_findings(layout, now, options, limits, run=run)
    applied = {f: apply_finding(f, run=run) for f in findings.clean} if options.apply else {}
    housekeeping = run_housekeeping(run) if options.apply else ""
    report = render_report(findings, now, layout.home, options.apply, applied, housekeeping)
    layout.report_file.parent.mkdir(parents=True, exist_ok=True)
    layout.report_file.write_text(report)
    sys.stdout.write(report)
    code = exit_code(findings)
    if code:
        notify(report, run)
    return code


if __name__ == "__main__":
    sys.exit(main())
