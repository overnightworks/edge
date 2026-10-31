"""The move of the stack between the Compose projects `edge` and `hal`: the reads, the plan and
the Docker calls of `hal migrate stack`.

The source is whichever of the two the resolved `compose.yaml` does not name. Everything is read
from Docker, so a rerun finishes what is left. Volumes are never removed by `down`: only the
source's volumes `compose.yaml` no longer names are removed, one by one, by name.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from hal.stack import compose
from hal.stack.compose import Compose, ComposeError, Runner

PROJECTS = ("edge", "hal")
PROJECT_LABEL = "com.docker.compose.project"
_TIMEOUT_SECONDS = compose.CHANGE_SECONDS


class Refusal(Exception):
    """`compose.yaml` names neither project; the message is one line."""


class Stage(Enum):
    DOWN = "down"
    VOLUME = "volume"
    START = "start"


@dataclass(frozen=True)
class Plan:
    """What the move does. Networks are named for the person; `down` removes them."""

    source: str
    target: str
    containers: tuple[str, ...]
    networks: tuple[str, ...]
    kept_volumes: tuple[str, ...]
    removed_volumes: tuple[str, ...]
    core: tuple[str, ...]  # the services to start; empty while the target's core is up

    @property
    def has_work(self) -> bool:
        return bool(self.containers or self.removed_volumes or self.core)


def make_plan(docker: Runner) -> Plan:
    cluster = Compose(run=docker)
    config = cluster.resolved_configuration()
    target = config["name"]
    if target not in PROJECTS:
        raise Refusal(f"compose.yaml names the project {target}, not edge or hal")
    source = next(project for project in PROJECTS if project != target)
    resolved = {volume["name"] for volume in config.get("volumes", {}).values()}
    volumes = _labelled(docker, ["volume", "ls"], "{{.Name}}", source)
    core = tuple(name for name, service in config["services"].items() if "profiles" not in service)
    return Plan(
        source,
        target,
        _labelled(docker, ["ps", "--all"], "{{.Names}}", source),
        _labelled(docker, ["network", "ls"], "{{.Name}}", source),
        tuple(name for name in volumes if name in resolved),
        tuple(name for name in volumes if name not in resolved),
        () if compose.is_up(cluster.states(), config, core) else core,
    )


def move(plan: Plan, docker: Runner) -> Iterator[tuple[Stage, tuple[str, ...]]]:
    """Each stage of the plan, yielded once it is done; a failing call raises `ComposeError`
    before the next stage."""
    if plan.containers:
        _call(docker, _down(plan.source), "docker compose down")
        yield Stage.DOWN, plan.containers
    for volume in plan.removed_volumes:
        _call(docker, ["docker", "volume", "rm", volume], f"docker volume rm {volume}")
        yield Stage.VOLUME, (volume,)
    if plan.core:
        Compose(run=docker).start("*", plan.core)
        yield Stage.START, plan.core


def _down(source: str) -> list[str]:
    repository = compose.REPOSITORY
    return [
        "docker", "compose", "-p", source, "--project-directory", str(repository),
        "--file", str(repository / "compose.yaml"), "--profile", "*", "down",
    ]  # fmt: skip


def _labelled(docker: Runner, listing: list[str], name_format: str, source: str) -> tuple[str, ...]:
    label = f"label={PROJECT_LABEL}={source}"
    command = ["docker", *listing, "--filter", label, "--format", name_format]
    return tuple(_call(docker, command, f"docker {listing[0]}").split())


def _call(docker: Runner, command: list[str], name: str) -> str:
    result = docker(command, _TIMEOUT_SECONDS)
    if result.returncode != 0:
        output = result.stderr + result.stdout
        raise ComposeError(f"`{name}` exited {result.returncode}: {compose.last_line(output)}")
    return result.stdout
