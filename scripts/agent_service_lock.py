"""Writes `agent-service/image/requirements.txt`, the one hashed lock of the agent service image.

`scripts/agent_service_lock.py <tag>` takes agent-providers' release tag (for example `v0.4.0`),
exports the service's runtime closure from that tag's `uv.lock`, and adds the release's three
wheels by URL and sha256, computed from the downloaded bytes. The file's header names the tag and
its commit. A release bump runs this script again; nothing else writes the file.
"""

from __future__ import annotations

import argparse
import hashlib
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
UV_HEADER_LINES: Final = 2
RELEASE_WHEEL_STEMS: Final = (
    "overnightworks_agent_client",
    "overnightworks_agent_providers",
    "overnightworks_agent_service",
)
LOCK_PATH: Final = Path(__file__).resolve().parent.parent / "agent-service/image/requirements.txt"
DOWNLOAD_SECONDS: Final = 120


def release_wheel_requirements(tag: str) -> list[str]:
    """One pinned requirement per release wheel, hashed from the bytes the release serves."""
    version = tag.removeprefix("v")
    requirements = []
    for stem in RELEASE_WHEEL_STEMS:
        wheel = f"{stem}-{version}-py3-none-any.whl"
        url = f"{REPOSITORY_URL}/releases/download/{tag}/{wheel}"
        with urllib.request.urlopen(url, timeout=DOWNLOAD_SECONDS) as response:
            digest = hashlib.sha256(response.read()).hexdigest()
        requirements.append(f"{stem.replace('_', '-')} @ {url} \\\n    --hash=sha256:{digest}\n")
    return requirements


def exported_closure(checkout: Path) -> str:
    """The service's runtime closure with hashes, without uv's own header comment."""
    exported = subprocess.run(
        UV_EXPORT, cwd=checkout, check=True, capture_output=True, text=True
    ).stdout
    return "".join(exported.splitlines(keepends=True)[UV_HEADER_LINES:])


def lock_text(tag: str) -> str:
    with tempfile.TemporaryDirectory() as scratch:
        checkout = Path(scratch)
        subprocess.run(
            [*GIT_CLONE_TAG, tag, REPOSITORY_URL, scratch],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", scratch, "rev-parse", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        closure = exported_closure(checkout)
    header = (
        f"# agent-providers {tag}, commit {commit}\n"
        f"# {' '.join(UV_EXPORT)}, then the release's wheels.\n"
        "# Written only by scripts/agent_service_lock.py; do not edit.\n"
    )
    return header + closure + "".join(release_wheel_requirements(tag))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_service_lock", description=__doc__)
    parser.add_argument("tag", help="agent-providers release tag, such as v0.4.0")
    arguments = parser.parse_args(argv)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(lock_text(arguments.tag), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
