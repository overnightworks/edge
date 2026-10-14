"""The only place that touches the host: two read-only commands, fixed argv, no shell."""

import subprocess

DOCKER_CONTAINER_IDS = ("docker", "ps", "--all", "--quiet", "--no-trunc")
DOCKER_INSPECT = ("docker", "inspect")
SOCKET_LISTENERS = ("ss", "-H", "-ltnupe")


class HostCommandError(RuntimeError):
    """A host command could not run, so the check cannot vouch for anything."""


def docker_inspect_json() -> str:
    container_ids = _run(DOCKER_CONTAINER_IDS).split()
    return _run((*DOCKER_INSPECT, *container_ids)) if container_ids else "[]"


def socket_listeners() -> str:
    return _run(SOCKET_LISTENERS)


def _run(argv: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise HostCommandError(f"{argv[0]} is not installed") from error
    except subprocess.CalledProcessError as error:
        raise HostCommandError(
            f"{' '.join(argv[:2])} failed with exit {error.returncode}: {error.stderr.strip()}"
        ) from error
    return completed.stdout
