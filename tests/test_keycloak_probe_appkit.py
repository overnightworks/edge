"""The Keycloak probe writes the appkit files the themes read before its stack comes up (#532)."""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from test_keycloak_probe import (
    DockerHost,
    docker_host,  # noqa: F401 the fixture, requested by name below
    probe,  # noqa: F401 the fixture, requested by name below
)


def stack_that_records(
    module: ModuleType,
    scratch: Path,
    steps: list[str],
    write_appkit: Callable[[], None] | None = None,
) -> object:
    stack = module.Stack(scratch, write_appkit=write_appkit or (lambda: steps.append("write")))

    def compose(*arguments: str, **_: object) -> subprocess.CompletedProcess[str]:
        steps.append(f"compose {arguments[0]}")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    stack.compose = compose
    return stack


def test_the_stack_writes_the_appkit_files_before_compose_up(
    probe: ModuleType,  # noqa: F811
    tmp_path: Path,
    docker_host: DockerHost,  # noqa: F811
) -> None:
    steps: list[str] = []

    stack_that_records(probe, tmp_path, steps).up("keycloak")

    assert steps == ["write", "compose up"]


def test_a_failing_write_stops_the_stack_before_compose_up_loudly(
    probe: ModuleType,  # noqa: F811
    tmp_path: Path,
    docker_host: DockerHost,  # noqa: F811
) -> None:
    steps: list[str] = []

    def failing() -> None:
        steps.append("write")
        raise probe.ProbeFailure("appkit: could not write the theme")

    with pytest.raises(probe.ProbeFailure, match="could not write the theme"):
        stack_that_records(probe, tmp_path, steps, failing).up("keycloak")

    assert steps == ["write"]


@pytest.mark.parametrize(
    ("exit_code", "outcome"), [(0, "written"), (2, "appkit step failed")], ids=["ok", "refused"]
)
def test_the_default_write_runs_the_step_through_the_repositorys_own_uv_run(
    probe: ModuleType,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    outcome: str,
) -> None:
    """The probe's own interpreter may lack appkit (webauth's has none); `uv run` has it."""
    log = tmp_path / "uv-arguments"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/sh\necho "$@" > "{log}"\necho "refused" >&2\nexit {exit_code}\n')
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{uv.parent}{os.pathsep}{os.environ['PATH']}")

    if exit_code:
        with pytest.raises(probe.ProbeFailure, match=outcome):
            probe.write_appkit_files()
    else:
        probe.write_appkit_files()

    assert log.read_text().split() == [
        "run",
        "--project",
        str(probe.REPO),
        "python",
        "-m",
        "edge.appkit_step",
        str(probe.REPO),
    ]
