"""The core of the stack and the record of what of it was last applied (#85 rules 1, 2, 6; #87).

The core is what compose.yaml runs without a profile (today socket-proxy and traefik), as Compose
resolves it. A core service is changed when its compose definition changed or a file it binds
changed. Its definition changed when `docker compose config --hash` differs from the
`com.docker.compose.config-hash` label of its container: both are Compose's own fact, so edge
keeps no record of them (#87 rule 1). A recreated core service pulls in the core services that
depend on it, and the core is recreated in the order of its `depends_on`: socket-proxy before
traefik (#87 rule 2).

A core service's file binds are its `type: bind` volumes whose source lies in the checkout and is
not a directory: today only `traefik.yml`. A directory bind such as `dynamic/` is live through
Traefik's watch and needs no record.

`~/.local/state/edge/applied.json`, beside the switches, holds for each service that binds a
file from the checkout the sha256 of each file bind's bytes as last fully applied:
`{service: {path in the checkout: sha256}}`. A file is changed when it differs from its entry; a
missing, unreadable or malformed record, a missing entry or an entry naming another file counts as
changed. An empty entry `{service: {}}` is written before a recreate starts and replaced only when
it finished, so a recreate that did not finish is planned again whatever the file holds now (rule
5). A core container found created and never started is retried only while the entry of a
recreate that took it in is empty; socket-proxy keeps none, so traefik's, emptied with it, speaks
for it (#87 rule 4). An entry for a service that binds no file from the checkout is ignored and
dropped at the next write (#87 rule 6).

A follower is a profile service that binds a file at Traefik's static-config path: today
traefik-mcp with `traefik-mcp.yml` (#89). It is changed as a core service is, by its file binds
against the record or its definition hash against its container's label, and what is done with a
changed one depends on its unit, which apply decides and passes in as a gate: while the unit is on,
a follower with a container is recreated on its own and one without starts with its unit; while
the unit is off or switched off it is only named and nothing starts. A follower whose profile is
no catalog unit is not planned at all.
"""

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from edge import compose, switches

APPLIED_FILE = "applied.json"
TRAEFIK_STATIC_CONFIG = "/etc/traefik/traefik.yml"  # where Traefik reads its static config
DEFINITION_CHANGED = "its compose definition changed"
UNFINISHED = "the last recreate did not finish"

type Digests = Mapping[str, str]  # a file bind's path in the checkout -> the sha256 of its bytes


class CoreError(Exception):
    """A file bind, the applied record or a core container's label cannot be read, or the record
    cannot be written; the message is one line."""


