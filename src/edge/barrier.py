"""The ingress barrier of `edge stop`: a flat file under `dynamic/` that makes Traefik answer 404.

Traefik's file provider watches `dynamic/` but not its subdirectories, so the barrier is
`dynamic/switch-<unit>.yml`, written through a `.tmp` name the provider never reads. One router per
host the unit routes, on the `edge` entrypoint only, beats any label router by its priority and
sends the request back to Traefik's own `edge` entrypoint without its `Host`; no router matches
that, so Traefik itself answers `404 page not found`. The checkout is an argument; only the CLI
entry resolves the real one.
"""

import http.client
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import yaml

from edge_check.docker import GO_TRUE_SPELLINGS

DYNAMIC_DIRECTORY = "dynamic"
TRAEFIK_FILE = "traefik.yml"
EDGE_NAME_LABEL = "edge.name"
_ROUTED_LABEL = "traefik.enable"
_TRAEFIK_NAME = re.compile(r"[a-z0-9-]{1,63}")  # the provider constraint of traefik.yml
_NOT_FOUND_BODY = b"404 page not found\n"
_NOT_FOUND = 404
_PRIORITY = 1_000_000
VERIFY_SECONDS = 15.0
_POLL_SECONDS = 0.5
_REQUEST_SECONDS = 3.0
_TEMPLATE = (
    "# Written by `edge stop {unit}` from ~/.local/state/edge/stopped/{unit}; "
    "removed by `edge start {unit}`. Generated, git-ignored.\n"
    """\
http:
  routers:
{routers}  services:
    switch-{unit}-deny:
      loadBalancer:
        passHostHeader: false
        servers:
          - url: http://{address}
"""
)
_ROUTER = """\
    switch-{unit}-{name}:
      entryPoints: [edge]
      rule: Host(`{name}.{{{{ env "EDGE_DOMAIN" }}}}`)
      priority: {priority}
      service: switch-{unit}-deny
"""


class BarrierError(Exception):
    """The barrier cannot be written, removed or proven; the message is one line."""


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes

    @property
    def is_barrier(self) -> bool:
        return self.status == _NOT_FOUND and self.body == _NOT_FOUND_BODY


Fetch = Callable[[str, str], Answer]  # (entrypoint address, Host) -> the answer to GET /


def http_fetch(address: str, host: str) -> Answer:
    connection = http.client.HTTPConnection(address, timeout=_REQUEST_SECONDS)
    try:
        connection.request("GET", "/", headers={"Host": host})
        response = connection.getresponse()
        return Answer(response.status, response.read())
    finally:
        connection.close()


@dataclass(frozen=True)
class Verifier:
    """How a barrier is proven: the request, and how long it is polled."""

    fetch: Fetch = http_fetch
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    seconds: float = VERIFY_SECONDS

    def unreached(self, address: str, hosts: Iterable[str], *, barrier: bool) -> str | None:
        """The first host that does not (or, for `barrier=False`, still does) answer the barrier's
        404 when the time is up; None when every host does what is wanted."""
        deadline = self.clock() + self.seconds
        for host in hosts:
            while not self._reached(address, host, barrier):
                if self.clock() >= deadline:
                    return host
                self.sleep(_POLL_SECONDS)
        return None

    def _reached(self, address: str, host: str, barrier: bool) -> bool:
        try:
            return self.fetch(address, host).is_barrier is barrier
        except OSError:
            return False


def routed_names(config: Mapping[str, Any], services: Iterable[str]) -> tuple[str, ...]:
    """The `edge.name` of each service Traefik routes, as Compose resolved it, once each."""
    names: dict[str, None] = {}
    for service in services:
        labels = config["services"][service].get("labels", {})
        name = labels.get(EDGE_NAME_LABEL, "")
        if labels.get(_ROUTED_LABEL) in GO_TRUE_SPELLINGS and _TRAEFIK_NAME.fullmatch(name):
            names[name] = None
    return tuple(names)


def _domain(config: Mapping[str, Any]) -> str:
    """`EDGE_DOMAIN` as Compose resolves it for Traefik, so the barrier follows Traefik's own."""
    try:
        return config["services"]["traefik"]["environment"]["EDGE_DOMAIN"]
    except (KeyError, TypeError) as error:
        raise BarrierError("compose.yaml gives traefik no EDGE_DOMAIN") from error


def edge_address(checkout: Path) -> str:
    """`entryPoints.edge.address` of `traefik.yml`, the one owner of where Traefik listens."""
    try:
        document = yaml.safe_load((checkout / TRAEFIK_FILE).read_text())
        return document["entryPoints"]["edge"]["address"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as error:
        raise BarrierError(f"cannot read the edge address of {TRAEFIK_FILE}") from error


PROVEN_OVER_HTTP = Verifier()


@dataclass(frozen=True)
class Barrier:
    """The barrier of one unit; with no host behind Traefik it has no file and does nothing."""

    checkout: Path
    unit: str
    names: tuple[str, ...]
    domain: str
    verifier: Verifier = PROVEN_OVER_HTTP

    @classmethod
    def of_unit(
        cls,
        config: Mapping[str, Any],
        services: Iterable[str],
        checkout: Path,
        unit: str,
        verifier: Verifier = PROVEN_OVER_HTTP,
    ) -> Self:
        names = routed_names(config, services)
        return cls(checkout, unit, names, _domain(config) if names else "", verifier)

    @property
    def path(self) -> Path:
        return self.checkout / DYNAMIC_DIRECTORY / f"switch-{self.unit}.yml"

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(f"{name}.{self.domain}" for name in self.names)

    def close(self) -> None:
        """Write the barrier and prove that every host answers 404 for it."""
        if not self.names:
            return
        address = edge_address(self.checkout)
        self._write(address)
        self._prove(address, barrier=True)

    def open(self) -> None:
        """Remove the barrier and prove that no host answers its 404 any more."""
        self._remove()
        if self.names:
            self._prove(edge_address(self.checkout), barrier=False)

    def present(self) -> bool:
        return self.path.exists()

    def _prove(self, address: str, *, barrier: bool) -> None:
        unreached = self.verifier.unreached(address, self.hosts, barrier=barrier)
        if unreached:
            within = f"within {self.verifier.seconds:g} s"
            if barrier:
                raise BarrierError(
                    f"Traefik did not answer 404 for {unreached} {within} (the barrier is written)"
                )
            raise BarrierError(
                f"Traefik did not open {unreached} {within} (the barrier is removed)"
            )

    def _write(self, address: str) -> None:
        routers = "".join(
            _ROUTER.format(unit=self.unit, name=name, priority=_PRIORITY) for name in self.names
        )
        text = _TEMPLATE.format(unit=self.unit, routers=routers, address=address)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        try:
            temporary.write_text(text)
            os.replace(temporary, self.path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise BarrierError(f"cannot write {self.path.name} ({error.strerror})") from error

    def _remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise BarrierError(f"cannot remove {self.path.name} ({error.strerror})") from error
