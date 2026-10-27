"""The fake app of the containment cells (probe/agent-service/containment.py): an MCP endpoint
in the JSON-only profile the agent service's client speaks, on TLS, in the service image.

It offers `step` (with a description), `bare` (without one) and `extra`. The host writes the mode
file: in `canary` mode `step`'s description carries the third segment of the bearer, the run
capability's signature, which is what the relay must refuse; in `relay` mode it is plain text; in
`hold` mode a call of `step` is held, without an answer, until its connection closes.
Per request it prints one line of a closed vocabulary and never the bearer.

Only the standard library of the service image is used.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import ssl
import sys
import time
from pathlib import Path
from typing import IO

PORT = 8444
CERTIFICATE = "/app/cert.pem"
PRIVATE_KEY = "/app/key.pem"
MODE_FILE = Path("/mode/mode")
CANARY_MODE = "canary"
HOLD_MODE = "hold"
PROTOCOL_VERSION = "2025-11-25"
SESSION_ID = "probe-session"
SIGNATURE_SEGMENT = 2
LIST_TOOLS_METHOD = "tools/list"
CALL_TOOL_METHOD = "tools/call"
HELD_TOOL = "step"
KNOWN_METHODS = {"initialize", "notifications/initialized", LIST_TOOLS_METHOD}
OTHER_METHOD = "other"
PLAIN_DESCRIPTION = "Takes one step."
NO_SCHEMA = {"type": "object", "properties": {}}
STEP_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}


def bearer_signature(authorization: str | None) -> str | None:
    """The third segment of the bearer, or None when the request carries no bearer of three."""
    scheme, _, token = (authorization or "").partition(" ")
    segments = token.split(".")
    if scheme != "Bearer" or len(segments) != SIGNATURE_SEGMENT + 1:
        return None
    return segments[SIGNATURE_SEGMENT]


def current_mode(mode_file: Path) -> str:
    return mode_file.read_text(encoding="utf-8").strip()


def canary_text(signature: str | None, mode_file: Path) -> str | None:
    """The signature to plant in `step`'s description: only in canary mode, only with a bearer."""
    return signature if current_mode(mode_file) == CANARY_MODE else None


def log_line(log: IO[str], line: str) -> None:
    """One write per line, so that threads cannot interleave two lines."""
    log.write(line + "\n")
    log.flush()


def called_tool(message: dict) -> str:
    """The name of the called tool when it is the held one, else `other`."""
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return HELD_TOOL if name == HELD_TOOL else "other"


def request_id_of(message: dict) -> int | None:
    """The id a client numbered its request with; only the number is echoed back."""
    request_id = message.get("id")
    return None if request_id is None else int(request_id)


def offered_tools(planted: str | None) -> list[dict]:
    return [
        {"name": "step", "description": planted or PLAIN_DESCRIPTION, "inputSchema": STEP_SCHEMA},
        {"name": "bare", "inputSchema": NO_SCHEMA},
        {"name": "extra", "description": "Takes an extra step.", "inputSchema": NO_SCHEMA},
    ]


class Handler(http.server.BaseHTTPRequestHandler):
    server: ThreadingServer

    def log_message(self, *_: object) -> None:
        """The access log of http.server is not the closed vocabulary below."""

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_error(404)
            return
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if (
            message["method"] == CALL_TOOL_METHOD
            and current_mode(self.server.mode_file) == HOLD_MODE
        ):
            self.hold_call(message)
            return
        method = message["method"] if message["method"] in KNOWN_METHODS else OTHER_METHOD
        signature = bearer_signature(self.headers.get("Authorization"))
        planted = (
            canary_text(signature, self.server.mode_file) if method == LIST_TOOLS_METHOD else None
        )
        tools = offered_tools(planted) if method == LIST_TOOLS_METHOD else []
        names = ",".join(tool["name"] for tool in tools) or "-"
        log_line(
            self.server.log,
            f"method={method} offered={names} canary={int(planted is not None)} "
            f"bearer={int(signature is not None)}",
        )
        self.answer(method, request_id_of(message), tools)

    def hold_call(self, message: dict) -> None:
        """A call of `step` gets no answer: the request ends when the peer closes its end."""
        tool = called_tool(message)
        if tool != HELD_TOOL:
            log_line(self.server.log, f"method={CALL_TOOL_METHOD} tool={tool}")
            self.answer(OTHER_METHOD, request_id_of(message), [])
            return
        log_line(self.server.log, f"method={CALL_TOOL_METHOD} tool={tool} held")
        self.read_until_closed()
        released = f"method={CALL_TOOL_METHOD} tool={tool} released at={time.time():.3f}"
        log_line(self.server.log, released)

    def read_until_closed(self) -> None:
        """Both the orderly end (nothing more to read) and a reset connection are the close."""
        try:
            while self.connection.recv(4096):
                continue
        except OSError:
            return

    def answer(self, method: str, request_id: int | None, tools: list[dict]) -> None:
        if method == "notifications/initialized":
            self.reply(202, b"")
            return
        results = {
            "initialize": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-app", "version": "1"},
            },
            LIST_TOOLS_METHOD: {"tools": tools},
        }
        document = {"jsonrpc": "2.0", "id": request_id}
        if method in results:
            document["result"] = results[method]
        else:
            document["error"] = {"code": -32601, "message": "Method not found"}
        self.reply(200, json.dumps(document).encode("utf-8"), session=method == "initialize")

    def reply(self, status: int, body: bytes, session: bool = False) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", SESSION_ID)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """A request held open does not keep another request from being read and logged."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], mode_file: Path, log: IO[str]) -> None:
        self.mode_file = mode_file
        self.log = log
        super().__init__(address, Handler)


class HttpsServer(ThreadingServer):
    """A threading HTTP server whose listening socket speaks TLS from the moment it is bound."""

    def __init__(
        self, address: tuple[str, int], mode_file: Path, log: IO[str], context: ssl.SSLContext
    ) -> None:
        self.tls_context = context
        super().__init__(address, mode_file, log)

    def server_bind(self) -> None:
        super().server_bind()  # NOSONAR(S5332) TLS: HttpsServer wraps its socket at bind
        self.socket = self.tls_context.wrap_socket(self.socket, server_side=True)


def main() -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTIFICATE, PRIVATE_KEY)
    server = HttpsServer(("0.0.0.0", PORT), MODE_FILE, sys.stdout, context)  # NOSONAR(S5332) TLS
    server.serve_forever()  # NOSONAR(S5332) TLS
    return 0


if __name__ == "__main__":
    sys.exit(main())
