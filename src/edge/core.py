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
5). An entry for a service that binds no file from the checkout is ignored and dropped at the
next write (#87 rule 6).
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


def followed_services(config: Mapping[str, Any], checkout: Path) -> frozenset[str]:
    """Every service, of any profile and running or not, that binds a file from the checkout."""
    return frozenset(
        name for name, service in config["services"].items() if file_binds(service, checkout)
    )


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
        """Write the changed entries over the record, keeping only the services edge follows."""
        kept = {
            service: entry for service, entry in self.read().entries.items() if service in followed
        }
        entries = kept | {service: dict(applied) for service, applied in changes.items()}
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
            return f"{self.why}; {self.service} is not running and is not started"
        return f"{self.service}: recreate ({self.why})"

    def recreated(self) -> str:
        """The success line once the recreate is observed and every barrier is proven."""
        if self.digests:
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
    --hash`. One with a live container is recreated and pulls in the live core services that
    depend on it; one without is only named and pulls in nothing, because the core is never
    started here (#85 rule 4, #87 rule 3). A core container that was created and never started
    while the record says a recreate that took it in did not finish is no stopped service: edge's
    recreate left it so, and it is recreated again (#87 rule 4)."""
    reading = record.read()
    states = compose.states_of(containers)
    labels = _config_hash_labels(containers, core_services(config))
    binds = {
        service: file_binds(config["services"][service], checkout)
        for service in start_order(config)
    }
    recorded = {service for service, sources in binds.items() if sources}
    plans: dict[str, CorePlan] = {}
    for service, sources in binds.items():
        current = digests(sources, checkout)
        whys = []
        if labels.get(service, set()) - {hashes[service]}:
            whys.append(DEFINITION_CHANGED)
        if sources and (why := _why_changed(current, reading, service)):
            whys.append(why)
        left_unstarted = compose.never_started(states, service) and _recreate_unfinished(
            config, reading, recorded, service
        )
        if left_unstarted and UNFINISHED not in whys:
            whys.append(UNFINISHED)
        live = left_unstarted or compose.has_running_container(states, (service,))
        if live:
            whys += [
                f"{dependency} is recreated"
                for dependency in dependencies(config, service)
                if dependency in plans and plans[dependency].action is CoreAction.RECREATE
            ]
        if whys:
            action = CoreAction.RECREATE if live else CoreAction.NOT_STARTED
            plans[service] = CorePlan(service, action, "; ".join(whys), current)
    return list(plans.values())


def _recreate_unfinished(
    config: Mapping[str, Any], reading: Reading, recorded: set[str], service: str
) -> bool:
    """The record marks unfinished a recreate that took the service in: its own, or that of a core
    service depending on it, which a recreate of the service always pulls in while it is live. Only
    the entry of a service edge follows through a file bind counts (#87 rules 4, 6): socket-proxy
    keeps none, so traefik's entry speaks for it."""
    took_in = {service, *_dependents(config, service)}
    return any(reading.unfinished(name) for name in took_in & recorded)


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
