"""`home setup`: quality, its engines and the global commit hook, then Ansible and the system step.

The activation comes before the system step, so a wrong sudo password never keeps the commit hook from being active.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING

from home_tools.system_step import Applied, CheckState, StepFailed, SystemStep
from home_tools.tidy_home import Config, Layout

if TYPE_CHECKING:
    from home_tools.cli import Ports

# A commit SHA cannot move and needs no tag; a bump is a one-line change here.
QUALITY_SOURCE = "overnightworks-quality @ git+https://github.com/overnightworks/quality@615764ea67b0f8e2f615ee23a2432c50e11e5e68"
# The engine of the system step; a bump is a one-line change here.
ANSIBLE_SOURCE = "ansible-core==2.21.5"
HOOK_NAMES = ("pre-commit", "pre-merge-commit")
HOOK_TEMPLATE = Path(__file__).parent / "hooks" / "commit-hook"
HOOK_MODE = 0o755
HOOK_WHY = "ohne den Hook prüft kein Commit auf Geheimnisse"
SYSTEM_WHY = "without it the tunnel ignores edge's global stop after a reboot, and only root may stop it"
# `--apply` runs every step, a verified one too, and the system step always asks for sudo, which needs a person at a terminal.
# The one fact `home next` reads for this command, as it reads `secrets_setup.SETUP_NEEDS_A_TERMINAL` for the wizard.
APPLY_ASKS_FOR_SUDO = True

SETUP_EXAMPLES = """\
Beispiele:
  home setup           zeigt, welche Schritte etwas ändern würden
  home setup --apply   führt sie der Reihe nach aus; the activation comes before the system step, which asks once for sudo
"""


@dataclass(frozen=True)
class Step:
    """`is_verified` is all `home next` asks, so it never needs sudo; `check` is the dry run, `apply` the run.

    `needs_first` names what a `not checked` dry run lacks; only the system step can end up there.
    """

    name: str
    is_verified: Callable[[], bool]
    check: Callable[[], CheckState]
    apply: Callable[[], Applied]
    why: str
    needs_first: Callable[[], str] = lambda: ""


def _local_step(name: str, is_current: Callable[[], bool], install: Callable[[], bool], why: str) -> Step:
    """A step in the user's own home: its check needs no password, so it is never `not checked`."""

    def check() -> CheckState:
        return CheckState.UNCHANGED if is_current() else CheckState.WOULD_CHANGE

    def apply() -> Applied:
        if is_current():
            return Applied.UNCHANGED
        if not install():
            raise StepFailed
        return Applied.CHANGED

    return Step(name, is_current, check, apply, why)


def render_hook(layout: Layout) -> str:
    template = HOOK_TEMPLATE.read_text()
    return template.replace("@QUALITY@", quote(str(layout.quality_executable))).replace("@ENGINES@", quote(str(layout.quality_engines_dir)))


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _has_hook_mode(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o777 == HOOK_MODE


def _uv_recorded(receipt_path: Path, requirement: Mapping[str, str]) -> bool:
    """Whether uv's receipt for a tool records this requirement, so a pin that moved reinstalls the tool."""
    try:
        receipt = tomllib.loads(receipt_path.read_text())
    except (OSError, ValueError):
        return False
    recorded = receipt.get("tool", {}).get("requirements", [])
    return any(all(entry.get(key) == value for key, value in requirement.items()) for entry in recorded)


def _recorded_quality_source_matches(layout: Layout) -> bool:
    """uv records a git requirement as `<url>?rev=<sha>`."""
    name, _, source = QUALITY_SOURCE.partition(" @ ")
    url, _, revision = source.removeprefix("git+").rpartition("@")
    return _uv_recorded(layout.quality_uv_receipt, {"name": name, "git": f"{url}?rev={revision}"})


def _recorded_ansible_source_matches(layout: Layout) -> bool:
    """uv records a pinned version as its specifier."""
    name, _, version = ANSIBLE_SOURCE.partition("==")
    return _uv_recorded(layout.ansible_uv_receipt, {"name": name, "specifier": f"=={version}"})


def _write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(content)
    temporary.chmod(HOOK_MODE)
    temporary.replace(path)


def _steps(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    run = ports.run
    quality, engines_dir = str(layout.quality_executable), str(layout.quality_engines_dir)
    hook_content = render_hook(layout)
    hook_paths = [layout.git_hooks_dir / name for name in HOOK_NAMES]
    system = SystemStep(layout, ports.system)

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

    def ansible_current() -> bool:
        return _recorded_ansible_source_matches(layout) and _is_executable(layout.ansible_playbook_executable)

    def install_ansible() -> bool:
        return run(["uv", "tool", "install", "--force", ANSIBLE_SOURCE]) is not None and ansible_current()

    return (
        _local_step("quality", quality_current, install_quality, HOOK_WHY),
        _local_step("Engines", engines_current, install_engines, HOOK_WHY),
        _local_step("Hook-Dateien", lambda: all(hook_current(path) for path in hook_paths), write_hooks, HOOK_WHY),
        _local_step("Aktivierung", hooks_active, activate_hooks, HOOK_WHY),
        _local_step("Ansible", ansible_current, install_ansible, SYSTEM_WHY),
        Step("System", system.is_verified, system.check, system.apply, SYSTEM_WHY, system.needs_first),
    )


def pending(layout: Layout, ports: Ports) -> tuple[Step, ...]:
    """The steps `home setup --apply` would run; `home next` reads the plan through this, and it never asks for sudo."""
    return tuple(step for step in _steps(layout, ports) if not step.is_verified())


def _outcome(step: Step, apply: bool) -> str:
    if apply:
        return step.apply().value
    state = step.check()
    needs = step.needs_first() if state is CheckState.NOT_CHECKED else ""
    return f"{state.value} (needs {needs})" if needs else state.value


def setup(layout: Layout, ports: Ports, apply: bool) -> int:
    """Stops at the first failed step, so a missing scanner never gets a hook activated."""
    for step in _steps(layout, ports):
        try:
            print(f"{step.name}: {_outcome(step, apply)}")
        except (OSError, StepFailed) as error:
            if str(error):
                print(f"home setup: {error}", file=sys.stderr)
            print(f"home setup: Schritt „{step.name}“ ist fehlgeschlagen; die späteren Schritte sind nicht gelaufen", file=sys.stderr)
            return 1
    return 0


def _setup_from_args(args: argparse.Namespace, layout: Layout, _config: Config, ports: Ports) -> int:
    return setup(layout, ports, args.apply)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "setup",
        help="quality, Engines und den globalen Commit-Hook einrichten, then the system step",
        description=(
            "Zeigt, was fehlt, oder richtet mit --apply quality, die Engines und den Hook ein."
            " Then it installs Ansible and runs the system step, edge's cloudflared files and the stop-only polkit rule,"
            " which asks once for sudo at a terminal; the activation comes before the system step."
        ),
        epilog=SETUP_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="führt die Schritte aus, statt sie nur zu zeigen")
    parser.set_defaults(handler=_setup_from_args)
