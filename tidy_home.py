#!/usr/bin/env python3
"""tidy-home keeps the home directory in the shape ~/README.md describes.

Without --apply it is a dry run: every finding is printed, nothing changes.
With --apply the CLEAN findings are executed. AUDIT findings are never acted
on automatically; they name what a person has to decide.

Exit code: 1 when any finding exists (dry run or apply), 0 when the home is
clean. That is not a failure — it is the signal the timer notifies on.
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

WORKTREE_ISSUE_PATTERN = re.compile(r"issue-(\d+)")
TRASHINFO_DATE_PATTERN = re.compile(r"^DeletionDate=(.+)$", re.MULTILINE)
DOCKER_CREATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
DOCKER_CREATED_AT_PREFIX_LENGTH = len("2026-09-05 15:47:49")  # docker appends "+0200 CEST"

BACKTICK_TOKEN_PATTERN = re.compile(r"`([^`]+)`")
NON_ENTRY_TOKEN_MARKERS = (" ", "*", "--", "<")

RECLAIMABLE_PATTERN = re.compile(r"reclaimable\s+([\d.]+)\s*(GB|MB|kB)")
BYTES_PER_UNIT = {"GB": 1_000_000_000, "MB": 1_000_000, "kB": 1_000}
GIGABYTE = BYTES_PER_UNIT["GB"]


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

    @property
    def is_cleanup(self) -> bool:
        return self.action is not Action.REPORT


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


def load_limits(config_file: Path) -> Limits:
    """Defaults apply when the file is absent; an unknown key is a config error."""
    if not config_file.is_file():
        return Limits()
    with config_file.open("rb") as handle:
        raw = tomllib.load(handle)
    known_keys = {field.name for field in fields(Limits)}
    unknown_keys = sorted(set(raw) - known_keys)
    if unknown_keys:
        raise ValueError(f"unknown tidy-home config key(s): {', '.join(unknown_keys)}")
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
        Finding("pasted-images", Action.DELETE, str(image), Verdict.DELETE, "--apply", _days(_age(image, now)))
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
        for info_file in sorted((trash / "info").glob("*.trashinfo")):
            deleted_at = _trash_deletion_date(info_file)
            if deleted_at is None or now - deleted_at <= limits.trash_max_age:
                continue
            item = trash / "files" / info_file.name.removesuffix(".trashinfo")
            for path in (item, info_file):
                if path.exists():
                    findings.append(Finding("trash", Action.DELETE, str(path), Verdict.DELETE, "--apply", _days(now - deleted_at)))
    return findings


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


def _docker_usage(run: CommandRunner) -> dict[str, str]:
    """Map docker's usage types ('Images', 'Build Cache', ...) to 'size, reclaimable'."""
    output = run(["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"]) or ""
    usage: dict[str, str] = {}
    for line in output.splitlines():
        kind, size, reclaimable = line.split("\t")
        usage[kind] = f"{size}, reclaimable {reclaimable}"
    return usage


def _reclaimable_bytes(detail: str) -> int:
    match = RECLAIMABLE_PATTERN.search(detail)
    if not match:
        return 0
    return int(float(match.group(1)) * BYTES_PER_UNIT[match.group(2)])


def _format_gb(total_bytes: int) -> str:
    return f"{total_bytes / GIGABYTE:.1f}".replace(".", ",") + " GB"


def _reclaim_recommendation(detail: str) -> str:
    reclaimable = _reclaimable_bytes(detail)
    return f"--apply, {_format_gb(reclaimable)}" if reclaimable else "--apply"


