"""What an app's `compose.yaml` may contain (#255, the contract for an app repository).

The one owner of that decision, pure over the text of `git show <sha>:compose.yaml`. It parses with
SafeLoader and does not interpolate, so a `${...}` in a key that is forbidden is refused as the key.
`check` runs before the operator's `yes`: a refusal means the app cannot be planned. The image half
of the volume rule needs the built image and runs after build and pull, before `create`:
`check_image_volumes`.

Every refusal is one line that names the entry, the tag, the service and the key, never a value.
A one-shot service and a `depends_on` that waits for health or completion are refused for now.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import yaml

from edge import compose
from edge_check.docker import EDGE_NAME_LABEL

_TRAEFIK_ENABLE = "traefik.enable"
_WAITING_CONDITIONS = {"service_healthy", "service_completed_successfully"}
_RESTART = "unless-stopped"


class ContractError(Exception):
    """The compose file breaks the contract; the message is one line."""


def check(text: str | None, entry: str, tag: str, hosts: Sequence[str]) -> tuple[str, ...]:
    """The services of a release's compose file when it keeps the contract for the entry's
    `hosts`; else ContractError."""

    def refuse(reason: str) -> ContractError:
        return ContractError(f"services.{entry}: compose.yaml at {tag}: {reason}")

    document = _document(text, refuse)
    services = document["services"]
    if "include" in document:
        raise refuse("uses include (include); an app is self-contained")
    if "name" in document and document["name"] != entry:
        raise refuse(f"names its project other than {entry} (name)")
    for name, definition in services.items():
        if reason := _service_refusal(definition):
            raise refuse(f"service {compose.printable(str(name))} {reason}")
    for reason in _routing_refusals(services, hosts):
        raise refuse(reason)
    return tuple(str(name) for name in services)


def check_image_volumes(
    text: str, entry: str, tag: str, declared: Mapping[str, Sequence[str]]
) -> None:
    """Raises ContractError for a volume that the image of a service declares (`declared` maps
    each service to its paths) and that no named volume, bind or tmpfs of that service covers:
    Docker would keep its data in an anonymous volume that the next release does not find."""
    for name, definition in yaml.safe_load(text)["services"].items():
        covered = _covered_paths(definition)
        for path in declared.get(str(name), ()):
            if path.rstrip("/") not in covered:
                raise ContractError(
                    f"services.{entry}: image of service {compose.printable(str(name))} at {tag} "
                    f"declares volume {compose.printable(path)}, which no named volume covers; "
                    "give it a named volume"
                )


def _document(text: str | None, refuse: Any) -> dict:
    if text is None:
        raise refuse("none at the root of the repository")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise refuse(f"cannot be read ({type(error).__name__})") from None
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        raise refuse("must be a map with services")
    return document


def _service_refusal(definition: object) -> str | None:
    """Why a service definition breaks the contract; None when it keeps it."""
    if not isinstance(definition, dict):
        return "must be a map"
    if "profiles" in definition:
        return "uses profiles (profiles); an app's services are all started"
    extends = definition.get("extends")
    if isinstance(extends, dict) and "file" in extends:
        return "extends a file (extends.file); an app is self-contained"
    return (
        _published_port_refusal(definition)
        or _dependency_refusal(definition)
        or _restart_refusal(definition)
        or _volume_refusal(definition)
    )


def _published_port_refusal(definition: dict) -> str | None:
    if "ports" in definition:
        return "publishes a port (ports); an app is reached through Traefik only"
    network_mode = definition.get("network_mode")
    if _interpolates(network_mode):
        return "interpolates network_mode ($); an app is reached through Traefik only"
    if network_mode == "host":
        return "uses the host network (network_mode); an app is reached through Traefik only"
    return None


def _dependency_refusal(definition: dict) -> str | None:
    conditions = definition.get("depends_on")
    if not isinstance(conditions, dict):
        return None
    waits = [d.get("condition") for d in conditions.values() if isinstance(d, dict)]
    if any(_interpolates(value) for value in waits):
        return "interpolates a depends_on condition ($); not yet supported"
    if any(value in _WAITING_CONDITIONS for value in waits):
        return "waits for health or completion (depends_on condition); not yet supported"
    return None


def _restart_refusal(definition: dict) -> str | None:
    restart = definition.get("restart")
    if _interpolates(restart):
        return "interpolates restart ($); not yet supported"
    if restart is False or restart == "no":
        return "is a one-shot (restart no); not yet supported"
    if restart != _RESTART:
        return f"must restart {_RESTART} (restart)"
    return None


def _volume_refusal(definition: dict) -> str | None:
    for volume in _mounts(definition):
        if isinstance(volume, str) and ":" not in volume:
            return "has a volume entry without a source (volumes); give it a named volume"
        if isinstance(volume, dict) and volume.get("type") != "tmpfs" and not volume.get("source"):
            return "has a volume entry without a source (volumes); give it a named volume"
    return None


def _routing_refusals(services: dict, hosts: Sequence[str]) -> Iterator[str]:
    """Each host has exactly one service labelled with its name and `traefik.enable: "true"`; a
    labelled service names a host of the entry and has a healthcheck."""
    holders: dict[str, list[str]] = {}
    for name, definition in services.items():
        labels = _labels(definition)
        if EDGE_NAME_LABEL not in labels:
            continue
        service = f"service {compose.printable(str(name))}"
        if labels.get(_TRAEFIK_ENABLE) != "true":
            yield f"{service} has {EDGE_NAME_LABEL} without {_TRAEFIK_ENABLE} true (labels)"
        if labels[EDGE_NAME_LABEL] not in hosts:
            yield f"{service} labels a name the entry does not list ({EDGE_NAME_LABEL})"
        holders.setdefault(labels[EDGE_NAME_LABEL], []).append(str(name))
        health = definition.get("healthcheck")
        if not isinstance(health, dict) or health.get("disable"):
            yield f"{service} has no healthcheck (healthcheck); the publish phase asks it for ready"
    for host in hosts:
        if len(holders.get(host, [])) != 1:
            yield (
                f"host {compose.printable(host)} needs exactly one service labelled "
                f"{EDGE_NAME_LABEL} with its name (labels)"
            )


def _labels(definition: dict) -> dict[str, str]:
    labels = definition.get("labels")
    if isinstance(labels, dict):
        return {
            str(key): str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in labels.items()
        }
    if isinstance(labels, list):
        pairs = (str(item).partition("=") for item in labels)
        return {key: value for key, _, value in pairs}
    return {}


def _covered_paths(definition: dict) -> set[str]:
    """The container paths that a named volume, a bind or a tmpfs of the service mounts."""
    covered = set()
    for volume in _mounts(definition):
        if isinstance(volume, dict):
            target = volume.get("target")
        else:
            parts = str(volume).split(":")
            target = parts[1] if len(parts) > 1 else None
        if isinstance(target, str):
            covered.add(target.rstrip("/"))
    tmpfs = definition.get("tmpfs") or ()
    for mount in [tmpfs] if isinstance(tmpfs, str) else tmpfs:
        covered.add(str(mount).split(":")[0].rstrip("/"))
    return covered


def _mounts(definition: dict) -> list:
    volumes = definition.get("volumes")
    return volumes if isinstance(volumes, list) else []


def _interpolates(value: object) -> bool:
    return isinstance(value, str) and "$" in value
