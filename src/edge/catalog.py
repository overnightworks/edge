"""`fleet.yaml`, the service catalog: the one owner of reading it and of its typed schemas.

`edge apply` reads the entries that have a `run` block and no `repo`: the units of this
repository. A unit's key is a compose profile (docs/naming.md). An entry with `repo` and `run` is
an app: its key is its repository's name under `github.org`, its compose project and its checkout
`~/srv/<key>`; `apps()` reads them. Entries without `run` are no units and no apps, and `units()`
and `apps()` ignore them. `hosts()` is the separate reader of every entry's `hosts` list (an entry
without `run` is enabled); the top-level `cloudflare:` and `github:` keys belong to OpenTofu, which
reads the same file and must agree with these readers.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

FLEET_FILE = "fleet.yaml"
_RUN_KEYS = {"enabled", "setup"}
_APP_RUN_KEYS = {"enabled"}
_REPO_KEYS = {"default_branch"}
EDGE_PROJECT = "edge"  # the compose project of this repository's own stack
_BOOL_TAG = "tag:yaml.org,2002:bool"
_TRUE_OR_FALSE = re.compile(r"^(?:true|false)$")
_FLOAT_TAG = "tag:yaml.org,2002:float"
_INT_TAG = "tag:yaml.org,2002:int"
# Forms tofu reads as numbers and PyYAML leaves as strings, so they must not pass as labels.
_EXPONENT_NUMBER = re.compile(r"^[-+]?\d+[eE][-+]?\d+$")
_OCTAL_NUMBER = re.compile(r"^0o[0-7]+$")

DEFAULT_TARGET = "http://127.0.0.1:8081"  # Traefik
ADMIN_PORT = 8090
_HOST_KEYS = {"name", "exposure", "reason", "ready", "target", "deny_public"}
# Lowercase unreserved segments, no query or trailing slash: the rendering embeds it in a rule.
_DENIED_PATH = re.compile(r"(/[a-z0-9._~-]+)+")
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# A compose project name: it is also the repository and the directory name of an app.
_APP_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*")
_GITHUB_ORGANISATION = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
# What `git check-ref-format --branch` accepts, narrowed to what never needs quoting.
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost"}
APEX = "@"
WILDCARD = "*"
PROBE_NAME = "edge-host-check"  # the host check asks the wildcard here (#154)
# Gap 35 (docs/architecture.md section 7): these hosts still bypass Traefik. Their `target` stands
# once, in fleet.yaml. Deleted when the songmaker cutover (X11) lands.
GAP_35_HOSTS = frozenset({("songmaker", APEX), ("songmaker", "www"), ("songmaker", "songmaker")})


class CatalogError(Exception):
    """The catalog is invalid; the message is one line that names the key."""


class _CatalogLoader(yaml.SafeLoader):
    """Only `true` and `false` are booleans, and the numbers tofu reads are no strings.

    tofu's `yamldecode` also reads YAML 1.1 words (`yes`, `no`, `on`, `off`, `y`, `n`, `True`)
    as booleans and PyYAML disagrees on some of them; only `true` and `false` mean the same to
    both readers, so every other spelling is refused.
    """


_CatalogLoader.yaml_implicit_resolvers = {
    first_character: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL_TAG]
    for first_character, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_CatalogLoader.add_implicit_resolver(_BOOL_TAG, _TRUE_OR_FALSE, list("tf"))
_CatalogLoader.add_implicit_resolver(_FLOAT_TAG, _EXPONENT_NUMBER, list("-+0123456789"))
_CatalogLoader.add_implicit_resolver(_INT_TAG, _OCTAL_NUMBER, ["0"])


@dataclass(frozen=True)
class Unit:
    key: str
    enabled: bool
    setup: str | None


@dataclass(frozen=True)
class App:
    key: str
    enabled: bool
    default_branch: str


def read(catalog_file: Path) -> Any:
    """The catalog's document, read the way tofu reads it."""
    return parse(read_bytes(catalog_file), catalog_file.name)


def read_bytes(catalog_file: Path) -> bytes:
    """The catalog file as it is now: a caller that must compare two reads keeps the bytes."""
    try:
        return catalog_file.read_bytes()
    except OSError as error:
        raise CatalogError(
            f"{catalog_file.name}: cannot read it ({type(error).__name__})"
        ) from error