def docker_leftovers(run: CommandRunner, now: datetime, limits: Limits) -> list[Finding]:
    """Stopped probe containers, dangling images and the build cache grow without bound."""
    if run(["docker", "info", "--format", "{{.ServerVersion}}"]) is None:
        return []
    max_age = limits.docker_stopped_container_max_age
    until = f"{max_age // timedelta(hours=1)}h"
    usage = _docker_usage(run)
    stale_containers = _docker_stopped_containers(run, now, max_age)
    images_detail = usage.get("Images", "")
    cache_detail = usage.get("Build Cache", "")
    return [
        Finding(
            "docker",
            Action.RUN,
            "stopped containers older than " + until,
            Verdict.DELETE,
            "--apply",
            ", ".join(stale_containers) or "none",
            ("docker", "container", "prune", "-f", "--filter", f"until={until}"),
        ),
        Finding(
            "docker",
            Action.RUN,
            "dangling images",
            Verdict.DELETE,
            _reclaim_recommendation(images_detail),
            images_detail,
            ("docker", "image", "prune", "-f"),
        ),
        Finding(
            "docker",
            Action.RUN,
            f"build cache beyond {limits.docker_build_cache_keep}",
            Verdict.DELETE,
            _reclaim_recommendation(cache_detail),
            cache_detail,
            ("docker", "builder", "prune", "-f", f"--keep-storage={limits.docker_build_cache_keep}"),
        ),
    ]


def package_caches(run: CommandRunner) -> list[Finding]:
    findings: list[Finding] = []
    if run(["uv", "--version"]):
        findings.append(Finding("caches", Action.RUN, "uv cache", Verdict.DELETE, "--apply", "", ("uv", "cache", "prune")))
    if run(["npm", "--version"]):
        findings.append(Finding("caches", Action.RUN, "npm cache", Verdict.DELETE, "--apply", "", ("npm", "cache", "verify")))
    return findings


# --- AUDIT rules: report only ---------------------------------------------


def _is_entry_name_token(token: str) -> bool:
    return not any(marker in token for marker in NON_ENTRY_TOKEN_MARKERS)


def _known_home_entry_names(readme_text: str) -> set[str]:
    names = {"README.md"}
    for token in BACKTICK_TOKEN_PATTERN.findall(readme_text):
        if not _is_entry_name_token(token):
            continue
        names.add(token.removeprefix("~/").split("/", 1)[0])
    return names


