"""`fleet.yaml`, the service catalog: the one owner of reading it and of the typed `run` schema.

`edge apply` reads the entries that have a `run` block and no `repo`: the units of this
repository. A unit's key is a compose profile (docs/naming.md). Entries without `run` belong to
other readers and are ignored; the top-level `cloudflare:` and `github:` keys belong to OpenTofu.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FLEET_FILE = "fleet.yaml"
_RUN_KEYS = {"enabled", "setup"}


class CatalogError(Exception):
    """The catalog is invalid; the message is one line that names the key."""


@dataclass(frozen=True)
class Unit:
    key: str
    enabled: bool
    setup: str | None


def load(repository: Path, profile_services: Mapping[str, tuple[str, ...]]) -> tuple[Unit, ...]:
    try:
        document = yaml.safe_load((repository / FLEET_FILE).read_text())
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"{FLEET_FILE}: cannot read it ({type(error).__name__})") from error
    return units(document, profile_services, repository)


def units(
    document: Any, profile_services: Mapping[str, tuple[str, ...]], repository: Path
) -> tuple[Unit, ...]:
    """The units of the repository's catalog, each checked against the compose profiles."""
    entries = (document or {}).get("services") or {}
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
    enabled = run.get("enabled")
    if not isinstance(enabled, bool):
        raise CatalogError(f"services.{key}.run.enabled: must be true or false")
    return Unit(key, enabled, _setup(key, run.get("setup"), repository))


def _setup(key: str, setup: Any, repository: Path) -> str | None:
    if setup is None:
        return None
    path = (repository / str(setup)).resolve()
    inside = path.is_relative_to(repository.resolve())
    if not (isinstance(setup, str) and inside and path.is_file() and os.access(path, os.X_OK)):
        raise CatalogError(f"services.{key}.run.setup: {setup!r} is not an executable file")
    return setup