def parse(raw: bytes, name: str = FLEET_FILE) -> Any:
    """The document of catalog bytes already read; `name` is the file the message names."""
    try:
        return yaml.load(raw.decode(), Loader=_CatalogLoader)
    except yaml.YAMLError as error:
        raise CatalogError(f"{name}: cannot read it ({type(error).__name__})") from error


def load(repository: Path, profile_services: Mapping[str, tuple[str, ...]]) -> tuple[Unit, ...]:
    return units(read(repository / FLEET_FILE), profile_services, repository)


def units(
    document: Any, profile_services: Mapping[str, tuple[str, ...]], repository: Path
) -> tuple[Unit, ...]:
    """The units of the repository's catalog, each checked against the compose profiles."""
    return tuple(
        _unit(key, entry["run"], profile_services, repository)
        for key, entry in _entries(document).items()
        if _is_unit(entry)
    )


def _entries(document: Any) -> dict:
    """The `services` map of the catalog's document."""
    if not isinstance(document, dict):
        raise CatalogError(f"{FLEET_FILE}: must be a map")
    entries = document.get("services", {})
    if not isinstance(entries, dict):
        raise CatalogError("services: must be a map of entries")
    return entries


def _is_unit(entry: Any) -> bool:
    return isinstance(entry, dict) and "run" in entry and "repo" not in entry


def _is_app(entry: Any) -> bool:
    return isinstance(entry, dict) and "run" in entry and "repo" in entry


def apps(document: Any, profile_services: Mapping[str, tuple[str, ...]]) -> tuple[App, ...]:
    """The apps of the catalog: the entries with `repo` and `run`. A `repo` entry without `run` is
    a library and stays ignored."""
    return tuple(
        _app(key, entry, profile_services)
        for key, entry in _entries(document).items()
        if _is_app(entry)
    )


def github_org(document: Any) -> str:
    """The GitHub organisation that owns the repositories of the apps (`github.org`)."""
    github = document.get("github") if isinstance(document, dict) else None
    organisation = github.get("org") if isinstance(github, dict) else None
    if not isinstance(organisation, str) or not _GITHUB_ORGANISATION.fullmatch(organisation):
        raise CatalogError("github.org: must be a GitHub organisation name")
    return organisation


def _app(key: str, entry: dict, profile_services: Mapping[str, tuple[str, ...]]) -> App:
    if key in profile_services or key == EDGE_PROJECT:
        raise CatalogError(f"services.{key}: an app's key is no compose profile or project of edge")
    if not _APP_KEY.fullmatch(key):
        raise CatalogError(f"services.{key}: an app's key must be a compose project name")
    run, repo = entry["run"], entry["repo"]
    if not isinstance(run, dict):
        raise CatalogError(f"services.{key}.run: must be a map")
    for run_key in run:
        if run_key not in _APP_RUN_KEYS:
            raise CatalogError(f"services.{key}.run.{run_key}: unknown key for an app")
    return App(key, _enabled(key, run), _default_branch(key, repo))


def _default_branch(key: str, repo: Any) -> str:
    if not isinstance(repo, dict):
        raise CatalogError(f"services.{key}.repo: must be a map")
    for repo_key in repo:
        if repo_key not in _REPO_KEYS:
            raise CatalogError(f"services.{key}.repo.{repo_key}: unknown key")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not _is_branch_name(branch):
        raise CatalogError(f"services.{key}.repo.default_branch: must be a branch name")
    return branch


def _is_branch_name(branch: str) -> bool:
    return (
        bool(_BRANCH.fullmatch(branch))
        and ".." not in branch
        and not branch.endswith((".", ".lock"))
    )


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
    deny_public: tuple[str, ...] = ()


def hosts(document: Any) -> tuple[Host, ...]:
    """The hosts of the enabled entries; the hosts of disabled entries are checked, not returned."""
    if not isinstance(document, dict):
        raise CatalogError(f"{FLEET_FILE}: must be a map")
    zone_name = zone(document)
    entries = document.get("services", {})
    if not isinstance(entries, dict):
        raise CatalogError("services: must be a map of entries")
    enabled_hosts: list[tuple[str, Host]] = []
    for key, entry in entries.items():
        if not isinstance(entry, dict) or "hosts" not in entry:
            continue
        entered = _entry_hosts(key, entry["hosts"], zone_name)
        if _entry_enabled(key, entry):
            enabled_hosts += entered
    _check_together(enabled_hosts)
    return tuple(host for _, host in enabled_hosts)


