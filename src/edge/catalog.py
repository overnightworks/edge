"""`fleet.yaml`, the service catalog: the one owner of reading it and of its typed schemas.

`edge apply` reads the entries that have a `run` block and no `repo`: the units of this
repository. A unit's key is a compose profile (docs/naming.md). Entries without `run` are no
units and `units()` ignores them. `hosts()` is the separate reader of every entry's `hosts` list
(an entry without `run` is enabled); the top-level `cloudflare:` and `github:` keys belong to
OpenTofu, which reads the same file and must agree with these readers.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

FLEET_FILE = "fleet.yaml"
_RUN_KEYS = {"enabled", "setup"}
_BOOL_TAG = "tag:yaml.org,2002:bool"
_TRUE_OR_FALSE = re.compile(r"^(?:true|false)$")

DEFAULT_TARGET = "http://127.0.0.1:8081"  # Traefik
ADMIN_PORT = 8090
_HOST_KEYS = {"name", "exposure", "reason", "ready", "target"}
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LOOPBACK_TARGET = re.compile(r"http://(?:127\.0\.0\.1|localhost):(?P<port>[0-9]{1,5})")
APEX = "@"
WILDCARD = "*"
# Gap 35 (docs/architecture.md section 7): these hosts still bypass Traefik. Their `target` stands
# once, in fleet.yaml. Deleted when the songmaker cutover (X11) lands.
GAP_35_HOSTS = frozenset({("songmaker", APEX), ("songmaker", "www")})


class CatalogError(Exception):
    """The catalog is invalid; the message is one line that names the key."""


class _CatalogLoader(yaml.SafeLoader):
    """YAML 1.2 booleans, as tofu's `yamldecode` reads them: `no` and `on` stay strings."""


_CatalogLoader.yaml_implicit_resolvers = {
    first_character: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL_TAG]
    for first_character, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_CatalogLoader.add_implicit_resolver(_BOOL_TAG, _TRUE_OR_FALSE, list("tf"))


@dataclass(frozen=True)
class Unit:
    key: str
    enabled: bool
    setup: str | None


def read(catalog_file: Path) -> Any:
    """The catalog's document, read the way tofu reads it."""
    try:
        return yaml.load(catalog_file.read_text(), Loader=_CatalogLoader)
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(
            f"{catalog_file.name}: cannot read it ({type(error).__name__})"
        ) from error


def load(repository: Path, profile_services: Mapping[str, tuple[str, ...]]) -> tuple[Unit, ...]:
    return units(read(repository / FLEET_FILE), profile_services, repository)


def units(
    document: Any, profile_services: Mapping[str, tuple[str, ...]], repository: Path
) -> tuple[Unit, ...]:
    """The units of the repository's catalog, each checked against the compose profiles."""
    if not isinstance(document, dict):
        raise CatalogError(f"{FLEET_FILE}: must be a map")
    entries = document.get("services", {})
    if not isinstance(entries, dict):
        raise CatalogError("services: must be a map of entries")
    return tuple(
        _unit(key, entry["run"], profile_services, repository)
        for key, entry in entries.items()
        if _is_unit(key, entry)
    )


def _is_unit(key: str, entry: Any) -> bool:
    if not isinstance(entry, dict) or "run" not in entry:
        return False
    if "repo" in entry:
        raise CatalogError(f"services.{key}: apps follow with block 6")
    return True


def _unit(
    key: str, run: Any, profile_services: Mapping[str, tuple[str, ...]], repository: Path
) -> Unit:
    if key not in profile_services:
        profiles = ", ".join(sorted(profile_services))
        raise CatalogError(
            f"services.{key}: no compose profile of that name (profiles: {profiles})"
        )
    if not isinstance(run, dict):
        raise CatalogError(f"services.{key}.run: must be a map")
    for run_key in run:
        if run_key not in _RUN_KEYS:
            raise CatalogError(f"services.{key}.run.{run_key}: unknown key")
    return Unit(key, _enabled(key, run), _setup(key, run.get("setup"), repository))


def _enabled(key: str, run: dict) -> bool:
    enabled = run.get("enabled")
    if not isinstance(enabled, bool):
        raise CatalogError(f"services.{key}.run.enabled: must be true or false")
    return enabled


def _setup(key: str, setup: Any, repository: Path) -> str | None:
    if setup is None:
        return None
    path = (repository / str(setup)).resolve()
    inside = path.is_relative_to(repository.resolve())
    if not (isinstance(setup, str) and inside and path.is_file() and os.access(path, os.X_OK)):
        raise CatalogError(f"services.{key}.run.setup: {setup!r} is not an executable file")
    return setup


class Exposure(StrEnum):
    PUBLIC = "public"
    BYPASS = "bypass"
    PROTECTED = "protected"


@dataclass(frozen=True)
class Host:
    service: str
    name: str
    fqdn: str
    exposure: Exposure
    reason: str | None
    ready: str | None
    target: str


