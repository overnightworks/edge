"""Writes `agent-service/image/requirements.txt`, the one hashed lock of the agent service image.

`scripts/agent_service_lock.py <tag>` takes agent-providers' release tag (for example `v0.4.0`),
exports the service's runtime closure from that tag's `uv.lock`, and adds the release's three
wheels by URL and sha256, computed from the downloaded bytes. The file's header names the tag and
its commit. A release bump runs this script again; nothing else writes the file.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Final

REPOSITORY_URL: Final = "https://github.com/overnightworks/agent-providers"
UV_EXPORT: Final = (
    "uv",
    "export",
    "--no-header",
    "--frozen",
    "--no-dev",
    "--no-emit-workspace",
    "--package",
    "overnightworks-agent-service",
)
GIT_CLONE_TAG: Final = (
    *("git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1"),
    "--branch",
)
RELEASE_TAG: Final = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", re.ASCII)
RELEASE_WHEEL_STEMS: Final = (
    "overnightworks_agent_client",
    "overnightworks_agent_providers",
    "overnightworks_agent_service",
)
LOCK_PATH: Final = Path(__file__).resolve().parent.parent / "agent-service/image/requirements.txt"
DOWNLOAD_SECONDS: Final = 120


def release_tag(text: str) -> str:
    """The only shape of tag that reaches `git clone` and the release URLs."""
    if not RELEASE_TAG.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a release tag such as v0.4.0")
    return text


def release_wheel_urls(tag: str) -> dict[str, str]:
    """The release's wheel URL by requirement name, in lock order."""
    version = tag.removeprefix("v")
    return {
        stem.replace("_", "-"): (
            f"{REPOSITORY_URL}/releases/download/{tag}/{stem}-{version}-py3-none-any.whl"
        )
        for stem in RELEASE_WHEEL_STEMS
    }


def lock_text(tag: str, commit: str, closure: str, wheel_digests: dict[str, str]) -> str:
    """The whole lock: a header naming tag and commit, the closure, then the wheels by URL and
    sha256. `wheel_digests` holds one sha256 per requirement name of `release_wheel_urls`."""
    header = (
        f"# agent-providers {tag}, commit {commit}\n"
        f"# {' '.join(UV_EXPORT)}, then the release's wheels.\n"
        "# Written only by scripts/agent_service_lock.py; do not edit.\n"
    )
    wheels = "".join(
        f"{name} @ {url} \\\n    --hash=sha256:{wheel_digests[name]}\n"
        for name, url in release_wheel_urls(tag).items()
    )
    return header + closure + wheels


def wheel_digest(url: str) -> str:
    """The sha256 of the bytes the URL serves."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_SECONDS) as response:
        return hashlib.sha256(response.read()).hexdigest()


def cloned_commit(repository_url: str, tag: str, destination: Path) -> str:
    """Clones the tag shallowly into `destination` and returns the commit it points at."""
    subprocess.run(
        [*GIT_CLONE_TAG, release_tag(tag), "--", repository_url, str(destination)], check=True
    )
    return subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def exported_closure(checkout: Path) -> str:
    """The service's runtime closure with hashes."""
    return subprocess.run(
        UV_EXPORT, cwd=checkout, check=True, capture_output=True, text=True
    ).stdout


def built_lock(tag: str) -> str:
    with tempfile.TemporaryDirectory() as scratch:
        checkout = Path(scratch)
        commit = cloned_commit(REPOSITORY_URL, tag, checkout)
        closure = exported_closure(checkout)
    digests = {name: wheel_digest(url) for name, url in release_wheel_urls(tag).items()}
    return lock_text(tag, commit, closure, digests)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_service_lock", description=__doc__)
    parser.add_argument("tag", type=release_tag, help="agent-providers release tag, such as v0.4.0")
    arguments = parser.parse_args(argv)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(built_lock(arguments.tag), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
