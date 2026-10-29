"""What the tests of the Keycloak probe share: the probe's script as a module and the docker
executable the probe's host runs on."""

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from support.script_module import loaded_script

PROBE = Path(__file__).resolve().parents[2] / "probe" / "keycloak" / "probe.py"
FAKE_DOCKER = Path(__file__).with_name("fake_docker.py")


@pytest.fixture(scope="module")
def probe() -> Iterator[ModuleType]:
    yield from loaded_script("keycloak_probe", PROBE)


class DockerHost:
    """The host the probe runs on: a docker executable on PATH over a state file."""

    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state_file = directory / "docker-state.json"
        self.configure(containers=[], one_offs=[])
        executables = directory / "bin"
        executables.mkdir()
        docker = executables / "docker"
        docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_DOCKER}" "$@"\n')
        docker.chmod(0o755)
        monkeypatch.setenv("PATH", f"{executables}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(self.state_file))
        # The compose environment names the docker group, which a host without Docker lacks.
        monkeypatch.setenv("DOCKER_GID", "999")

    def configure(self, **changes: object) -> None:
        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.state_file.write_text(json.dumps({**state, **changes}))

    @property
    def containers(self) -> set[str]:
        return set(json.loads(self.state_file.read_text())["containers"])


@pytest.fixture
def docker_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DockerHost:
    return DockerHost(tmp_path, monkeypatch)
