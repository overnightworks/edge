"""A docker executable for the Keycloak probe's tests, answering over a JSON state file.

The state file named by FAKE_DOCKER_STATE holds the containers of the probe project and how the
host misbehaves; every invocation reads it, acts like the real CLI would and writes it back.
"""

import json
import os
import signal
import sys
from pathlib import Path

SERVICE_CONTAINER = "edge-kc-probe-keycloak-1"
RUNNING_APPLY = "edge-kc-probe-apply-0a1b2c3d"
LATE_APPLY = "edge-kc-probe-apply-4e5f6a7b"
DAEMON_GONE = "Cannot connect to the Docker daemon"
ONE_OFF_LABEL = "label=com.docker.compose.oneoff=True"


def compose_verb(arguments: list[str]) -> tuple[str, list[str]]:
    """The compose subcommand and its arguments, after the project and --file options."""
    rest = arguments[1:]
    while rest[0].startswith("--"):
        rest = rest[2:]
    return rest[0], rest[1:]


def compose(state: dict, arguments: list[str]) -> tuple[int, str]:
    verb, options = compose_verb(arguments)
    if verb == "up":
        state["containers"].append(SERVICE_CONTAINER)
    elif verb == "run":
        container = options[options.index("--name") + 1]
        state["containers"].append(container)
        state["one_offs"].append(container)
        signalled, state["signal_parent_on_run"] = state.get("signal_parent_on_run"), None
        if signalled:
            os.kill(os.getppid(), getattr(signal, signalled))
    elif verb == "ps":
        return 0, SERVICE_CONTAINER
    elif verb == "down":
        # Like Compose, down leaves a running one-off container alone.
        remove(state, [SERVICE_CONTAINER])
        if state.get("apply_lands_during_down"):
            state["containers"].append(LATE_APPLY)
            state["one_offs"].append(LATE_APPLY)
    return 0, ""


def remove(state: dict, names: list[str]) -> None:
    state["containers"] = [name for name in state["containers"] if name not in names]


def list_containers(state: dict, arguments: list[str]) -> str:
    listed = state["containers"]
    if ONE_OFF_LABEL in arguments:
        listed = [name for name in listed if name in state["one_offs"]]
    return "\n".join(sorted(set(listed)))


def answer(state: dict, arguments: list[str]) -> tuple[int, str]:
    command = arguments[0]
    if command == "compose":
        return compose(state, arguments)
    if command == "inspect":
        return 0, f"/{arguments[-1]} []"
    if command == "rm":
        remove(state, arguments[1:])
        return 0, ""
    if command == "container" and arguments[1] == "rm":
        remove(state, arguments[3:])
        return 0, ""
    if arguments[:2] == [state.get("unlistable_kind"), "ls"]:
        print(DAEMON_GONE, file=sys.stderr)
        return 1, ""
    if command == "container" and arguments[1] == "ls":
        return 0, list_containers(state, arguments)
    return 0, ""


def main() -> int:
    state_file = Path(os.environ["FAKE_DOCKER_STATE"])
    state = json.loads(state_file.read_text())
    code, output = answer(state, sys.argv[1:])
    # Replaced whole: a signalled probe kills this process, which must not leave half a file.
    scratch_file = state_file.with_suffix(f".{os.getpid()}")
    scratch_file.write_text(json.dumps(state))
    scratch_file.replace(state_file)
    if output:
        print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
