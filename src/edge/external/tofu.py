"""The pinned `tofu`: version check, `init` and `plan` with their exit codes."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# uv installs the project editable, so this file sits in the checkout: src/edge/external/tofu.py.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXTERNAL_ROOT = PROJECT_ROOT / "external"
TOFURC = EXTERNAL_ROOT / "tofurc"
_PIN = re.compile(r'required_version\s*=\s*"=\s*([\w.+-]+)"')
_VERSION_LINE = re.compile(r"^OpenTofu v(\S+)")
_STATE_FILE = Path(".secrets") / "opentofu" / "fleet.tfstate"


class TofuError(Exception):
    """tofu is missing, is not the pinned version, or failed in a step before the plan."""


class PlanExit(IntEnum):
    """What `tofu plan -detailed-exitcode` returns."""

    NO_CHANGES = 0
    FAILED = 1
    CHANGES = 2


@dataclass(frozen=True)
class Completed:
    exit_code: int
    output: str


def pinned_version() -> str:
    """The version `external/main.tf` requires: the pin is stated once, there."""
    match = _PIN.search((EXTERNAL_ROOT / "main.tf").read_text())
    if match is None:
        raise TofuError(f"{EXTERNAL_ROOT / 'main.tf'} has no required_version pin")
    return match.group(1)


def locate_tofu() -> Path:
    found = shutil.which("tofu")
    if found is None:
        raise TofuError("tofu is not on PATH")
    return Path(found).resolve()


def init_arguments(home: Path) -> list[str]:
    return [
        "init",
        "-input=false",
        "-lockfile=readonly",
        f"-backend-config=path={home / _STATE_FILE}",
    ]


PLAN_ARGUMENTS = ["plan", "-refresh=true", "-detailed-exitcode"]


def run(tofu: Path, arguments: list[str], environment: dict[str, str]) -> Completed:
    result = subprocess.run(
        [str(tofu), *arguments],
        cwd=EXTERNAL_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return Completed(result.returncode, result.stdout + result.stderr)


def require_pinned_version(tofu: Path, environment: dict[str, str]) -> None:
    pinned = pinned_version()
    reported = run(tofu, ["version"], environment)
    match = _VERSION_LINE.match(reported.output)
    found = match.group(1) if match else None
    if reported.exit_code != 0 or found != pinned:
        raise TofuError(f"tofu is {found or 'of unknown version'}, the pin is {pinned}")
