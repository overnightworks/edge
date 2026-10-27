"""The appkit step of `edge apply --closed`: the installed appkit's static files (tokens and fonts),
compared with `legal/assets/appkit-<version>/` and written there after the operator's `yes`.

The legal unit serves that directory at `/_rechtliches/appkit-<version>/`, so the version in the
path is the version in the lock. The step runs whether or not `services.legal` is enabled, and
every refusal comes at plan time, before the question: nothing is written on one.
"""

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module, metadata
from types import ModuleType
from pathlib import Path

from edge.unit_plans import Progress

DISTRIBUTION = "overnightworks-appkit"
ASSETS_DIRECTORY = "legal/assets"
APPKIT_UNIT = "appkit"
_DIRECTORY_PREFIX = "appkit-"
_TEMPORARY_PREFIX = ".appkit-"
_DIRECTORY_MODE = 0o755  # nginx-unprivileged must read what a 0700 mkdtemp directory would hide
_FILE_MODE = 0o644


class AppkitError(Exception):
    """The step cannot plan: appkit is not installed, or the checkout has no assets directory."""


@dataclass(frozen=True)
class AppkitPlan:
    version: str
    source: Path
    directory: Path  # `legal/assets/appkit-<version>/`
    new: bool  # the directory is missing or differs from the installed files
    stale: tuple[str, ...]  # this step's other `appkit-*` directories, to be removed

    @property
    def changes_nothing(self) -> bool:
        return not (self.new or self.stale)

    def describe(self) -> list[str]:
        line = f"appkit {self.version}: {'new' if self.new else 'unchanged'}"
        if self.stale:
            line += f", removes {', '.join(self.stale)}"
        return [line]

    def write(self, progress: Progress) -> str | None:
        """Write the directory and remove the stale ones; the failure line, or None. The progress
        names this step only while it runs."""
        progress.unit, progress.step = APPKIT_UNIT, "write the appkit files"
        try:
            if self.new:
                _replace_directory(self.source, self.directory)
            for name in self.stale:
                shutil.rmtree(self.directory.parent / name)
        except OSError as error:
            return f"appkit: could not write {self.directory} ({error.strerror})"
        finally:
            progress.unit = progress.step = ""
        return None


def plan_appkit(
    repository: Path, find_package: Callable[[str], ModuleType] = import_module
) -> AppkitPlan:
    """What the step would write. The lookup happens here, not at import: `hal.stack_commands`
    imports `edge.apply`, and a machine without a synced environment must still run `hal`."""
    source = installed_static(find_package)
    directory = served_directory(repository)
    version = directory.name.removeprefix(_DIRECTORY_PREFIX)
    if not directory.parent.is_dir():
        raise AppkitError(f"{directory.parent} is missing, the checkout is broken")
    stale = tuple(
        sorted(
            path.name
            for path in directory.parent.glob(f"{_DIRECTORY_PREFIX}*")
            if path.is_dir() and path != directory
        )
    )
    return AppkitPlan(version, source, directory, _bytes_of(source) != _bytes_of(directory), stale)


def installed_static(find_package: Callable[[str], ModuleType] = import_module) -> Path:
    try:
        appkit = find_package("appkit")
    except ImportError as error:
        raise AppkitError("appkit is not installed, run `uv sync`") from error
    return Path(str(appkit.__file__)).parent / "static"


def served_directory(repository: Path) -> Path:
    return repository / ASSETS_DIRECTORY / f"{_DIRECTORY_PREFIX}{metadata.version(DISTRIBUTION)}"


def _bytes_of(directory: Path) -> dict[str, bytes]:
    """Every file under the directory by its relative path; empty while it does not exist."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _replace_directory(source: Path, directory: Path) -> None:
    """The files in a temporary directory beside the target, then renamed into place; a damaged
    directory moves aside first and moves back when the final rename fails."""
    temporary = Path(tempfile.mkdtemp(dir=directory.parent, prefix=_TEMPORARY_PREFIX))
    displaced = temporary / "displaced"
    try:
        shutil.copytree(source, temporary / "files", copy_function=_copy_readable)
        _make_readable(temporary / "files")
        if directory.exists():
            os.rename(directory, displaced)
        try:
            os.rename(temporary / "files", directory)
        except BaseException:
            if displaced.exists():
                os.rename(displaced, directory)
            raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _copy_readable(source: str, destination: str) -> str:
    shutil.copyfile(source, destination)
    os.chmod(destination, _FILE_MODE)  # NOSONAR python:S2612 public files, read by nginx
    return destination


def _make_readable(tree: Path) -> None:
    for directory in (tree, *(path for path in tree.rglob("*") if path.is_dir())):
        os.chmod(directory, _DIRECTORY_MODE)  # NOSONAR python:S2612 read by nginx
