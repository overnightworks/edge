"""The docker calls of an app: their shape, and that none of Compose's words reaches a message
(#255 A18, E7)."""

import json
import subprocess
from pathlib import Path

import pytest
from app_support import KEY, SENTINEL

from edge import app_compose, compose

DIRECTORY = Path("/home/test/srv") / KEY
TAG = "v2026.10.22.1"


class Docker:
    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.commands: list[list[str]] = []
        self.exit_code, self.stdout = exit_code, stdout

    def run(
        self, command: list[str], _timeout: int, _stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.exit_code, self.stdout, SENTINEL)


def engine_over(docker: Docker) -> app_compose.AppCompose:
    return app_compose.AppCompose(KEY, DIRECTORY, docker.run)


FILE_BOUND = ["--project-directory", str(DIRECTORY), "--file", str(DIRECTORY / "compose.yaml")]


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (lambda engine: engine.build(TAG), [*FILE_BOUND, "build"]),
        (lambda engine: engine.pull(TAG), [*FILE_BOUND, "pull", "--ignore-buildable"]),
        (
            lambda engine: engine.create(TAG),
            [*FILE_BOUND, "create", "--no-build", "--pull", "never", "--remove-orphans"],
        ),
        (lambda engine: engine.start(TAG), [*FILE_BOUND, "start"]),
        (lambda engine: engine.containers(), ["ps", "--all", "--format", "json"]),
    ],
    ids=["build", "pull", "create never builds or pulls", "start", "ps is file-free"],
)
def test_each_step_is_one_call_of_project_key_and_only_ps_is_file_free(step, expected):
    docker = Docker(stdout="[]")

    step(engine_over(docker))

    assert docker.commands == [["docker", "compose", "-p", KEY, *expected]]


def test_the_containers_carry_their_state_and_health():
    entries = [
        {"Name": "hopin-hopin-1", "Service": "hopin", "State": "running", "Health": "healthy"},
        {"Name": "hopin-db-1", "Service": "db", "State": "created", "Health": ""},
        {"Name": "hopin-old-1", "Service": "old", "State": "exited", "Health": "healthy"},
    ]
    docker = Docker(stdout="\n".join(json.dumps(entry) for entry in entries))

    containers = engine_over(docker).containers()

    assert [container.describe() for container in containers] == [
        "hopin running (healthy)",
        "db created",
        "old exited (healthy)",
    ]
    assert [container.healthy for container in containers] == [True, False, False]


@pytest.mark.parametrize("answer", ['[{"Service": "x"}]', "[1, 2]", '"text"'])
def test_a_ps_answer_that_lists_no_containers_is_one_line_without_its_content(answer):
    engine = engine_over(Docker(stdout=answer))

    with pytest.raises(compose.ComposeError) as failed:
        engine.containers()

    assert str(failed.value) == f"`docker compose ps` of {KEY} answered no list of containers"


@pytest.mark.parametrize(
    "step",
    [
        lambda engine: engine.build(TAG),
        lambda engine: engine.pull(TAG),
        lambda engine: engine.create(TAG),
        lambda engine: engine.start(TAG),
        lambda engine: engine.containers(),
    ],
    ids=["build", "pull", "create", "start", "ps"],
)
def test_a_failing_call_names_step_app_and_exit_and_never_composes_output(step):
    docker = Docker(exit_code=17, stdout=SENTINEL)
    engine = engine_over(docker)

    with pytest.raises(compose.ComposeError) as failed:
        step(engine)

    line = str(failed.value)
    assert SENTINEL not in line
    assert f"of {KEY}" in line
    assert "exited 17" in line
    assert "output is withheld" in line
