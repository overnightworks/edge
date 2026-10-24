#!/usr/bin/env python3
"""The capture verifier, songmaker-web's stand-in on the MCP network (docs/agent-service.md).

It runs in the service image, started by `live_signer.py` (or by a probe), serves plain http on 8080
and judges the first POST to /mcp: the bearer capability against the one public key it trusts, the
header `kid`, the one tool the capability names, and the Host. It answers 401 with an empty body,
prints one line (`verified` or `refused: <reason>`) and ends. Every other request gets 404 and is
not judged. Neither the capability nor any part of it is printed or logged.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, Protocol
from urllib.parse import urlsplit

PORT = 8080
SCRIPT_MOUNT = "/opt/mcp_signer.py"
PUBLIC_KEY_MOUNT = "/opt/public.pem"
MCP_PATH = "/mcp"
LISTENING_LINE = "listening"
VERIFIED = "verified"
REFUSED_PREFIX = "refused: "


class Refusal(StrEnum):
    """What this module refuses on its own; the verifier's own refusals pass through as they are."""

    NO_BEARER = "no bearer"
    KID = "kid"
    TOOLS = "tools"
    HOST = "host"


class Claims(Protocol):
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Expectation:
    """`verify` gives the claims of a sound capability, else the verifier's refusal."""

    verify: Callable[[str], Claims | str]
    key_id: str
    host: str
    tool: str


def header_kid(token: str) -> object:
    header = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))).get("kid")


def judge(expected: Expectation, authorization: str | None, host: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or not token:
        return REFUSED_PREFIX + Refusal.NO_BEARER
    outcome = expected.verify(token)
    if isinstance(outcome, str):
        return REFUSED_PREFIX + outcome
    if header_kid(token) != expected.key_id:
        return REFUSED_PREFIX + Refusal.KID
    if tuple(outcome.tools) != (expected.tool,):
        return REFUSED_PREFIX + Refusal.TOOLS
    if host != expected.host:
        return REFUSED_PREFIX + Refusal.HOST
    return VERIFIED


class CaptureServer(HTTPServer):
    def __init__(self, address: tuple[str, int], expected: Expectation) -> None:
        super().__init__(address, CaptureHandler)
        self.expected = expected
        self.verdict: str | None = None


class CaptureHandler(BaseHTTPRequestHandler):
    server: CaptureServer

    def do_POST(self) -> None:
        if self.path != MCP_PATH:
            return self.answer(HTTPStatus.NOT_FOUND)
        self.server.verdict = judge(
            self.server.expected, self.headers.get("Authorization"), self.headers.get("Host")
        )
        self.answer(HTTPStatus.UNAUTHORIZED)

    def not_judged(self) -> None:
        self.answer(HTTPStatus.NOT_FOUND)

    def __getattr__(self, name: str) -> Callable[[], None]:
        """Every method but POST, even TRACE or CONNECT, is answered 404 and not judged."""
        if name.startswith("do_"):
            return self.not_judged
        raise AttributeError(name)

    def answer(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *arguments: object) -> None:
        """Silent: a request line is nobody's business here."""


def serve(server: CaptureServer, out: IO[str]) -> str:
    while server.verdict is None:
        server.handle_request()
    print(server.verdict, file=out, flush=True)
    return server.verdict


def main(argv: list[str]) -> int:
    origin, tool = argv
    # Only the service image has the runtime packages; the decisions above need none of them.
    from agent_runtime_client.mcp_server import CapabilityVerificationKey, verify_capability
    from agent_runtime_service.signing import key_id

    key = CapabilityVerificationKey.from_pem(Path(PUBLIC_KEY_MOUNT).read_bytes())
    expected = Expectation(
        lambda token: verify_capability(token, key=key, origin=origin, now=time.time()),
        key_id(key.public_key),
        urlsplit(origin).netloc,
        tool,
    )
    server = CaptureServer(("0.0.0.0", PORT), expected)
    print(LISTENING_LINE, flush=True)
    serve(server, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