def zone(document: Any) -> str:
    """The catalog's `cloudflare.zone`, validated the way every reader of it needs."""
    cloudflare = document.get("cloudflare") if isinstance(document, dict) else None
    name = cloudflare.get("zone") if isinstance(cloudflare, dict) else None
    if not isinstance(name, str) or not _is_dotted_labels(name):
        raise CatalogError("cloudflare.zone: must be a lowercase name without a trailing dot")
    return name


def _is_dotted_labels(zone: str) -> bool:
    labels = zone.split(".")
    return len(labels) >= 2 and all(_LABEL.fullmatch(label) for label in labels)


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
    _check_exposure_fits_name(path, name, exposure)
    return Host(
        service=service,
        name=name,
        fqdn=zone if name == APEX else f"{name}.{zone}",
        exposure=exposure,
        reason=_reason(path, item.get("reason"), exposure),
        ready=_ready(path, item),
        target=_target(path, service, name, item.get("target", DEFAULT_TARGET)),
        deny_public=_deny_public(path, item.get("deny_public", [])),
    )


def _name(path: str, name: Any) -> str:
    if not isinstance(name, str) or not (name in (APEX, WILDCARD) or _LABEL.fullmatch(name)):
        raise CatalogError(f"{path}.name: must be '@', '*' or one lowercase DNS label")
    if name == PROBE_NAME:
        raise CatalogError(f"{path}.name: reserved for the host check")
    return name


def _exposure(path: str, exposure: Any) -> Exposure:
    try:
        return Exposure(exposure)
    except ValueError:
        values = ", ".join(member.value for member in Exposure)
        raise CatalogError(f"{path}.exposure: must be one of {values}") from None


def _check_exposure_fits_name(path: str, name: str, exposure: Exposure) -> None:
    if name == WILDCARD and exposure is not Exposure.PROTECTED:
        raise CatalogError(f"{path}.exposure: the wildcard must be protected")
    if exposure is Exposure.PROTECTED and name != WILDCARD:
        raise CatalogError(f"{path}.exposure: only the wildcard may be protected")


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
    if not isinstance(ready, str) or not _is_request_path(ready):
        raise CatalogError(
            f"{path}.ready: must be a path of printable ASCII, no space, that starts with one '/'"
        )
    return ready


def _is_request_path(ready: str) -> bool:
    """What the probe can send as a request target: its request line has room for no more."""
    return (
        ready.startswith("/")
        and not ready.startswith("//")
        and ready.isascii()
        and ready.isprintable()
        and " " not in ready
    )


def _deny_public(path: str, denied: Any) -> tuple[str, ...]:
    if not isinstance(denied, list) or not all(
        isinstance(entry, str) and _DENIED_PATH.fullmatch(entry) for entry in denied
    ):
        raise CatalogError(
            f"{path}.deny_public: must be a list of lowercase absolute paths "
            "(letters, digits, . _ ~ -), no query, no trailing slash"
        )
    return tuple(denied)


def _loopback_port(target: Any) -> int | None:
    """The port of a loopback `target` (127.0.0.1 or localhost, explicit port); else None."""
    if not isinstance(target, str):
        return None
    parts = urlsplit(target)
    try:
        port = parts.port
    except ValueError:
        return None
    spelled_out = f"{parts.scheme}://{parts.netloc}"
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTNAMES or not port:
        return None
    return port if target == spelled_out and parts.netloc == f"{parts.hostname}:{port}" else None


def _target(path: str, service: str, name: str, target: Any) -> str:
    port = _loopback_port(target)
    if port is None:
        raise CatalogError(
            f"{path}.target: must be http://127.0.0.1:<port> or http://localhost:<port>"
        )
    if port == ADMIN_PORT:
        raise CatalogError(f"{path}.target: must not be the admin entrypoint :{ADMIN_PORT}")
    if target != DEFAULT_TARGET and (service, name) not in GAP_35_HOSTS:
        raise CatalogError(f"{path}.target: only gap-35 hosts may differ from {DEFAULT_TARGET}")
    return target


def _check_together(enabled_hosts: list[tuple[str, Host]]) -> None:
    wildcard_exists = any(host.name == WILDCARD for _, host in enabled_hosts)
    seen: set[str] = set()
    for path, host in enabled_hosts:
        if wildcard_exists and host.exposure is Exposure.PUBLIC and host.name != APEX:
            raise CatalogError(
                f"{path}.exposure: a public host needs its own bypass while '*' exists"
            )
        if host.fqdn in seen:
            raise CatalogError(f"{path}.name: {host.fqdn} is listed twice")
        seen.add(host.fqdn)