def unknown_home_entries(layout: Layout) -> list[Finding]:
    """Anything in the home root that ~/README.md does not know is either new or litter."""
    if not layout.readme.is_file():
        return [Finding("unknown-home-entry", Action.REPORT, "README.md fehlt", Verdict.REVIEW, "anlegen")]
    known = _known_home_entry_names(layout.readme.read_text(errors="replace"))
    return [
        Finding(
            "unknown-home-entry",
            Action.REPORT,
            str(entry),
            Verdict.REVIEW,
            "Symlink-Ziel prüfen" if entry.is_symlink() else "in ~/README.md eintragen oder löschen",
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
                Verdict.REVIEW,
                "in ein Repo verschieben oder löschen",
                "neither a repo nor a <repo>-worktrees dir",
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
                        Verdict.REMOVE,
                        recommendation,
                        "not registered in git worktree list",
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
                        Verdict.REMOVE,
                        recommendation,
                        f"issue #{match.group(1)} is closed",
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
            Verdict.REVIEW,
            "nach Dokumente/ einsortieren oder löschen",
            _days(_age(entry, now)),
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
    return [
        Finding(
            "codex-sessions",
            Action.REPORT,
            str(layout.codex_sessions),
            Verdict.REVIEW,
            "alte Sessions prüfen und bei Bedarf löschen",
            f"{len(old)} files older than {limits.codex_session_days} d, {size / 2**30:.1f} GiB",
        )
    ]


def largest_entries(layout: Layout, run: CommandRunner) -> list[Finding]:
    output = run(["du", "-s", "--block-size=1M", *(str(p) for p in layout.home.iterdir())]) or ""
    sized = sorted(
        ((int(size), path) for size, path in (line.split("\t", 1) for line in output.splitlines())),
        reverse=True,
    )
    return [
        Finding("largest-entries", Action.REPORT, path, Verdict.REVIEW, "Größe prüfen", f"{size / 1024:.1f} GiB")
        for size, path in sized[:LARGEST_ENTRIES_SHOWN]
    ]


# --- orchestration --------------------------------------------------------


@dataclass(frozen=True)
class Options:
    apply: bool = False
    sizes: bool = False


ALWAYS_RUN_RULES: tuple[str, ...] = (
    "pasted-images",
    "trash",
    "docker",
    "caches",
    "unknown-home-entry",
    "git-root-litter",
    "stale-worktree",
    "downloads-inbox",
    "codex-sessions",
)


@dataclass(frozen=True)
class Findings:
    """Clean findings may be applied; audit findings only ever reach the report."""

    clean: tuple[Finding, ...]
    audit: tuple[Finding, ...]
    ran_rules: tuple[str, ...] = ()

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
    clean = (
        *pasted_images(layout, now, limits),
        *expired_trash(layout, now, limits),
        *docker_leftovers(run, now, limits),
        *package_caches(run),
    )
    audit = (
        *unknown_home_entries(layout),
        *git_root_litter(layout),
        *stale_worktrees(layout, lookup, run),
        *old_downloads(layout, now, limits),
        *old_codex_sessions(layout, now, limits),
        *(largest_entries(layout, run) if options.sizes else ()),
    )
    ran_rules = ALWAYS_RUN_RULES + (("largest-entries",) if options.sizes else ())
    return Findings(clean, audit, ran_rules)


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


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(cell or "-" for cell in row) + " |" for row in rows)
    return lines


def render_report(
    findings: Findings,
    now: datetime,
    home: Path,
    apply: bool = False,
    applied: dict[Finding, str] | None = None,
) -> str:
    applied = applied or {}
    all_findings = list(findings)
    mode = "Apply" if apply else "Dry-Run"
    reclaimable = sum(_reclaimable_bytes(f.detail) for f in all_findings)
    reclaim_clause = f" | --apply gäbe ~{_format_gb(reclaimable)} frei" if reclaimable else ""
    header = f"# tidy-home {now:%d.%m.%Y %H:%M}  {mode} | {len(all_findings)} Funde{reclaim_clause}"
    if not all_findings:
        return f"{header}\n\nNichts zu tun.\n"

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
            finding.recommendation,
        ]
        if apply:
            row.append(applied.get(finding, ""))
        rows.append(row)

    lines = [header, "", *_markdown_table(headers, rows)]
    found_rules = {f.rule for f in all_findings}
    checked_empty = [rule for rule in findings.ran_rules if rule not in found_rules]
    if checked_empty:
        lines.append("")
        lines.append("Geprüft, nichts gefunden: " + ", ".join(checked_empty))
    return "\n".join(lines).rstrip() + "\n"


def notify(report: str, run: CommandRunner) -> None:
    """Desktop nudge for the timer; a no-op (via run_command) when notify-send is missing."""
    run(["notify-send", report.splitlines()[0]])


def exit_code(findings: Findings) -> int:
    """The timer treats 1 as success (SuccessExitStatus=1): findings are news, not failure."""
    return 1 if list(findings) else 0


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(prog="tidy-home", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="execute the clean findings (default: dry run)")
    parser.add_argument("--sizes", action="store_true", help="also list the largest home entries (slow)")
    namespace = parser.parse_args(argv)
    return Options(apply=namespace.apply, sizes=namespace.sizes)


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    layout = Layout(Path.home())
    limits = load_limits(layout.config_file)
    now = datetime.now()
    findings = collect_findings(layout, now, options, limits)
    applied = {f: apply_finding(f) for f in findings.clean} if options.apply else {}
    report = render_report(findings, now, layout.home, options.apply, applied)
    layout.report_file.parent.mkdir(parents=True, exist_ok=True)
    layout.report_file.write_text(report)
    sys.stdout.write(report)
    code = exit_code(findings)
    if code:
        notify(report, run_command)
    return code


if __name__ == "__main__":
    sys.exit(main())
