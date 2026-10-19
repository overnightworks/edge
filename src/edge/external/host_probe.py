"""The host probe (#154): one unauthenticated `GET /` per probed host, to confirm what Cloudflare's
API said. Holds no token; `http.client` follows no redirect and reads no proxy variable. Nothing an
answer carries is ever quoted: a finding's wording comes from the typed answer, not from its text.
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
from urllib.parse import urljoin, urlsplit

from edge.external.cloudflare_read import LOOPBACK_HOSTNAMES

REQUEST_TIMEOUT_SECONDS = 10.0
USER_AGENT = "edge-host-check/1"
ACCESS_TEAM_SUFFIX = ".cloudflareaccess.com"
ACCESS_PATH = "/cdn-cgi/access"
HTTPS_PORT = 443
ACCESS_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ESCAPED_UNRESERVED = re.compile(r"%(2[dDeE]|3[0-9]|[46][1-9a-fA-F]|[57][0-9aA]|5[fF]|7[eE])")


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
    def ask(self, name: str) -> Answer | Unreachable: ...


ConnectionFactory = Callable[[str, float], http.client.HTTPConnection]

_ENCODED_SEPARATOR = re.compile(r"%(2[fF]|5[cC])")


def _connection_to_origin(origin: str | None) -> ConnectionFactory:
    if origin is None:
        return lambda name, timeout: http.client.HTTPSConnection(name, timeout=timeout)
    parts = urlsplit(origin)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTNAMES:
        raise ValueError("the probe origin is for loopback tests: an http URL on a loopback name")
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

    def ask(self, name: str) -> Answer | Unreachable:
        try:
            connection = self._connection(name, self._timeout)
        except (http.client.HTTPException, OSError) as failure:
            return Unreachable(_failure_kind(failure))
        try:
            return _exchange(connection, name)
        finally:
            _close(connection)


def _exchange(connection: http.client.HTTPConnection, name: str) -> Answer | Unreachable:
    try:
        connection.request("GET", "/", headers={"Host": name, "User-Agent": USER_AGENT})
        response = connection.getresponse()
        challenge = (response.getheader("cf-mitigated") or "").lower() == "challenge"
        return Answer(
            response.status,
            response.getheader("Location"),
            response.getheader("cf-ray") is not None,
            challenge,
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
    try:
        target = urlsplit(urljoin(f"https://{probed_host}/", answer.location))
        port, hostname = target.port, target.hostname
    except ValueError:
        return False
    if target.scheme != "https" or port not in (None, HTTPS_PORT) or hostname is None:
        return False
    if _hides_a_path_separator(target.path):
        return False
    host = hostname.removesuffix(".")
    if host.endswith(ACCESS_TEAM_SUFFIX):
        return True
    path = normalized_path(target.path)
    on_access_path = path == ACCESS_PATH or path.startswith(f"{ACCESS_PATH}/")
    return host == probed_host.lower().removesuffix(".") and on_access_path


def _hides_a_path_separator(path: str) -> bool:
    """A browser may read a backslash or an encoded slash as a separator the server did not."""
    return "\\" in path or _ENCODED_SEPARATOR.search(path) is not None


def normalized_path(path: str) -> str:
    """RFC 3986 6.2.2.2 (unreserved escapes decoded once), then 5.2.4 (dot segments removed)."""
    remaining = _ESCAPED_UNRESERVED.sub(lambda escape: chr(int(escape[1], 16)), path)
    output: list[str] = []
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith(("./", "/./")):
            remaining = remaining[2:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = remaining[3:]
            output = output[:-1]
        elif remaining == "/..":
            remaining = "/"
            output = output[:-1]
        elif remaining in (".", ".."):
            remaining = ""
        else:
            segment_end = remaining.find("/", 1)
            segment_end = len(remaining) if segment_end == -1 else segment_end
            output.append(remaining[:segment_end])
            remaining = remaining[segment_end:]
    return "".join(output)
