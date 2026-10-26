"""What each unit needs to become what the catalog says, and how one unit is switched."""

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from edge import barrier, catalog, compose, core, switches


class Action(StrEnum):
    ON = "on"
    OFF = "off"
    STAYS_STOPPED = "stays stopped"


def stays_stopped_line(key: str, since: str | None) -> str:
    """A unit or app that its marker or its standing barrier keeps off; `since` is None when only
    the barrier stands."""
    if since is None:
        return f"{key} stays stopped (its barrier stands; no switch recorded)"
    return f"{key} stays stopped (switch since {switches.describe_since(since)})"


@dataclass(frozen=True)
class UnitPlan:
    unit: catalog.Unit
    action: Action
    services: tuple[str, ...]
    volumes: tuple[str, ...]
    switched_off_since: str | None = None
    running: bool = False

    def describe(self) -> str:
        touched = f"services {', '.join(self.services)}"
        if self.action is Action.STAYS_STOPPED:
            return stays_stopped_line(self.unit.key, self.switched_off_since)
        if self.action is Action.OFF:
            line = f"{self.unit.key}: off ({touched}; volumes {', '.join(self.volumes)} stay)"
            if self.switched_off_since:
                line += f"; its switch since {self._since()} stays"
            return line
        return f"{self.unit.key}: on ({touched})"

    def _since(self) -> str:
        return switches.describe_since(self.switched_off_since or "")


@dataclass(frozen=True)
class Fleet:
    """What `fleet.yaml` and Compose say about this repository's units."""

    config: Mapping[str, Any]
    profile_services: Mapping[str, tuple[str, ...]]
    units: tuple[catalog.Unit, ...]


def read_fleet(engine: compose.Compose, repository: Path) -> Fleet:
    config = engine.resolved_configuration()
    profile_services = compose.profile_services(config)
    return Fleet(config, profile_services, catalog.load(repository, profile_services))


def plan_switches(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    states: Mapping[str, set[str]],
    switched_off: Mapping[str, str | None],
) -> list[UnitPlan]:
    """What each unit needs to become what the catalog says; a unit already so is left out.

    `switched_off` maps a switched-off unit to the time of its switch, None when only its barrier
    stands: that unit is never started, and an enabled one is listed as staying stopped.
    """
    plans = []
    for unit in units:
        services = profile_services[unit.key]
        since = switched_off.get(unit.key)
        if unit.enabled and unit.key in switched_off:
            action = Action.STAYS_STOPPED
        elif unit.enabled and not compose.is_up(states, config, services):
            action = Action.ON
        elif not unit.enabled and not compose.is_gone(states, services):
            action = Action.OFF
        else:
            continue
        volumes = sorted(
            {v for name in services for v in compose.named_volumes(config["services"][name])}
        )
        running = compose.has_running_container(states, services)
        plans.append(UnitPlan(unit, action, services, tuple(volumes), since, running))
    return plans


DIGEST_SEPARATOR = "@"
KEPT_IMAGE_OWNER = "the upgrade is yours (block 6; Keycloak with a dump first, K11b)"


def unit_gates(
    units: Sequence[catalog.Unit], switched_off: Mapping[str, str | None]
) -> dict[str, core.Gate]:
    """Each catalogued unit's gate for its followers: on while enabled and not switched off,
    whether or not it is up (#89)."""
    return {unit.key: _gate(unit, switched_off) for unit in units}


def _gate(unit: catalog.Unit, switched_off: Mapping[str, str | None]) -> core.Gate:
    if unit.key in switched_off:
        return core.Gate.SWITCHED_OFF
    if not unit.enabled:
        return core.Gate.OFF
    return core.Gate.ON


def switched_off_units(
    fleet: Fleet, read: Mapping[str, switches.Mark | None], repository: Path
) -> dict[str, str | None]:
    """Each unit switched off by its marker (its time) or by a barrier a stop left (None)."""
    switched_off = {}
    for unit in fleet.units:
        mark = read[unit.key]
        services = fleet.profile_services[unit.key]
        ingress = barrier.Barrier.of_unit(fleet.config, services, repository, unit)
        if barrier.switched_off(mark, ingress):
            switched_off[unit.key] = mark.since if mark else None
    return switched_off


@dataclass(frozen=True)
class KeptImage:
    """A live service of a unit that runs another image than compose.yaml names now."""

    unit: str
    service: str
    running: str
    configured: str

    def describe(self) -> str:
        return (
            f"{self.unit}: {self.service} runs {self.running}, compose.yaml names "
            f"{self.configured}; edge apply never swaps the image of a running unit, so it "
            f"keeps running; {KEPT_IMAGE_OWNER}"
        )


def plan_kept_images(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    containers: Sequence[compose.Container],
    plans: Sequence[UnitPlan],
    followers: Collection[str],
) -> list[KeptImage]:
    """Each live service of a unit whose container's image is not the exact reference its
    resolved definition names, in catalog and file order (#91 rules 2, 3). The core has no
    profile, the followers are recreated by their own plan, a unit planned off loses its
    containers, and a service whose definition names no image has no pin to compare."""
    planned_off = {plan.unit.key for plan in plans if plan.action is Action.OFF}
    live = [container for container in containers if container.live]
    kept: dict[KeptImage, None] = {}
    for unit in units:
        if unit.key not in planned_off:
            services = [
                service for service in profile_services[unit.key] if service not in followers
            ]
            kept.update(dict.fromkeys(_kept_images_of(unit.key, services, config, live)))
    return list(kept)


