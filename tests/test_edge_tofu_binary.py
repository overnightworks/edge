"""`tofu_binary`: edge installs only the pinned tofu whose download matches its recorded sha256."""

import email.message
import functools
import hashlib
import http.client
import io
import os
import tomllib
import urllib.request
import urllib.response
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from edge.external import tofu, tofu_binary
from edge.external.tofu import TofuError

URL = "https://downloads.invalid/tofu.zip"
FAKE_TOFU = b'#!/bin/sh\necho "OpenTofu v%s"\n'


def release_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def pinned_zip(**extra_members: bytes) -> bytes:
    executable = FAKE_TOFU % tofu.pinned_version().encode()
    return release_zip({"LICENSE": b"MPL-2.0", "tofu": executable, **extra_members})


def release_of(archive: bytes, sha256: str | None = None) -> tofu_binary.Release:
    return tofu_binary.Release(
        version=tofu.pinned_version(),
        url=URL,
        sha256=sha256 or hashlib.sha256(archive).hexdigest(),
    )


class Download:
    """The injected fetch: serves one archive and records every URL it was asked for."""

    def __init__(self, archive: bytes, during: Callable[[], None] = lambda: None) -> None:
        self.archive = archive
        self.during = during
        self.urls: list[str] = []

    def __call__(self, url: str) -> Iterator[bytes]:
        self.urls.append(url)
        yield half_of(self.archive)
        self.during()
        yield self.archive[len(half_of(self.archive)) :]


