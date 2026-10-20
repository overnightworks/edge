"""The host probe (#154): one unauthenticated `GET` per probed host (`/` for the host check, a
host's `ready` path for the apply, #173), to confirm what Cloudflare's API said or that the
target answers. Holds no token; `http.client` follows no redirect and reads no proxy variable.
Nothing an answer carries is ever quoted: a finding's wording comes from the typed answer, not from
its text.
"""

import contextlib
import http.client
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from edge.external.cloudflare_read import LOOPBACK_HOSTNAMES

REQUEST_TIMEOUT_SECONDS = 10.0
USER_AGENT = "edge-host-check/1"
ACCESS_TEAM_SUFFIX = ".cloudflareaccess.com"
ACCESS_PATH = "/cdn-cgi/access"
ACCESS_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Only Cloudflare's canonical Access URL counts: a Location outside these two forms is never read
# further, because no two URL parsers agree on what a non-canonical one means.
_PATH_CHARACTERS = r"[A-Za-z0-9\-._~!$&'()*+,;=:@/]"
_QUERY_AND_FRAGMENT = r"(?:[?#][\s\S]*)?"
_ABSOLUTE_LOCATION = re.compile(
    rf"https://(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.?)(?::443)?"
    rf"(?P<path>/{_PATH_CHARACTERS}*){_QUERY_AND_FRAGMENT}"
)
_ROOT_RELATIVE_LOCATION = re.compile(rf"(?P<path>/(?!/){_PATH_CHARACTERS}*){_QUERY_AND_FRAGMENT}")


class FailureKind(StrEnum):
    DNS = "the name did not resolve"
    TLS = "the TLS handshake failed"
    REFUSED = "the connection was refused"
    DISCONNECTED = "the connection was reset or closed early"
    TIMEOUT = "the request timed out"
    MALFORMED = "the answer was malformed"
    OTHER = "connection failure"


@dataclass(frozen=True)
class Answer:
    status: int
    location: str | None
    has_cf_ray: bool
    challenge: bool


@dataclass(frozen=True)
class Unreachable:
    kind: FailureKind


class Prober(Protocol):
    def ask(self, name: str, path: str = "/") -> Answer | Unreachable: ...


ConnectionFactory = Callable[[str, float], http.client.HTTPConnection]


def _connection_to_origin(origin: str | None) -> ConnectionFactory:
    if origin is None:
        return lambda name, timeout: http.client.HTTPSConnection(name, timeout=timeout)
    parts = urlsplit(origin)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTNAMES:
        raise ValueError("the probe origin must be an http URL on a loopback name")
    return lambda _name, timeout: http.client.HTTPConnection(
        parts.hostname, parts.port, timeout=timeout
    )


class HttpProber:
    def __init__(
        self,
        origin: str | None = None,
        connection: ConnectionFactory | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._connection = connection or _connection_to_origin(origin)
        self._timeout = timeout

    def ask(self, name: str, path: str = "/") -> Answer | Unreachable:
        try:
            connection = self._connection(name, self._timeout)
        except (http.client.HTTPException, OSError) as failure:
            return Unreachable(_failure_kind(failure))
        try:
            return _exchange(connection, name, path)
        finally:
            _close(connection)


def _exchange(connection: http.client.HTTPConnection, name: str, path: str) -> Answer | Unreachable:
    try:
        connection.request("GET", path, headers={"Host": name, "User-Agent": USER_AGENT})
        response = connection.getresponse()
        return Answer(
            response.status,
            response.getheader("Location"),
            response.getheader("cf-ray") is not None,
            response.getheader("cf-mitigated") is not None,
        )
    except (http.client.HTTPException, OSError) as failure:
        return Unreachable(_failure_kind(failure))


def _close(connection: http.client.HTTPConnection) -> None:
    # Why: the verdict is already decided; a failed cleanup must not overturn it.
    with contextlib.suppress(OSError):
        connection.close()


def _failure_kind(failure: Exception) -> FailureKind:
    if isinstance(failure, socket.gaierror):
        return FailureKind.DNS
    if isinstance(failure, TimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(failure, ssl.SSLError):
        return FailureKind.TLS
    if isinstance(failure, ConnectionRefusedError):
        return FailureKind.REFUSED
    if isinstance(failure, (ConnectionError, http.client.IncompleteRead)):
        return FailureKind.DISCONNECTED
    if isinstance(failure, http.client.HTTPException):
        return FailureKind.MALFORMED
    return FailureKind.OTHER


def is_access_redirect(answer: Answer, probed_host: str) -> bool:
    """Whether the answer sends the visitor to Cloudflare Access; never follows the redirect."""
    if answer.status not in ACCESS_REDIRECT_STATUSES or answer.location is None or answer.challenge:
        return False
    absolute = _ABSOLUTE_LOCATION.fullmatch(answer.location)
    relative = _ROOT_RELATIVE_LOCATION.fullmatch(answer.location)
    canonical = absolute or relative
    if canonical is None or any(segment in (".", "..") for segment in canonical["path"].split("/")):
        return False
    probed = probed_host.lower().removesuffix(".")
    host = absolute["host"].lower().removesuffix(".") if absolute else probed
    if host.endswith(ACCESS_TEAM_SUFFIX):
        return True
    path = canonical["path"]
    return host == probed and (path == ACCESS_PATH or path.startswith(f"{ACCESS_PATH}/"))
