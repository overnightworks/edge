"""The built service of a unit with `run.build`: the one service of its profile with `pull_policy:
never`, whose image apply swaps in one recreate under the plan's yes, whatever state its container
is in. Its plan is read from Docker's facts alone (the container's `ps` image and labels, the
tag's id), so it keeps no record and every interruption is planned again from what is there."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hal.stack import catalog, compose, core

PULL_NEVER = "never"
ENDS_RUNS = "in-flight requests end"


@dataclass(frozen=True)
class BuiltRecreate:
    """The built service of a unit that runs on another image or definition than compose.yaml and
    its tag name now; one reason for each distinct state of its containers."""

    unit: str
    service: str
    reference: str
    moved_from: tuple[str, ...]  # the distinct `ps` images that are neither the tag nor its id
    definition_changed: bool
    rebuilt: bool


class Reason(StrEnum):
    IMAGE_MOVED = "image moved"
    DEFINITION_CHANGED = "definition changed"
    REBUILT = "rebuilt"


def built_services(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    gates: Mapping[str, core.Gate],
) -> dict[str, str]:
    """The built service of each unit with `run.build` whose gate is on, by unit; a unit without
    exactly one `pull_policy: never` service cannot be planned."""
    services = {}
    for unit in units:
        if unit.build is None or gates[unit.key] is not core.Gate.ON:
            continue
        never_pulled = [
            service
            for service in profile_services[unit.key]
            if config["services"][service].get("pull_policy") == PULL_NEVER
        ]
        if len(never_pulled) != 1:
            raise compose.ComposeError(
                f"{unit.key} builds its image, so exactly one of its services must name "
                f"pull_policy {PULL_NEVER}; it has {len(never_pulled)}"
            )
        services[unit.key] = never_pulled[0]
    return services


def plan_built_images(
    services: Mapping[str, str],
    config: Mapping[str, Any],
    containers: Sequence[compose.Container],
    hashes: Mapping[str, str],
    built_image_id: Callable[[str], str | None],
) -> list[BuiltRecreate]:
    """A recreate for each built service with a container, in any state, that runs another image
    than compose.yaml names, another definition than `hashes` (from `docker compose config
    --hash`) or another image than its tag names now; a service without a container is created
    by its unit's switch-on."""
    plans = []
    for unit, service in services.items():
        reference = config["services"][service]["image"]
        reasons = {
            container: reason
            for container in containers
            if container.service == service
            if (reason := _reason(container, reference, hashes[service], built_image_id))
        }
        if reasons:
            moved_from = {
                container.image
                for container, reason in reasons.items()
                if reason is Reason.IMAGE_MOVED and container.image is not None
            }
            plans.append(
                BuiltRecreate(
                    unit,
                    service,
                    reference,
                    tuple(sorted(moved_from)),
                    Reason.DEFINITION_CHANGED in reasons.values(),
                    Reason.REBUILT in reasons.values(),
                )
            )
    return plans


def _reason(
    container: compose.Container,
    reference: str,
    current_hash: str,
    built_image_id: Callable[[str], str | None],
) -> Reason | None:
    """The first reason that applies, in the order they are named. A `ps` image that is the
    container's own image id is no reference: once its tag moved or is gone, Compose may show the
    id, and a recreate onto the tag is then named by the definition or the rebuilt tag. An empty
    tag lookup gives no reason, so the unit's setup still builds it."""
    _require_facts(container)
    if container.image not in (container.image_id, reference):
        return Reason.IMAGE_MOVED
    if container.config_hash != current_hash:
        return Reason.DEFINITION_CHANGED
    if (built := built_image_id(reference)) and built != container.image_id:
        return Reason.REBUILT
    return None


def _require_facts(container: compose.Container) -> None:
    """A container without its `ps` image or either label cannot be compared, so it cannot be
    planned, as a missing label is for every other unit (#87, #376)."""
    if container.image is None:
        raise compose.ComposeError(
            f"a {container.service} container's `docker compose ps` line names no Image"
        )
    for value, label in (
        (container.config_hash, compose.CONFIG_HASH_LABEL),
        (container.image_id, compose.IMAGE_ID_LABEL),
    ):
        if value is None:
            raise compose.ComposeError(f"a {container.service} container has no {label} label")