def hosts(document: Any) -> tuple[Host, ...]:
    """The hosts of the enabled entries; the hosts of disabled entries are checked, not returned."""
    if not isinstance(document, dict):
        raise CatalogError(f"{FLEET_FILE}: must be a map")
    zone = _zone(document)
    entries = document.get("services", {})
    if not isinstance(entries, dict):
        raise CatalogError("services: must be a map of entries")
    enabled_hosts: list[tuple[str, Host]] = []
    for key, entry in entries.items():
        if not isinstance(entry, dict) or "hosts" not in entry:
            continue
        entered = _entry_hosts(key, entry["hosts"], zone)
        if _entry_enabled(key, entry):
            enabled_hosts += entered
    _check_together(enabled_hosts)
    return tuple(host for _, host in enabled_hosts)


def _zone(document: dict) -> str:
    cloudflare = document.get("cloudflare")
    zone = cloudflare.get("zone") if isinstance(cloudflare, dict) else None
    if not isinstance(zone, str) or zone != zone.lower() or zone.endswith("."):
        raise CatalogError("cloudflare.zone: must be a lowercase name without a trailing dot")
    return zone


def _entry_enabled(key: str, entry: dict) -> bool:
    if "run" not in entry:
        return True
    if not isinstance(entry["run"], dict):
        raise CatalogError(f"services.{key}.run: must be a map")
    return _enabled(key, entry["run"])


def _entry_hosts(key: str, listed: Any, zone: str) -> list[tuple[str, Host]]:
    if not isinstance(listed, list):
        raise CatalogError(f"services.{key}.hosts: must be a list")
    return [
        (f"services.{key}.hosts[{index}]", _host(key, f"services.{key}.hosts[{index}]", item, zone))
        for index, item in enumerate(listed)
    ]


def _host(service: str, path: str, item: Any, zone: str) -> Host:
    if not isinstance(item, dict):
        raise CatalogError(f"{path}: must be a map")
    for field in item:
        if field not in _HOST_KEYS:
            raise CatalogError(f"{path}.{field}: unknown key")
    name = _name(path, item.get("name"))
    exposure = _exposure(path, item.get("exposure"))
    return Host(
        service=service,
        name=name,
        fqdn=zone if name == APEX else f"{name}.{zone}",
        exposure=exposure,
        reason=_reason(path, item.get("reason"), exposure),
        ready=_ready(path, item),
        target=_target(path, service, name, item.get("target", DEFAULT_TARGET)),
    )


def _name(path: str, name: Any) -> str:
    if not isinstance(name, str) or not (name in (APEX, WILDCARD) or _LABEL.fullmatch(name)):
        raise CatalogError(f"{path}.name: must be '@', '*' or one lowercase DNS label")
    return name


def _exposure(path: str, exposure: Any) -> Exposure:
    try:
        return Exposure(exposure)
    except ValueError:
        values = ", ".join(member.value for member in Exposure)
        raise CatalogError(f"{path}.exposure: must be one of {values}") from None


def _reason(path: str, reason: Any, exposure: Exposure) -> str | None:
    if reason is not None and not isinstance(reason, str):
        raise CatalogError(f"{path}.reason: must be a string")
    if exposure is not Exposure.PROTECTED and not (reason and reason.strip()):
        raise CatalogError(f"{path}.reason: a {exposure} host needs one")
    return reason


def _ready(path: str, item: dict) -> str | None:
    if "ready" not in item:
        return None
    ready = item["ready"]
    if not isinstance(ready, str) or not ready.startswith("/") or ready.startswith("//"):
        raise CatalogError(f"{path}.ready: must be a path that starts with one '/'")
    return ready


def _target(path: str, service: str, name: str, target: Any) -> str:
    matched = _LOOPBACK_TARGET.fullmatch(target) if isinstance(target, str) else None
    if not matched or not 0 < int(matched["port"]) < 65536:
        raise CatalogError(
            f"{path}.target: must be http://127.0.0.1:<port> or http://localhost:<port>"
        )
    if int(matched["port"]) == ADMIN_PORT:
        raise CatalogError(f"{path}.target: must not be the admin entrypoint :{ADMIN_PORT}")
    if target != DEFAULT_TARGET and (service, name) not in GAP_35_HOSTS:
        raise CatalogError(f"{path}.target: only gap-35 hosts may differ from {DEFAULT_TARGET}")
    return target


def _check_together(enabled_hosts: list[tuple[str, Host]]) -> None:
    wildcard_exists = any(host.name == WILDCARD for _, host in enabled_hosts)
    seen: set[str] = set()
    for path, host in enabled_hosts:
        if host.name == WILDCARD and host.exposure is not Exposure.PROTECTED:
            raise CatalogError(f"{path}.exposure: the wildcard must be protected")
        if host.exposure is Exposure.PROTECTED and host.name != WILDCARD:
            raise CatalogError(f"{path}.exposure: only the wildcard may be protected")
        if wildcard_exists and host.exposure is Exposure.PUBLIC and host.name != APEX:
            raise CatalogError(
                f"{path}.exposure: a public host needs its own bypass while '*' exists"
            )
        if host.fqdn in seen:
            raise CatalogError(f"{path}.name: {host.fqdn} is listed twice")
        seen.add(host.fqdn)
