"""The `docker` and `ss` the exposure scan runs, as shell shims a test puts first on PATH."""

import shlex
from pathlib import Path


def write_host_shims(directory: Path, listeners: str, docker_script: str) -> None:
    """The `docker` and `ss` the exposure scan runs: no container and no listener unless a test
    says so."""
    directory.mkdir(exist_ok=True)
    for name, script in {
        "docker": docker_script,
        "ss": f"printf %s {shlex.quote(listeners)}",
    }.items():
        shim = directory / name
        shim.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        shim.chmod(0o755)
