"""Docker Compose for `edge apply`: what the stack is made of and what is running of it.

The pure part maps the profiles of a resolved or parsed compose file to their services. The
`Compose` class is the one place that runs `docker compose`; it never runs `down` and never
names a volume, so a unit switched off loses containers and keeps its data.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
READ_SECONDS = 60
CHANGE_SECONDS = 600
# A container that cannot load its configuration is running for a moment after `up`, then
# exits and Docker restarts it with a backoff that starts at 100 ms; seconds of watching see it.
SETTLE_SECONDS = 5.0
_SETTLE_POLL_SECONDS = 0.5
_COMPLETES = "service_completed_successfully"
_RUNNING = "running"
_COMPLETED = "completed"  # a container that exited 0: a one-shot has done its job
_NOT_RUNNING = {"exited", _COMPLETED, "created", "dead"}
# Compose's record of the service definition a container was created from. Only this label is
# read out of ps's comma-joined `Labels`, so another label's value may hold commas.
_CONFIG_HASH_LABEL = re.compile(r"(?:^|,)com\.docker\.compose\.config-hash=([0-9a-f]{64})(?:,|$)")
_HASH_LINE = re.compile(r"(\S+) ([0-9a-f]{64})")

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


def one_shot_services(config: Mapping[str, Any]) -> set[str]:
    """Services another service waits on to complete; only these may be done after exiting 0."""
    return {
        name
        for service in config["services"].values()
        for name, dependency in service.get("depends_on", {}).items()
        if dependency.get("condition") == _COMPLETES
    }


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
class Watch:
    """How long a started container is watched before it counts as running."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    seconds: float = SETTLE_SECONDS


SETTLED = Watch()


@dataclass(frozen=True)
class Container:
    """One container of the project as `ps` lists it. `config_hash` is the hash of the service
    definition it was created from, as Compose labelled it; None when the label is missing."""

    service: str
    state: str
    config_hash: str | None
    image: str


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

    def containers(self) -> list[Container]:
        """Every container of the project, of every profile."""
        output = self._call("*", "ps", ["--all", "--format", "json"], READ_SECONDS)
        return [_container(entry) for entry in _containers(output)]

    def states(self) -> dict[str, set[str]]:
        """Each service that has a container, with the states of its containers."""
        return states_of(self.containers())

    def config_hashes(self, services: tuple[str, ...]) -> dict[str, str]:
        """Compose's hash of each service's definition as it resolves the checkout now; the
        label of a container created from the same definition carries the same hash."""
        output = self._call("*", "config", ["--hash", ",".join(services)], READ_SECONDS)
        hashes = {}
        for line in filter(str.strip, output.splitlines()):
            if not (match := _HASH_LINE.fullmatch(line.strip())):
                raise ComposeError(f"`docker compose config --hash` answered {line.strip()!r}")
            hashes[match[1]] = match[2]
        if missing := [service for service in services if service not in hashes]:
            raise ComposeError(
                f"`docker compose config --hash` named no hash for {', '.join(missing)}"
            )
        return hashes

    def start(self, unit: str, services: tuple[str, ...]) -> None:
        self._call(unit, "up", ["-d", "--no-recreate", *services], CHANGE_SECONDS)

    def recreate(self, service: str) -> None:
        """A new container for one service, so it mounts its files as checked out now; the
        services it depends on are left as they are, and its volumes are kept."""
        self._call("*", "up", ["-d", "--no-deps", "--force-recreate", service], CHANGE_SECONDS)

    def stays_up(self, config: Mapping[str, Any], services: tuple[str, ...], watch: Watch) -> bool:
        """The services are up at every look until the watch is over: a container that dies
        right after it started and is restarting is not running."""
        deadline = watch.clock() + watch.seconds
        while is_up(self.states(), config, services):
            if watch.clock() >= deadline:
                return True
            watch.sleep(_SETTLE_POLL_SECONDS)
        return False

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
    """Every service of the unit has a container: running, or for a one-shot also completed."""
    one_shots = one_shot_services(config)
    return all(
        states.get(name, set())
        and states[name] <= ({_RUNNING, _COMPLETED} if name in one_shots else {_RUNNING})
        for name in services
    )


def has_running_container(states: Mapping[str, set[str]], services: tuple[str, ...]) -> bool:
    """Some container of the unit's services is alive: running, restarting or paused."""
    return any(states.get(name, set()) - _NOT_RUNNING for name in services)


def states_of(containers: list[Container]) -> dict[str, set[str]]:
    """Each service that has a container, with the states of its containers."""
    states: dict[str, set[str]] = {}
    for container in containers:
        states.setdefault(container.service, set()).add(container.state)
    return states


def _container(entry: Mapping[str, Any]) -> Container:
    label = _CONFIG_HASH_LABEL.search(entry.get("Labels", ""))
    return Container(
        entry["Service"], _state_of(entry), label[1] if label else None, entry.get("Image", "")
    )


def _state_of(container: Mapping[str, Any]) -> str:
    if container["State"] == "exited" and container.get("ExitCode") == 0:
        return _COMPLETED
    return container["State"]


def is_gone(states: Mapping[str, set[str]], services: tuple[str, ...]) -> bool:
    """No container of the unit's services exists, running or not."""
    return not any(name in states for name in services)


def _containers(output: str) -> list[dict[str, Any]]:
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
