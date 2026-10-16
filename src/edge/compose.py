"""Docker Compose for `edge apply`: what the stack is made of and what is running of it.

The pure part maps the profiles of a resolved or parsed compose file to their services. The
`Compose` class is the one place that runs `docker compose`; it never runs `down` and never
names a volume, so a unit switched off loses containers and keeps its data.
"""

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
READ_SECONDS = 60
CHANGE_SECONDS = 600
_NO_RESTART = "no"
_RUNNING = "running"

Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


class ComposeError(Exception):
    """A `docker compose` call failed or answered nonsense; the message is one line."""


def profile_services(config: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Each profile of a compose file with the services that name it, in file order."""
    services: dict[str, list[str]] = {}
    for name, service in config["services"].items():
        for profile in service.get("profiles", []):
            services.setdefault(profile, []).append(name)
    return {profile: tuple(names) for profile, names in services.items()}


def is_long_running(service: Mapping[str, Any]) -> bool:
    """A service that restarts is meant to stay up; the others (restart "no") run once."""
    return service.get("restart", _NO_RESTART) != _NO_RESTART


def named_volumes(service: Mapping[str, Any]) -> tuple[str, ...]:
    """The compose volume keys a resolved service mounts."""
    return tuple(
        mount["source"] for mount in service.get("volumes", []) if mount.get("type") == "volume"
    )


def run_docker(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout, cwd=REPOSITORY
        )
    except subprocess.TimeoutExpired as error:
        raise ComposeError(f"`{' '.join(command[:3])}` took longer than {timeout}s") from error
    except OSError as error:
        raise ComposeError(f"cannot run {command[0]}: {error.strerror}") from error


@dataclass(frozen=True)
class Compose:
    run: Runner = run_docker
    repository: Path = REPOSITORY

    def resolved_configuration(self) -> dict[str, Any]:
        """The configuration of every profile, as Compose resolves it."""
        output = self._call("*", "config", ["--format", "json"], READ_SECONDS)
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise ComposeError("`docker compose config` did not answer JSON") from error

    def states(self) -> dict[str, set[str]]:
        """Each service that has a container, with the states of its containers."""
        output = self._call("*", "ps", ["--all", "--format", "json"], READ_SECONDS)
        states: dict[str, set[str]] = {}
        for container in _containers(output):
            states.setdefault(container["Service"], set()).add(container["State"])
        return states

    def start(self, unit: str, services: tuple[str, ...]) -> None:
        self._call(unit, "up", ["-d", *services], CHANGE_SECONDS)

    def stop(self, unit: str, services: tuple[str, ...]) -> None:
        self._call(unit, "stop", list(services), CHANGE_SECONDS)

    def remove_containers(self, unit: str, services: tuple[str, ...]) -> None:
        self._call(unit, "rm", ["-f", *services], CHANGE_SECONDS)

    def _call(self, profile: str, verb: str, arguments: list[str], timeout: int) -> str:
        prefix = [
            "docker", "compose", "--project-directory", str(self.repository),
            "--file", str(self.repository / "compose.yaml"),
        ]  # fmt: skip
        result = self.run([*prefix, "--profile", profile, verb, *arguments], timeout)
        if result.returncode != 0:
            raise ComposeError(_failure_line(verb, result))
        return result.stdout


def is_up(
    states: Mapping[str, set[str]], config: Mapping[str, Any], services: tuple[str, ...]
) -> bool:
    """Every long-running service of the unit has only running containers."""
    return all(
        states.get(name) == {_RUNNING}
        for name in services
        if is_long_running(config["services"][name])
    )


def is_gone(states: Mapping[str, set[str]], services: tuple[str, ...]) -> bool:
    """No container of the unit's services exists, running or not."""
    return not any(name in states for name in services)


def _containers(output: str) -> list[dict[str, str]]:
    """`ps --format json` is one JSON array or one object per line, by Compose version."""
    text = output.strip()
    try:
        if text.startswith("["):
            return json.loads(text)
        return [json.loads(line) for line in text.splitlines()]
    except json.JSONDecodeError as error:
        raise ComposeError("`docker compose ps` did not answer JSON") from error


def _failure_line(verb: str, result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
    last = lines[-1].strip() if lines else "no output"
    return f"`docker compose {verb}` exited {result.returncode}: {last}"