def _kept_images_of(
    unit: str, services: Sequence[str], config: Mapping[str, Any], live: Sequence[compose.Container]
) -> Iterator[KeptImage]:
    for service in services:
        configured = config["services"][service].get("image")
        if configured is None:
            continue
        for running in _running_images(service, live):
            if running != configured:
                yield KeptImage(unit, service, running, configured)


def _running_images(service: str, live: Sequence[compose.Container]) -> list[str]:
    """The images the service's live containers were created from; a ps line naming none cannot
    be compared, so it cannot be planned, as a missing label is (#87)."""
    images = []
    for container in live:
        if container.service != service:
            continue
        if container.image is None:
            raise compose.ComposeError(
                f"a live {service} container's `docker compose ps` line names no Image"
            )
        images.append(container.image)
    return images


@dataclass(frozen=True)
class Recreate:
    """A live service of a unit that runs on whose compose definition changed."""

    unit: str
    service: str

    def describe(self) -> str:
        return f"{self.unit}: recreate {self.service} ({core.DEFINITION_CHANGED})"


class RecreatePlans(NamedTuple):
    recreates: list[Recreate]
    moved: list[KeptImage]


def plan_recreates(
    units: Sequence[catalog.Unit],
    profile_services: Mapping[str, tuple[str, ...]],
    config: Mapping[str, Any],
    containers: Sequence[compose.Container],
    hashes: Mapping[str, str],
    plans: Sequence[UnitPlan],
    followers: Collection[str],
    kept: Sequence[KeptImage],
    image_id: Callable[[str], str],
) -> RecreatePlans:
    """Each live service of an enabled unit that is not switched on, off or kept stopped by a
    plan, whose container is labelled with another definition hash than `hashes` (from
    `docker compose config --hash`), in catalog order and, within a unit, after the services it
    depends on. The followers have their own plan, a service whose image differs is only named
    (#91), and a container without the label cannot be planned (#376). A reference that is not
    pinned by digest may name another image than the container runs (a rebuilt local tag), which
    a recreate would swap in: that service is named, not recreated."""
    planned = {plan.unit.key for plan in plans}
    kept_services = {image.service for image in kept}
    live = [container for container in containers if container.live]
    recreates: list[Recreate] = []
    moved: list[KeptImage] = []
    for unit in units:
        if not unit.enabled or unit.key in planned:
            continue
        services = [
            service
            for service in profile_services[unit.key]
            if service not in followers and service not in kept_services
        ]
        changed = core.changed_definitions(live, services, hashes)
        for service in core.dependency_order(config, services):
            if service not in changed:
                continue
            if swapped := _swapped_image(unit.key, service, config, live, image_id):
                moved.append(swapped)
            else:
                recreates.append(Recreate(unit.key, service))
    return RecreatePlans(recreates, moved)


def _swapped_image(
    unit: str,
    service: str,
    config: Mapping[str, Any],
    live: Sequence[compose.Container],
    image_id: Callable[[str], str],
) -> KeptImage | None:
    """The service when a recreate would run another image than its container runs now: its
    reference is not pinned by digest and names another image id than the container was created
    from."""
    reference = config["services"][service].get("image")
    if reference is None or DIGEST_SEPARATOR in reference:
        return None
    current = image_id(reference)
    for container in live:
        if container.service != service:
            continue
        if container.image_id is None:
            raise compose.ComposeError(
                f"a live {service} container has no {compose.IMAGE_ID_LABEL} label"
            )
        if container.image_id != current:
            return KeptImage(
                unit, service, f"{reference} ({container.image_id})", f"{reference} ({current})"
            )
    return None


@dataclass
class Progress:
    unit: str = ""
    step: str = ""


def switch(
    engine: compose.Compose,
    plan: UnitPlan,
    progress: Progress,
    marks: switches.Switches,
    read: Mapping[str, switches.Mark | None],
) -> str | None:
    """Switch one unit; the time of a switch that appeared meanwhile when the unit stays down."""
    progress.unit = plan.unit.key
    if plan.action is Action.ON:
        progress.step = "start"
        with marks.lock():
            if changed := marks.changed(plan.unit.key, read[plan.unit.key]):
                return since_of(changed)
            engine.start(plan.unit.key, plan.services)
        return None
    progress.step = "stop"
    engine.stop(plan.unit.key, plan.services)
    progress.step = "remove the containers"
    engine.remove_containers(plan.unit.key, plan.services)
    return None


def since_of(changed: switches.Changed) -> str:
    """The time of a switch that appeared after the plan was read."""
    return changed.current.since if changed.current else switches.UNKNOWN_SWITCH_TIME


def unobserved(
    engine: compose.Compose,
    plans: Sequence[UnitPlan],
    config: Mapping[str, Any],
    watch: compose.Watch,
) -> list[UnitPlan]:
    """The plans whose result is not observed: a unit switched on counts only when its services
    stay up through the watch, the way a recreated core service does."""
    switched_on = [plan for plan in plans if plan.action is Action.ON]
    watched = tuple(dict.fromkeys(service for plan in switched_on for service in plan.services))
    down = set(engine.down_while_watched(config, watched, watch)) if watched else set()
    states = engine.states()
    return [
        plan
        for plan in plans
        if (
            down.intersection(plan.services)
            if plan.action is Action.ON
            else not compose.is_gone(states, plan.services)
        )
    ]