def core_services(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The services compose.yaml runs without a profile, in file order."""
    return tuple(
        name for name, service in config["services"].items() if not service.get("profiles")
    )


def dependencies(config: Mapping[str, Any], service: str) -> tuple[str, ...]:
    """The core services the service depends on, by its `depends_on`."""
    core = core_services(config)
    return tuple(name for name in config["services"][service].get("depends_on", {}) if name in core)


def start_order(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The core services, each after the core services it depends on, else in file order."""
    ordered: dict[str, None] = {}

    def place(service: str) -> None:
        if service not in ordered:
            for dependency in dependencies(config, service):
                place(dependency)
            ordered[service] = None

    for service in core_services(config):
        place(service)
    return tuple(ordered)


def followers(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The profile services that bind a file at Traefik's static-config path, in file order: each
    is a Traefik that runs the bound file (#89)."""
    return tuple(
        name
        for name, service in config["services"].items()
        if service.get("profiles")
        and any(
            mount.get("type") == "bind" and mount.get("target") == TRAEFIK_STATIC_CONFIG
            for mount in service.get("volumes", [])
        )
    )


def follower_units(config: Mapping[str, Any], units: Collection[str]) -> dict[str, str]:
    """Each follower whose profile is one of the catalog's units, with that unit; any other is
    never planned (#89 rule 5)."""
    found = {}
    for follower in followers(config):
        profiles = config["services"][follower]["profiles"]
        if unit := next((profile for profile in profiles if profile in units), None):
            found[follower] = unit
    return found


def followed_services(config: Mapping[str, Any], checkout: Path) -> frozenset[str]:
    """Every service, of any profile and running or not, that binds a file from the checkout."""
    return frozenset(name for name in config["services"] if follows(config, name, checkout))


def follows(config: Mapping[str, Any], service: str, checkout: Path) -> bool:
    """Edge follows the service, and keeps its entry in the record, while it binds a file from
    the checkout (#87 rule 6)."""
    return bool(file_binds(config["services"][service], checkout))


def file_binds(service: Mapping[str, Any], checkout: Path) -> tuple[Path, ...]:
    """The bind sources of a resolved service that lie in the checkout and are no directory."""
    sources = (
        Path(mount["source"]) for mount in service.get("volumes", []) if mount.get("type") == "bind"
    )
    return tuple(
        source for source in sources if source.is_relative_to(checkout) and not _is_dir(source)
    )


def _is_dir(source: Path) -> bool:
    # Python 3.12 raises from is_dir() when a directory on the way cannot be entered.
    try:
        return source.is_dir()
    except OSError as error:
        raise _cannot_read(source, error) from error


def digests(sources: Iterable[Path], checkout: Path) -> dict[str, str]:
    """The sha256 of each file's bytes as they are now; a file that cannot be read is an error."""
    found = {}
    for source in sources:
        try:
            content = source.read_bytes()
        except OSError as error:
            raise _cannot_read(source, error) from error
        found[source.relative_to(checkout).as_posix()] = hashlib.sha256(content).hexdigest()
    return found


def _cannot_read(source: Path, error: OSError) -> CoreError:
    return CoreError(f"cannot read {source} ({error.strerror})")


@dataclass(frozen=True)
class Reading:
    """The record as read; `problem` says why none of it can be trusted."""

    entries: dict[str, dict[str, str]]
    problem: str | None = None

    def unfinished(self, service: str) -> bool:
        """The service's recreate was started and never finished: its entry was emptied first."""
        return self.entries.get(service) == {}


@dataclass(frozen=True)
class Record:
    directory: Path

    @property
    def path(self) -> Path:
        return self.directory / APPLIED_FILE

    def read(self) -> Reading:
        try:
            text = self.path.read_bytes()
        except FileNotFoundError:
            return Reading({})
        except OSError as error:
            return Reading({}, f"{self.path} cannot be read ({error.strerror})")
        try:
            entries = json.loads(text)
        except ValueError:
            return Reading({}, f"{self.path} is malformed: it is not JSON")
        if why := _malformation(entries):
            return Reading({}, f"{self.path} is malformed: {why}")
        return Reading(entries)

    def mark_unfinished(self, services: Iterable[str], followed: Collection[str]) -> None:
        """Empty the services' entries before their recreates start, so a run that ends partial
        leaves nothing that could match the files later, and the next run knows a core container
        it finds created and never started is edge's own."""
        self._write({service: {} for service in services}, followed)

    def write(self, service: str, applied: Digests, followed: Collection[str]) -> None:
        """Set the service's entry; an unreadable or malformed record is replaced whole."""
        self._write({service: applied}, followed)

    def _write(self, changes: Mapping[str, Digests], followed: Collection[str]) -> None:
        """Write the changed entries over the record, keeping only the services edge follows: one
        it follows through no file keeps no entry (#87 rules 4, 6)."""
        entries = {
            service: dict(entry)
            for service, entry in (self.read().entries | dict(changes)).items()
            if service in followed
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            switches.write_atomically(
                self.path, json.dumps(entries, indent=2, sort_keys=True), keep_existing=False
            )
        except OSError as error:
            raise CoreError(f"cannot write {self.path} ({error.strerror})") from error


def _malformation(entries: object) -> str | None:
    """Why the parsed record is not services mapped to file digests; None when it is."""
    if not isinstance(entries, dict):
        return "it is not an object of services"
    for service, entry in entries.items():
        if not isinstance(entry, dict):
            return f"the {service} entry is not an object of files"
        for name, digest in entry.items():
            if not isinstance(digest, str):
                return f"{name} of {service} is not a digest string"
    return None


class CoreAction(StrEnum):
    RECREATE = "recreate"
    NOT_STARTED = "not started"


@dataclass(frozen=True)
class CorePlan:
    """A changed core service; `digests` are its file binds as read when planning, empty for a
    service edge follows through no file."""

    service: str
    action: CoreAction
    why: str
    digests: Digests

    @property
    def files(self) -> str:
        return ", ".join(self.digests)

    def describe(self) -> str:
        if self.action is CoreAction.NOT_STARTED:
            return f"{self.service}: {self.why}; it is not running and is not started"
        return f"{self.service}: recreate ({self.why})"

    def recreated(self, followed: Collection[str]) -> str:
        """The success line once the recreate is observed and every barrier is proven; one for a
        followed service names the files it runs."""
        if self.service in followed:
            return f"{self.service} is recreated and runs the checked-out {self.files}"
        return f"{self.service} is recreated"


def plan_core(
    config: Mapping[str, Any],
    containers: Sequence[compose.Container],
    hashes: Mapping[str, str],
    checkout: Path,
    record: Record,
) -> list[CorePlan]:
    """Each changed core service in start order, with `hashes` from `docker compose config
    --hash`. One with a live container is recreated and pulls in the core services that depend
    on it; one without, pulled in or not, is only named and pulls in nothing, because the core is
    never started here (#85 rule 4, #87 rule 3). A core container that was created and never started
    while the record says a recreate that took it in did not finish is no stopped service: edge's
    recreate left it so, and it is recreated again (#87 rule 4)."""
    reading = record.read()
    states = compose.states_of(containers)
    labels = _config_hash_labels(containers, core_services(config))
    plans: dict[str, CorePlan] = {}
    for service in start_order(config):
        current = digests(file_binds(config["services"][service], checkout), checkout)
        whys = _own_changes(
            config, service, labels.get(service, set()), hashes, checkout, current, reading
        )
        left_unstarted = _left_unstarted(config, states, reading, checkout, service)
        if left_unstarted and UNFINISHED not in whys:
            whys.append(UNFINISHED)
        whys += _pulled_in(config, service, plans)
        if whys:
            live = left_unstarted or compose.has_running_container(states, (service,))
            action = CoreAction.RECREATE if live else CoreAction.NOT_STARTED
            plans[service] = CorePlan(service, action, "; ".join(whys), current)
    return list(plans.values())


class Gate(StrEnum):
    """The state of a follower's unit as apply decides it: on is enabled and not switched off,
    whether or not the unit is up."""

    ON = "on"
    OFF = "off"
    SWITCHED_OFF = "switched off"


class FollowerAction(StrEnum):
    RECREATE = "recreate"
    STARTS_WITH_UNIT = "starts with its unit"
    HELD = "held"


@dataclass(frozen=True)
class FollowerPlan:
    """A changed follower; `digests` are its file binds as read when planning."""

    service: str
    unit: str
    action: FollowerAction
    why: str
    digests: Digests
    gate: Gate

    def describe(self) -> str:
        if self.action is FollowerAction.HELD:
            return f"{self.service}: {self.why}; {self.unit} is {self.gate}, so it is not started"
        if self.action is FollowerAction.STARTS_WITH_UNIT:
            return f"{self.service}: {self.why}; it starts with {self.unit}"
        return f"{self.service}: recreate ({self.why})"

    def recreated(self) -> str:
        return f"{self.service} is recreated and runs the checked-out {', '.join(self.digests)}"


def plan_followers(
    config: Mapping[str, Any],
    containers: Sequence[compose.Container],
    hashes: Mapping[str, str],
    checkout: Path,
    record: Record,
    gates: Mapping[str, Gate],
) -> list[FollowerPlan]:
    """Each changed follower whose profile is a unit in `gates`, in file order, with `hashes` from
    `docker compose config --hash` (#89 rules 1-6). A follower with any container whose unit is on
    is recreated, so a fixed file reaches one that crash-loops on a broken one; one without a
    container starts with its unit; one whose unit is off or switched off is held."""
    reading = record.read()
    states = compose.states_of(containers)
    units = follower_units(config, gates)
    labels = _config_hash_labels(containers, units)
    plans = []
    for follower, unit in units.items():
        current = digests(file_binds(config["services"][follower], checkout), checkout)
        whys = _own_changes(
            config, follower, labels.get(follower, set()), hashes, checkout, current, reading
        )
        if not whys:
            continue
        gate = gates[unit]
        if gate is not Gate.ON:
            action = FollowerAction.HELD
        elif follower in states:
            action = FollowerAction.RECREATE
        else:
            action = FollowerAction.STARTS_WITH_UNIT
        plans.append(FollowerPlan(follower, unit, action, "; ".join(whys), current, gate))
    return plans


def _own_changes(
    config: Mapping[str, Any],
    service: str,
    labels: Collection[str],
    hashes: Mapping[str, str],
    checkout: Path,
    current: Digests,
    reading: Reading,
) -> list[str]:
    """Why the service itself changed: a container labelled with another definition hash, or a
    file it binds that differs from the record."""
    whys = []
    if set(labels) - {hashes[service]}:
        whys.append(DEFINITION_CHANGED)
    if follows(config, service, checkout) and (why := _why_changed(current, reading, service)):
        whys.append(why)
    return whys


def _left_unstarted(
    config: Mapping[str, Any],
    states: Mapping[str, set[str]],
    reading: Reading,
    checkout: Path,
    service: str,
) -> bool:
    """The service's container was created and never started by a recreate of edge's that did
    not finish (#87 rule 4)."""
    return compose.never_started(states, service) and _recreate_unfinished(
        config, reading, checkout, service
    )


def _pulled_in(config: Mapping[str, Any], service: str, plans: Mapping[str, CorePlan]) -> list[str]:
    """A reason for each core service the service depends on that is recreated."""
    return [
        f"{dependency} is recreated"
        for dependency in dependencies(config, service)
        if dependency in plans and plans[dependency].action is CoreAction.RECREATE
    ]


def _recreate_unfinished(
    config: Mapping[str, Any], reading: Reading, checkout: Path, service: str
) -> bool:
    """The record marks unfinished a recreate that took the service in: its own, or that of a core
    service depending on it, which a recreate of the service always pulls in while it is live. Only
    the entry of a service edge follows through a file bind counts (#87 rules 4, 6): socket-proxy
    keeps none, so traefik's entry speaks for it."""
    took_in = {service, *_dependents(config, service)}
    return any(reading.unfinished(name) for name in took_in if follows(config, name, checkout))


def _dependents(config: Mapping[str, Any], service: str) -> set[str]:
    """The core services that depend on the service, directly or through another."""
    found: set[str] = set()
    for name in start_order(config):
        if any(
            dependency == service or dependency in found
            for dependency in dependencies(config, name)
        ):
            found.add(name)
    return found


def _config_hash_labels(
    containers: Iterable[compose.Container], core: Collection[str]
) -> dict[str, set[str]]:
    """The definition hash labels of each core service's containers; one without is an error,
    because without it every run would see a change (P15)."""
    labels: dict[str, set[str]] = {}
    for container in containers:
        if container.service not in core:
            continue
        if container.config_hash is None:
            raise CoreError(
                f"a {container.service} container has no {compose.CONFIG_HASH_LABEL} label"
            )
        labels.setdefault(container.service, set()).add(container.config_hash)
    return labels


def _why_changed(current: Digests, reading: Reading, service: str) -> str | None:
    if reading.problem:
        return reading.problem
    if reading.unfinished(service):
        return UNFINISHED
    applied = reading.entries.get(service, {})
    for name, digest in current.items():
        if name not in applied:
            return f"{name} has no applied record"
        if applied[name] != digest:
            return f"{name} changed"
    if others := sorted(applied.keys() - current.keys()):
        return f"{APPLIED_FILE} names {', '.join(others)}, which {service} does not mount"
    return None
