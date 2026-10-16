"""`home setup`: quality, its engines and the global commit hook, with the activation last."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from shlex import quote

from home_tools.secrets_view import ExitCodeProbe
from home_tools.tidy_home import CommandRunner, Config, Layout

# A commit SHA cannot move and needs no tag; a bump is a one-line change here.
QUALITY_SOURCE = "overnightworks-quality @ git+https://github.com/overnightworks/quality@615764ea67b0f8e2f615ee23a2432c50e11e5e68"
HOOK_NAMES = ("pre-commit", "pre-merge-commit")
HOOK_TEMPLATE = Path(__file__).parent / "hooks" / "commit-hook"
HOOK_MODE = 0o755

SETUP_EXAMPLES = """\
Beispiele:
  home setup           zeigt, welche Schritte etwas ändern würden
  home setup --apply   führt sie der Reihe nach aus; die Aktivierung kommt zuletzt
"""


@dataclass(frozen=True)
class Step:
    name: str
    is_current: Callable[[], bool]
    apply: Callable[[], bool]


def render_hook(layout: Layout) -> str:
    template = HOOK_TEMPLATE.read_text()
    return template.replace("@QUALITY@", quote(str(layout.quality_executable))).replace("@ENGINES@", quote(str(layout.quality_engines_dir)))


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _has_hook_mode(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o777 == HOOK_MODE


def _recorded_quality_source_matches(layout: Layout) -> bool:
    """uv records a git requirement as `<url>?rev=<sha>`."""
    name, _, source = QUALITY_SOURCE.partition(" @ ")
    url, _, revision = source.removeprefix("git+").rpartition("@")
    try:
        receipt = tomllib.loads(layout.quality_uv_receipt.read_text())
    except (OSError, ValueError):
        return False
    requirements = receipt.get("tool", {}).get("requirements", [])
    return any(requirement.get("name") == name and requirement.get("git") == f"{url}?rev={revision}" for requirement in requirements)


def _write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(content)
    temporary.chmod(HOOK_MODE)
    temporary.replace(path)


def _steps(layout: Layout, run: CommandRunner) -> tuple[Step, ...]:
    quality, engines_dir = str(layout.quality_executable), str(layout.quality_engines_dir)
    hook_content = render_hook(layout)
    hook_paths = [layout.git_hooks_dir / name for name in HOOK_NAMES]

    def quality_current() -> bool:
        return _recorded_quality_source_matches(layout) and _is_executable(layout.quality_executable)

    def install_quality() -> bool:
        return run(["uv", "tool", "install", "--force", QUALITY_SOURCE]) is not None and _is_executable(layout.quality_executable)

    def engines_current() -> bool:
        return _is_executable(layout.quality_executable) and run([quality, "engines", "verify", engines_dir]) is not None

    def install_engines() -> bool:
        return run([quality, "engines", "install", engines_dir]) is not None and engines_current()

    def hook_current(path: Path) -> bool:
        return _has_hook_mode(path) and path.read_text() == hook_content

    def write_hooks() -> bool:
        for path in hook_paths:
            if not hook_current(path):
                _write_atomically(path, hook_content)
        return True

    def hooks_active() -> bool:
        return (run(["git", "config", "--global", "--get", "core.hooksPath"]) or "").strip() == str(layout.git_hooks_dir)

    def activate_hooks() -> bool:
        return run(["git", "config", "--global", "core.hooksPath", str(layout.git_hooks_dir)]) is not None

    return (
        Step("quality", quality_current, install_quality),
        Step("Engines", engines_current, install_engines),
        Step("Hook-Dateien", lambda: all(hook_current(path) for path in hook_paths), write_hooks),
        Step("Aktivierung", hooks_active, activate_hooks),
    )


def _applied(step: Step) -> bool:
    try:
        return step.apply()
    except OSError as error:
        print(f"home setup: {error}", file=sys.stderr)
        return False


def setup(layout: Layout, run: CommandRunner, apply: bool) -> int:
    """Stops at the first failed step, so a missing scanner never gets a hook activated."""
    for step in _steps(layout, run):
        if step.is_current():
            print(f"{step.name}: unverändert")
        elif not apply:
            print(f"{step.name}: würde geändert")
        elif _applied(step):
            print(f"{step.name}: geändert")
        else:
            print(f"home setup: Schritt „{step.name}“ ist fehlgeschlagen; die späteren Schritte sind nicht gelaufen", file=sys.stderr)
            return 1
    return 0


def _setup_from_args(
    args: argparse.Namespace, layout: Layout, _config: Config, run: CommandRunner, _probe: ExitCodeProbe, _now: datetime
) -> int:
    return setup(layout, run, args.apply)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "setup",
        help="quality, Engines und den globalen Commit-Hook einrichten",
        description="Zeigt, was fehlt, oder richtet mit --apply quality, die Engines und den Hook ein, die Aktivierung zuletzt.",
        epilog=SETUP_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="führt die Schritte aus, statt sie nur zu zeigen")
    parser.set_defaults(handler=_setup_from_args)