def half_of(archive: bytes) -> bytes:
    """The first chunk `Download` serves."""
    return archive[: len(archive) // 2]


def tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def assert_names_the_url_and_both_shas(message: str, recorded: str, received: bytes) -> None:
    assert URL in message
    assert recorded in message
    assert hashlib.sha256(received).hexdigest() in message


def connection_reset() -> None:
    raise OSError("connection reset")


def truncated_response() -> None:
    raise http.client.IncompleteRead(b"", 1024)


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "opentofu"


def test_an_empty_root_gets_the_verified_binary_runnable_by_its_owner_only(root):
    """#101 T1: an empty root gets the verified binary at 0700, runnable by its owner only."""
    archive = pinned_zip()
    download = Download(archive)

    installed = tofu_binary.install(root, release_of(archive), download)

    assert installed == root / tofu.pinned_version() / "tofu"
    assert download.urls == [URL]
    assert installed.stat().st_mode & 0o777 == 0o700
    assert tree(root) == {tofu.pinned_version(), f"{tofu.pinned_version()}/tofu"}


def test_a_relative_root_is_installed_and_verified_where_it_points(tmp_path, monkeypatch):
    """#101 rule 2: `ensure(root)` takes a root relative to the caller's working directory."""
    archive = pinned_zip()
    monkeypatch.chdir(tmp_path)

    installed = tofu_binary.install(Path("opentofu"), release_of(archive), Download(archive))

    assert installed == tmp_path / "opentofu" / tofu.pinned_version() / "tofu"


def test_an_installed_pinned_binary_is_reused_without_the_network(root):
    """#101 T2: an installed binary reporting the pin is returned with no network call."""
    archive = pinned_zip()
    first = tofu_binary.install(root, release_of(archive), Download(archive))
    again = Download(archive)

    assert tofu_binary.install(root, release_of(archive), again) == first
    assert again.urls == []


@pytest.mark.parametrize(
    ("content", "mode"),
    [
        (FAKE_TOFU % b"0.0.1", 0o755),
        (FAKE_TOFU % tofu.pinned_version().encode(), 0o644),
        (b"\x7fELF not a binary for this machine", 0o755),
    ],
    ids=["wrong-version", "not-executable", "garbage"],
)
def test_an_installed_binary_off_the_pin_is_replaced_by_the_verified_download(root, content, mode):
    """#101 rule 2 Download: a `<version>/tofu` that does not report the pin is replaced."""
    archive = pinned_zip()
    stale = root / tofu.pinned_version() / "tofu"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(content)
    stale.chmod(mode)
    download = Download(archive)

    installed = tofu_binary.install(root, release_of(archive), download)

    assert download.urls == [URL]
    assert installed.read_bytes() == FAKE_TOFU % tofu.pinned_version().encode()
    assert tree(root) == {tofu.pinned_version(), f"{tofu.pinned_version()}/tofu"}


def test_a_sha_mismatch_names_the_url_and_both_shas_and_installs_nothing(root):
    """#101 T3: a sha mismatch names the URL and both shas, and nothing is installed."""
    archive = pinned_zip()
    recorded = "0" * 64
    release = release_of(archive, sha256=recorded)
    download = Download(archive)

    with pytest.raises(TofuError) as raised:
        tofu_binary.install(root, release, download)

    assert_names_the_url_and_both_shas(str(raised.value), recorded, archive)
    assert tree(root) == set()


@pytest.mark.parametrize(
    "network_error", [connection_reset, truncated_response], ids=["os-error", "http-exception"]
)
def test_a_network_error_names_the_url_and_both_shas_and_installs_nothing(root, network_error):
    """#101 rule 2 Failure: a network error names the URL and both shas; nothing is installed."""
    archive = pinned_zip()
    release = release_of(archive)
    download = Download(archive, network_error)

    with pytest.raises(TofuError) as raised:
        tofu_binary.install(root, release, download)

    assert_names_the_url_and_both_shas(str(raised.value), release.sha256, half_of(archive))
    assert tree(root) == set()


def test_a_download_over_the_size_limit_names_both_shas_and_installs_nothing(root):
    """#101 T6: a download over the size limit is refused with both shas; nothing is installed.

    The diagnostics count and hash the chunk that crossed the limit, since it was received."""
    archive = pinned_zip()
    recorded = "0" * 64
    release = release_of(archive, sha256=recorded)
    download = Download(archive)

    with pytest.raises(TofuError, match="limit") as raised:
        tofu_binary.install(root, release, download, max_bytes=len(archive) - 1)

    assert_names_the_url_and_both_shas(str(raised.value), recorded, archive)
    assert f"the {len(archive)} bytes received" in str(raised.value)
    assert tree(root) == set()


def test_a_folder_a_parallel_run_installed_meanwhile_is_used_and_ours_removed(root):
    """#101 T7: a `<version>` a parallel run made meanwhile is used, and ours is removed."""
    archive = pinned_zip()
    theirs = root / tofu.pinned_version() / "tofu"

    def parallel_install() -> None:
        theirs.parent.mkdir()
        theirs.write_bytes(FAKE_TOFU % tofu.pinned_version().encode())
        theirs.chmod(0o755)
        os.utime(theirs, (0, 0))

    installed = tofu_binary.install(root, release_of(archive), Download(archive, parallel_install))

    assert installed == theirs
    assert installed.stat().st_mtime == 0
    assert tree(root) == {tofu.pinned_version(), f"{tofu.pinned_version()}/tofu"}


def test_a_parallel_run_folder_that_cannot_execute_fails_naming_it(root):
    """#101 rule 2: a race winner that cannot run fails loud as a TofuError naming its file."""
    archive = pinned_zip()
    theirs = root / tofu.pinned_version() / "tofu"

    def parallel_install() -> None:
        theirs.parent.mkdir()
        theirs.write_bytes(FAKE_TOFU % tofu.pinned_version().encode())
        theirs.chmod(0o644)

    release = release_of(archive)
    download = Download(archive, parallel_install)

    with pytest.raises(TofuError, match="is not the pinned tofu") as raised:
        tofu_binary.install(root, release, download)

    assert str(theirs) in str(raised.value)


@pytest.mark.parametrize("installed_before", [False, True], ids=["empty-root", "beside-install"])
def test_leftover_temp_names_of_a_killed_run_are_cleaned(root, installed_before):
    """#101 T8: leftover temp names of a killed run are cleaned, also beside a reused install."""
    archive = pinned_zip()
    if installed_before:
        tofu_binary.install(root, release_of(archive), Download(archive))
    root.mkdir(exist_ok=True)
    (root / f"{tofu_binary.PARTIAL_PREFIX}download").write_bytes(b"half a zip")
    (root / f"{tofu_binary.PARTIAL_PREFIX}folder").mkdir()
    (root / f"{tofu_binary.PARTIAL_PREFIX}folder" / "tofu").write_bytes(b"half a binary")

    tofu_binary.install(root, release_of(archive), Download(archive))

    assert tree(root) == {tofu.pinned_version(), f"{tofu.pinned_version()}/tofu"}


def test_member_names_that_escape_are_never_extracted(tmp_path, root):
    """#101 T9: member names that escape are never extracted; only `tofu` is read."""
    archive = pinned_zip(**{"../tofu": b"escaped", "../../evil": b"escaped"})

    installed = tofu_binary.install(root, release_of(archive), Download(archive))

    assert installed.read_bytes() == FAKE_TOFU % tofu.pinned_version().encode()
    assert tree(tmp_path) == {"opentofu", *(f"opentofu/{name}" for name in tree(root))}


def test_an_archive_without_the_tofu_member_installs_nothing(root):
    """#101 rule 2 Check before open: an archive without the member `tofu` installs nothing."""
    archive = release_zip({"../tofu": b"escaped"})
    release = release_of(archive)
    download = Download(archive)

    with pytest.raises(TofuError, match="tofu"):
        tofu_binary.install(root, release, download)
    assert tree(root) == set()


PINS = """
[versions."1.13.1"]
url = "https://example.invalid/v{version}/tofu_{version}_{platform}.zip"
sha256 = { linux_amd64 = "ab" }

[versions."1.13.2"]
sha256 = { linux_amd64 = "ab" }

[versions."1.13.3"]
url = "https://example.invalid/{arch}.zip"
sha256 = { linux_amd64 = "ab" }

[versions]
"1.13.4" = "not a table"
"""


def test_the_pinned_release_comes_from_its_entry_with_the_url_filled_in():
    """#101 rule 1: the release comes from its pins entry, with the URL template filled in."""
    release = tofu_binary.release_for(tomllib.loads(PINS), "1.13.1", "linux_amd64")

    assert release == tofu_binary.Release(
        version="1.13.1",
        url="https://example.invalid/v1.13.1/tofu_1.13.1_linux_amd64.zip",
        sha256="ab",
    )


@pytest.mark.parametrize(
    ("version", "platform", "missing"),
    [
        ("1.14.0", "linux_amd64", "no entry for OpenTofu 1.14.0"),
        ("1.13.1", "darwin_arm64", "no sha256 for darwin_arm64"),
        ("1.13.2", "linux_amd64", "no url for OpenTofu 1.13.2"),
        ("1.13.3", "linux_amd64", "url for OpenTofu 1.13.3 cannot be filled in: KeyError('arch')"),
        ("1.13.4", "linux_amd64", "entry for OpenTofu 1.13.4 is not a table"),
    ],
    ids=[
        "unpinned-version",
        "other-platform",
        "entry-without-url",
        "unknown-url-placeholder",
        "entry-not-a-table",
    ],
)
def test_a_release_without_its_entry_fails_naming_the_pins_file(version, platform, missing):
    """#101 T4, T5, rule 1: a gap in the version's entry names the pins file."""
    pins = tomllib.loads(PINS)

    with pytest.raises(TofuError) as raised:
        tofu_binary.release_for(pins, version, platform)

    assert missing in str(raised.value)
    assert "external/opentofu.toml" in str(raised.value)


def test_the_committed_pins_cover_the_pinned_version_on_linux_amd64():
    """#101 rule 1: the committed pins file covers the pinned version on linux_amd64."""
    pins = tomllib.loads(tofu_binary.PINS_FILE.read_text())

    release = tofu_binary.release_for(pins, tofu.pinned_version(), "linux_amd64")

    assert release.url.startswith("https://github.com/opentofu/opentofu/releases/download/")
    assert len(release.sha256) == 64


@pytest.mark.parametrize(
    ("make", "missing"),
    [
        (lambda pins_file: None, "is missing"),
        (lambda pins_file: pins_file.write_text("[versions\n"), "is not valid TOML"),
        (lambda pins_file: pins_file.mkdir(), "cannot be read"),
        (lambda pins_file: pins_file.write_bytes(b"\xff\xfe"), "cannot be read"),
    ],
    ids=["no-pins-file", "malformed-pins-file", "pins-file-is-a-directory", "pins-file-not-utf8"],
)
def test_a_pins_file_it_cannot_parse_fails_naming_it(tmp_path, make, missing):
    """#101 rule 1: a missing, unreadable or malformed pins file fails as a TofuError naming it."""
    pins_file = tmp_path / "opentofu.toml"
    make(pins_file)

    with pytest.raises(TofuError) as raised:
        tofu_binary.read_pins(pins_file)

    assert str(pins_file) in str(raised.value)
    assert missing in str(raised.value)


class RedirectsToPlainHttp(urllib.request.HTTPSHandler):
    """The injected HTTPS transport: answers every request with a 302 to plain http."""

    location = "http://downloads.invalid/tofu.zip"

    def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        headers = email.message.Message()
        headers["Location"] = self.location
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        response.msg = "Found"
        return response


def test_a_redirect_off_https_names_both_urls_and_the_expected_sha(root):
    """#101 rule 2 Failure: a redirect to plain http names both URLs and the expected sha."""
    opener = tofu_binary.https_opener(RedirectsToPlainHttp())
    fetch = functools.partial(tofu_binary.fetch_https, opener=opener)
    release = release_of(pinned_zip())

    with pytest.raises(TofuError) as raised:
        tofu_binary.install(root, release, fetch)

    assert_names_the_url_and_both_shas(str(raised.value), release.sha256, b"")
    assert RedirectsToPlainHttp.location in str(raised.value)
    assert tree(root) == set()
