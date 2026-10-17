"""The core of the stack and the record of what of it was last applied (#85 rules 1, 2, 6).

The core is what compose.yaml runs without a profile (today socket-proxy and traefik), as Compose
resolves it. A core service's file binds are its `type: bind` volumes whose source lies in the
checkout and is not a directory: today only `traefik.yml`. A directory bind such as `dynamic/` is
live through Traefik's watch and needs no record.

`~/.local/state/edge/applied.json`, beside the switches, holds for each core service the sha256 of
each file bind's bytes as last fully applied: `{service: {path in the checkout: sha256}}`. A
service is changed when a file bind read now differs from its entry; a missing, unreadable or
malformed record, a missing entry or an entry naming another file counts as changed.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from edge import compose, switches

APPLIED_FILE = "applied.json"

type Digests = Mapping[str, str]  # a file bind's path in the checkout -> the sha256 of its bytes


class CoreError(Exception):
    """A file bind or the applied record cannot be read or written; the message is one line."""


def core_services(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The services compose.yaml runs without a profile, in file order."""
    return tuple(
        name for name, service in config["services"].items() if not service.get("profiles")
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
            return Reading({}, f"{APPLIED_FILE} cannot be read ({error.strerror})")
        try:
            entries = json.loads(text)
        except ValueError:
            entries = None
        if not _is_record(entries):
            return Reading({}, f"{APPLIED_FILE} is malformed")
        return Reading(entries)

    def write(self, service: str, applied: Digests) -> None:
        """Set the service's entry; an unreadable or malformed record is replaced whole."""
        entries = {**self.read().entries, service: dict(applied)}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            switches.write_atomically(
                self.path, json.dumps(entries, indent=2, sort_keys=True), keep_existing=False
            )
        except OSError as error:
            raise CoreError(f"cannot write {self.path} ({error.strerror})") from error


def _is_record(entries: object) -> bool:
    return isinstance(entries, dict) and all(
        isinstance(entry, dict)
        and all(isinstance(name, str) and isinstance(digest, str) for name, digest in entry.items())
        for entry in entries.values()
    )


class CoreAction(StrEnum):
    RECREATE = "recreate"
    NOT_STARTED = "not started"


@dataclass(frozen=True)
class CorePlan:
    """A changed core service; `digests` are the file binds as read when planning."""

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


def plan_core(
    config: Mapping[str, Any], states: Mapping[str, set[str]], checkout: Path, record: Record
) -> list[CorePlan]:
    """Each core service whose file binds differ from the record; one with a live container is
    recreated, one without is only named, because the core is never started here (rule 4)."""
    reading = record.read()
    plans = []
    for service in core_services(config):
        sources = file_binds(config["services"][service], checkout)
        if not sources:
            continue
        current = digests(sources, checkout)
        why = _why_changed(current, reading, service)
        if why is None:
            continue
        live = compose.has_running_container(states, (service,))
        action = CoreAction.RECREATE if live else CoreAction.NOT_STARTED
        plans.append(CorePlan(service, action, why, current))
    return plans


def _why_changed(current: Digests, reading: Reading, service: str) -> str | None:
    if reading.problem:
        return reading.problem
    applied = reading.entries.get(service, {})
    for name, digest in current.items():
        if name not in applied:
            return f"{name} has no applied record"
        if applied[name] != digest:
            return f"{name} changed"
    if others := sorted(applied.keys() - current.keys()):
        return f"{APPLIED_FILE} names {', '.join(others)}, which {service} does not mount"
    return None
