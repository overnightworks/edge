"""edge's own pinned tofu: downloaded once, installed only when its sha256 matches the pins file.

`python -m edge.external.tofu_binary ROOT` installs it under ROOT and prints its folder; CI puts
that folder on PATH. It is a build helper, not an `edge` subcommand.
"""

import errno
import fcntl
import hashlib
import http.client
import os
import platform
import shutil
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from edge.external import secrets
from edge.external.tofu import (
    EXTERNAL_ROOT,
    PROJECT_ROOT,
    TofuError,
    pinned_version,
    require_pinned_version,
)

PINS_FILE = EXTERNAL_ROOT / "opentofu.toml"
PARTIAL_PREFIX = ".partial-"
# The linux_amd64 release zip is about 30 MiB; the limit only stops an endless or swapped stream.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_NETWORK_TIMEOUT_SECONDS = 60
_MEMBER = "tofu"
_MACHINES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

Fetch = Callable[[str], Iterator[bytes]]


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str


def _pins_name() -> str:
    return str(PINS_FILE.relative_to(PROJECT_ROOT))


def release_for(pins: dict, version: str, platform_name: str) -> Release:
    """The download the pins file records for this version and platform; a gap fails loud."""
    entry = pins.get("versions", {}).get(version)
    if entry is None:
        raise TofuError(f"{_pins_name()} has no entry for OpenTofu {version}")
    sha256 = entry.get("sha256", {}).get(platform_name)
    if sha256 is None:
        raise TofuError(f"{_pins_name()} has no sha256 for {platform_name} of OpenTofu {version}")
    url_template = entry.get("url")
    if url_template is None:
        raise TofuError(f"{_pins_name()} has no url for OpenTofu {version}")
    url = url_template.format(version=version, platform=platform_name)
    return Release(version=version, url=url, sha256=sha256)


def current_platform() -> str:
    machine = platform.machine().lower()
    return f"{platform.system().lower()}_{_MACHINES.get(machine, machine)}"


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            # An OSError, so the download failure names the expected sha like every other one.
            raise urllib.error.URLError(f"redirects to {newurl}, which is not https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url: str) -> Iterator[bytes]:
    if not url.startswith("https://"):
        raise TofuError(f"{url} is not an https URL")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    with opener.open(url, timeout=_NETWORK_TIMEOUT_SECONDS) as response:
        while chunk := response.read(_CHUNK_BYTES):
            yield chunk


def ensure(root: Path, fetch: Fetch = fetch_https) -> Path:
    """The pinned tofu under `root`, downloaded and verified first when it is not there yet."""
    return install(root, release_for(_read_pins(), pinned_version(), current_platform()), fetch)


def _read_pins() -> dict:
    try:
        return tomllib.loads(PINS_FILE.read_text())
    except FileNotFoundError:
        raise TofuError(f"{_pins_name()} is missing") from None
    except tomllib.TOMLDecodeError as error:
        raise TofuError(f"{_pins_name()} is not valid TOML: {error}") from error


def install(
    root: Path, release: Release, fetch: Fetch, *, max_bytes: int = MAX_DOWNLOAD_BYTES
) -> Path:
    root = root.resolve()  # tofu runs with its own working directory, so the path must be absolute
    root.mkdir(parents=True, exist_ok=True)
    installed = root / release.version / _MEMBER
    with _exclusive(root):
        _remove_partials(root)
        if installed.exists():
            if _reports_pin(installed):
                return installed
            _discard(installed.parent)
        with _verified_download(root, release, fetch, max_bytes) as archive:
            _install_member(root, archive, release)
    return _verified_install(installed)


@contextmanager
def _exclusive(root: Path) -> Iterator[None]:
    """One install per root at a time, so the partials it cleans are never a live run's."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _verified_install(installed: Path) -> Path:
    try:
        require_pinned_version(installed, _version_environment())
    except (TofuError, OSError) as error:  # OSError: a file that cannot be executed at all
        raise TofuError(f"{installed} is not the pinned tofu: {error}") from error
    return installed


def _reports_pin(installed: Path) -> bool:
    try:
        _verified_install(installed)
    except TofuError:
        return False
    return True


def _discard(folder: Path) -> None:
    """Move the folder under a partial name before removing it: a run killed meanwhile leaves a
    leftover the next run cleans, never a half-removed `<version>`."""
    aside = Path(tempfile.mkdtemp(dir=folder.parent, prefix=PARTIAL_PREFIX))
    folder.rename(aside / folder.name)
    shutil.rmtree(aside)


def _version_environment() -> dict[str, str]:
    return {"PATH": secrets.SYSTEM_PATH, "LANG": secrets.TOFU_LANG}


def _remove_partials(root: Path) -> None:
    for leftover in root.glob(f"{PARTIAL_PREFIX}*"):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover)
        else:
            leftover.unlink()


@contextmanager
def _verified_download(
    root: Path, release: Release, fetch: Fetch, max_bytes: int
) -> Iterator[Path]:
    """The archive as a partial file inside `root`, yielded only once its sha256 matches."""
    descriptor, name = tempfile.mkstemp(dir=root, prefix=PARTIAL_PREFIX)
    archive = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as partial:
            actual = _stream(release, fetch, partial, max_bytes)
        if actual != release.sha256:
            raise TofuError(
                f"{release.url} has sha256 {actual}, {_pins_name()} records {release.sha256}"
            )
        yield archive
    finally:
        archive.unlink(missing_ok=True)


def _stream(release: Release, fetch: Fetch, partial, max_bytes: int) -> str:
    digest = hashlib.sha256()
    received = 0
    try:
        for chunk in fetch(release.url):
            received += len(chunk)
            digest.update(chunk)
            if received > max_bytes:
                raise _download_failure(
                    release,
                    f"exceeds the {max_bytes}-byte download limit",
                    received,
                    digest.hexdigest(),
                )
            partial.write(chunk)
    except (OSError, http.client.HTTPException) as error:
        raise _download_failure(
            release, f"could not be downloaded ({error})", received, digest.hexdigest()
        ) from error
    return digest.hexdigest()


def _download_failure(
    release: Release, reason: str, received: int, received_sha256: str
) -> TofuError:
    return TofuError(
        f"{release.url} {reason}; the {received} bytes received so far have sha256"
        f" {received_sha256}, {_pins_name()} records {release.sha256}"
    )


def _install_member(root: Path, archive: Path, release: Release) -> None:
    """Write the one member `tofu` into a partial folder, then rename that folder to the version."""
    folder = Path(tempfile.mkdtemp(dir=root, prefix=PARTIAL_PREFIX))
    try:
        _write_executable(archive, folder / _MEMBER, release.url)
        try:
            folder.rename(root / release.version)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            return  # a parallel install got there first; its folder is verified by the caller
        _fsync_directory(root)
    finally:
        if folder.exists():
            shutil.rmtree(folder)


def _write_executable(archive: Path, target: Path, url: str) -> None:
    with zipfile.ZipFile(archive) as release_zip:
        try:
            member = release_zip.open(_MEMBER)
        except KeyError:
            raise TofuError(f"{url} has no member {_MEMBER}") from None
        with member, target.open("xb") as binary:
            shutil.copyfileobj(member, binary, _CHUNK_BYTES)
            os.fchmod(binary.fileno(), 0o700)  # only its owner runs it
            binary.flush()
            os.fsync(binary.fileno())
    _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: python -m edge.external.tofu_binary ROOT", file=sys.stderr)
        return 2
    try:
        installed = ensure(Path(arguments[0]))
    except TofuError as error:
        print(f"edge: {error}", file=sys.stderr)
        return 2
    print(installed.parent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
