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
        (lambda engine: engine.stop(), ["stop"]),
        (lambda engine: engine.remove(), ["rm", "-f"]),
    ],
    ids=[
        "build", "pull", "create never builds or pulls", "start", "ps is file-free",
        "stop is file-free", "rm -f is file-free, never down and never -v",
    ],
)  # fmt: skip
def test_each_step_is_one_call_of_project_key_and_only_ps_and_off_are_file_free(step, expected):
    docker = Docker(stdout="[]")

    step(engine_over(docker))

    assert docker.commands == [["docker", "compose", "-p", KEY, *expected]]


def test_the_containers_carry_their_state_and_health():
    entries = [
        {
            "Name": "hopin-hopin-1",
            "ID": "a1",
            "Service": "hopin",
            "State": "running",
            "Health": "healthy",
        },
        {"Name": "hopin-db-1", "ID": "a2", "Service": "db", "State": "created", "Health": ""},
        {
            "Name": "hopin-old-1",
            "ID": "a3",
            "Service": "old",
            "State": "exited",
            "Health": "healthy",
        },
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
        lambda engine: engine.image_volumes(["web"], TAG),
        lambda engine: engine.stop(),
        lambda engine: engine.remove(),
        lambda engine: engine.volumes(),
    ],
    ids=["build", "pull", "create", "start", "ps", "config", "stop", "rm", "volume ls"],
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


def test_the_volumes_are_the_keys_docker_labels_for_the_project():
    docker = Docker(stdout="data\n\ncache\n")

    assert engine_over(docker).volumes() == ["data", "cache"]
    assert docker.commands == [
        ["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={KEY}",
         "--format", '{{.Label "com.docker.compose.volume"}}']
    ]  # fmt: skip


class ImageDocker(Docker):
    """Answers `config --format json` with `services` and `image inspect` with what the image
    declares in `by_image`, else with `inspected`. The services chain through `depends_on` across
    the three shapes: image only, build only, build plus image."""

    def __init__(
        self,
        inspected: str = "null",
        inspect_exit_code: int = 0,
        by_image: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.inspected, self.inspect_exit_code = inspected, inspect_exit_code
        self.by_image = by_image or {}
        self.services = {
            "web": {"build": {"context": "."}, "depends_on": {"app": {"condition": "started"}}},
            "app": {
                "build": {"context": "app"},
                "image": "registry.example/app:1",
                "depends_on": {"db": {"condition": "started"}},
            },
            "db": {"image": "postgres:17"},
        }

    def run(self, command, _timeout, _stdin=None):
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            answer = self.by_image.get(command[-1], self.inspected)
            return subprocess.CompletedProcess(command, self.inspect_exit_code, answer, "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": self.services}), "")


def test_each_service_of_a_dependency_chain_reports_the_volumes_of_its_own_image():
    docker = ImageDocker(
        by_image={
            f"{KEY}-web": '{"/built":{}}',
            "registry.example/app:1": '{"/tagged":{}}',
            "postgres:17": '{"/var/lib/postgresql/data":{}}',
        }
    )

    declared = engine_over(docker).image_volumes(["web", "app", "db"], TAG)

    assert declared == {
        "web": ["/built"],
        "app": ["/tagged"],
        "db": ["/var/lib/postgresql/data"],
    }
    assert [command[-1] for command in docker.commands[1:]] == [
        f"{KEY}-web",
        "registry.example/app:1",
        "postgres:17",
    ]


def test_a_built_image_is_named_as_compose_names_it_without_compatibility_mode(monkeypatch):
    monkeypatch.setenv("COMPOSE_COMPATIBILITY", "true")

    result = compose.run_docker(
        ["sh", "-c", 'printf %s "$COMPOSE_COMPATIBILITY"'], compose.READ_SECONDS
    )

    assert result.stdout == "false"


def test_a_service_the_config_names_no_image_for_is_one_line():
    engine = engine_over(ImageDocker("null"))

    with pytest.raises(compose.ComposeError, match="names no image of a service"):
        engine.image_volumes(["worker"], TAG)


def test_an_image_without_volumes_declares_none():
    assert engine_over(ImageDocker("null")).image_volumes(["web"], TAG) == {"web": []}


@pytest.mark.parametrize(("inspected", "exit_code"), [("not json", 0), (SENTINEL, 1)])
def test_an_image_inspection_that_fails_or_answers_no_json_is_one_line_without_its_content(
    inspected, exit_code
):
    engine = engine_over(ImageDocker(inspected, exit_code))

    with pytest.raises(compose.ComposeError) as failed:
        engine.image_volumes(["web"], TAG)

    assert inspected not in str(failed.value)
    assert f"for {KEY} at {TAG}" in str(failed.value)
