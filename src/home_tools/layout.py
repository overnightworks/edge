"""The paths `home` derives from the home directory, and the one way it runs an external command."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    """Every path `home` touches, derived from one home directory."""

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
        return self.home / ".config" / "home" / "config.toml"

    @property
    def legacy_config_file(self) -> Path:
        return self.home / ".config" / "tidy-home" / "config.toml"

    @property
    def pasted_image_dirs(self) -> tuple[Path, ...]:
        return (self.home, self.git, self.home / "Dokumente", self.home / "Schreibtisch")

    @property
    def local_dir(self) -> Path:
        return self.home / ".local"

    @property
    def data_dir(self) -> Path:
        return self.local_dir / "share"

    @property
    def trash_dirs(self) -> tuple[Path, ...]:
        gnome = self.data_dir / "Trash"
        vscode_snap = sorted((self.home / "snap" / "code").glob("*/.local/share/Trash"))
        return (gnome, *vscode_snap)

    @property
    def state_dir(self) -> Path:
        return self.local_dir / "state" / "home"

    @property
    def system_step_record(self) -> Path:
        """The digest of what the system step rendered at its last verified run; the next-steps command compares it, never sudo."""
        return self.state_dir / "system-step.sha256"

    @property
    def tidy_report_file(self) -> Path:
        return self.state_dir / "tidy-report.md"

    @property
    def check_report_file(self) -> Path:
        return self.state_dir / "check-report.md"

    @property
    def check_findings_file(self) -> Path:
        return self.state_dir / "check-findings.txt"

    @property
    def secrets_dir(self) -> Path:
        return self.home / ".secrets"

    @property
    def git_hooks_dir(self) -> Path:
        return self.data_dir / "home" / "git-hooks"

    @property
    def quality_executable(self) -> Path:
        return self.local_dir / "bin" / "quality"

    @property
    def quality_engines_dir(self) -> Path:
        return self.data_dir / "quality" / "engines"

    @property
    def quality_uv_receipt(self) -> Path:
        return self.data_dir / "uv" / "tools" / "overnightworks-quality" / "uv-receipt.toml"

    @property
    def ansible_playbook_executable(self) -> Path:
        return self.local_dir / "bin" / "ansible-playbook"

    @property
    def ansible_uv_receipt(self) -> Path:
        return self.data_dir / "uv" / "tools" / "ansible-core" / "uv-receipt.toml"

    @property
    def edge_tunnel_dir(self) -> Path:
        """edge's cloudflared unit, its drop-ins and the polkit rule, which the system step installs."""
        return self.git / "edge" / "tunnel"

    @property
    def secrets_report_file(self) -> Path:
        return self.state_dir / "secrets-report.md"


IssueStateLookup = Callable[[Path, int], str | None]
CommandRunner = Callable[[Sequence[str]], str | None]

COMMAND_TIMEOUT_SECONDS = 120


def run_command(argv: Sequence[str]) -> str | None:
    """Run an external command; None when the tool is missing, hangs or fails."""
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def registered_worktrees(repo: Path, run: CommandRunner, resolve_links: bool = True) -> set[Path]:
    listing = run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"]) or ""
    return {(Path.resolve if resolve_links else Path)(Path(field.removeprefix("worktree "))) for field in listing.split("\0") if field.startswith("worktree ")}
