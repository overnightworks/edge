"""The only place that touches the host: two read-only commands, fixed argv, no shell."""

import json
import subprocess

from edge_check.exposure import UnreadableOutputError

DOCKER_CONTAINER_IDS = ("docker", "ps", "--all", "--quiet", "--no-trunc")
DOCKER_INSPECT = ("docker", "inspect")
SOCKET_LISTENERS = ("ss", "-H", "-ltnupe")
HOST_COMMAND_TIMEOUT_SECONDS = 30


class HostCommandError(RuntimeError):
    """A host command could not run, so the check cannot vouch for anything."""


def docker_inspect_json() -> str:
    container_ids = _run(DOCKER_CONTAINER_IDS).split()
    if not container_ids:
        return "[]"
    inspect_json = _run((*DOCKER_INSPECT, *container_ids))
    _require_every_container_inspected(inspect_json, len(container_ids))
    return inspect_json


def socket_listeners() -> str:
    return _run(SOCKET_LISTENERS)


def _require_every_container_inspected(inspect_json: str, listed: int) -> None:
    """A short answer would make the check vouch for containers it never saw."""
    try:
        answer = json.loads(inspect_json)
    except json.JSONDecodeError:
        return  # the parser names what is wrong with it
    if isinstance(answer, list) and len(answer) < listed:
        raise UnreadableOutputError(
            f"docker inspect answered {len(answer)} containers, docker ps listed {listed}"
        )


def _run(argv: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=True,
            timeout=HOST_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise HostCommandError(
            f"{' '.join(argv[:2])} did not answer within {HOST_COMMAND_TIMEOUT_SECONDS:g} s"
        ) from error
    except FileNotFoundError as error:
        raise HostCommandError(f"{argv[0]} is not installed") from error
    except subprocess.CalledProcessError as error:
        raise HostCommandError(
            f"{' '.join(argv[:2])} failed with exit {error.returncode}: {error.stderr.strip()}"
        ) from error
    return completed.stdout
