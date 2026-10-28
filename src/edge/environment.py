"""The two settings `compose.yaml` reads from `.env`, written once by `hal apply --local` (#136).

`EDGE_DOMAIN` is `fleet.yaml`'s `cloudflare.zone` and `DOCKER_GID` the gid of the `docker` group;
neither is a secret. A missing `.env` is written atomically and never over a file that appears
meanwhile; one that differs, or lacks a key, is named and left alone, because it is the
operator's file and a domain move or a new docker gid is deliberate.
"""

import contextlib
import grp
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from edge import catalog
from hal_output.command import Verb, command

ENVIRONMENT_FILE = ".env"
DOMAIN_KEY = "EDGE_DOMAIN"
DOCKER_GID_KEY = "DOCKER_GID"
DOCKER_GROUP = "docker"
HEADER = "# Written by `edge apply --closed`, which checks it on every run.\n"
_PRIVATE_FILE = 0o600
_QUOTES = "\"'"

GroupLookup = Callable[[str], grp.struct_group]


@dataclass(frozen=True)
class Settings:
    """The two derived values, in the order `.env` holds them."""

    domain: str
    docker_gid: str

    @property
    def assignments(self) -> tuple[tuple[str, str], ...]:
        return ((DOMAIN_KEY, self.domain), (DOCKER_GID_KEY, self.docker_gid))


class EnvironmentRefused(Exception):
    """The `.env` step cannot go on; the message is one line, nothing was written."""


def ensure(repository: Path, group_lookup: GroupLookup = grp.getgrnam) -> Settings | None:
    """Writes the `.env` of `repository` when it is missing and returns what it wrote; None when
    a `.env` already holds both derived values. Raises EnvironmentRefused otherwise."""
    wanted = _derived(repository, group_lookup)
    path = repository / ENVIRONMENT_FILE
    if os.path.lexists(path):
        _check(path, wanted)
        return None
    try:
        _write_without_replacing(path, wanted)
    except FileExistsError:
        _check(path, wanted)
        return None
    except OSError as error:
        raise EnvironmentRefused(f"cannot write {path} ({error.strerror})") from error
    return wanted


def _derived(repository: Path, group_lookup: GroupLookup) -> Settings:
    try:
        domain = catalog.zone(catalog.read(repository / catalog.FLEET_FILE))
    except catalog.CatalogError as error:
        raise EnvironmentRefused(f"cannot derive {DOMAIN_KEY} from {error}") from error
    try:
        docker_gid = group_lookup(DOCKER_GROUP).gr_gid
    except KeyError as error:
        raise EnvironmentRefused(
            f"this PC has no `{DOCKER_GROUP}` group to derive {DOCKER_GID_KEY} from; "
            "nothing written"
        ) from error
    return Settings(domain, str(docker_gid))


def _check(path: Path, wanted: Settings) -> None:
    try:
        written = _settings(path.read_text())
    except (OSError, UnicodeDecodeError) as error:
        reason = error.strerror if isinstance(error, OSError) else "not text"
        raise EnvironmentRefused(f"cannot read {path} ({reason})") from error
    for key, derived in wanted.assignments:
        if written.get(key) != derived:
            shown = "not set" if key not in written else f'"{written[key]}"'
            raise EnvironmentRefused(
                f'{path}: {key} is {shown} but edge derives "{derived}"; delete it and '
                f"`{command(Verb.APPLY)}` writes it again"
            )


def _settings(text: str) -> dict[str, str]:
    """The `KEY=value` lines the way compose reads them; the last of a key wins."""
    settings = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.lstrip().startswith("#"):
            settings[key.strip()] = value.strip().strip(_QUOTES)
    return settings


def _write_without_replacing(path: Path, wanted: Settings) -> None:
    """Raises FileExistsError when `path` exists by the time it is linked into place."""
    content = HEADER + "".join(f"{key}={value}\n" for key, value in wanted.assignments)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
    try:
        os.fchmod(descriptor, _PRIVATE_FILE)
        with os.fdopen(descriptor, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
