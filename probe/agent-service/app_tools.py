"""The fake app of the containment cells (probe/agent-service/containment.py): an MCP endpoint
in the JSON-only profile the agent service's client speaks, on TLS, in the service image.

It offers `step` (with a description), `bare` (without one) and `extra`. The host writes the mode
file: in `canary` mode `step`'s description carries the third segment of the bearer, the run
capability's signature, which is what the relay must refuse; in `relay` mode it is plain text.
Per request it prints one line of a closed vocabulary and never the bearer.

Only the standard library of the service image is used.
"""

from __future__ import annotations

import http.server
import json
import ssl
import sys
from pathlib import Path

PORT = 8444
CERTIFICATE = "/app/cert.pem"
PRIVATE_KEY = "/app/key.pem"
MODE_FILE = Path("/mode/mode")
CANARY_MODE = "canary"
PROTOCOL_VERSION = "2025-11-25"
SESSION_ID = "probe-session"
SIGNATURE_SEGMENT = 2
KNOWN_METHODS = {"initialize", "notifications/initialized", "tools/list"}
OTHER_METHOD = "other"
PLAIN_DESCRIPTION = "Takes one step."
NO_SCHEMA = {"type": "object", "properties": {}}


def bearer_signature(authorization: str | None) -> str | None:
    """The third segment of the bearer, or None when the request carries no bearer of three."""
    scheme, _, token = (authorization or "").partition(" ")
    segments = token.split(".")
    if scheme != "Bearer" or len(segments) != SIGNATURE_SEGMENT + 1:
        return None
    return segments[SIGNATURE_SEGMENT]


def canary_text(signature: str | None) -> str | None:
    """The signature to plant in `step`'s description: only in canary mode, only with a bearer."""
    in_canary_mode = MODE_FILE.read_text(encoding="utf-8").strip() == CANARY_MODE
    return signature if in_canary_mode else None


def offered_tools(planted: str | None) -> list[dict]:
    return [
        {"name": "step", "description": planted or PLAIN_DESCRIPTION, "inputSchema": NO_SCHEMA},
        {"name": "bare", "inputSchema": NO_SCHEMA},
        {"name": "extra", "description": "Takes an extra step.", "inputSchema": NO_SCHEMA},
    ]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        """The access log of http.server is not the closed vocabulary below."""

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_error(404)
            return
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = message["method"] if message["method"] in KNOWN_METHODS else OTHER_METHOD
        signature = bearer_signature(self.headers.get("Authorization"))
        planted = canary_text(signature) if method == "tools/list" else None
        tools = offered_tools(planted) if method == "tools/list" else []
        names = ",".join(tool["name"] for tool in tools) or "-"
        print(
            f"method={method} offered={names} canary={int(planted is not None)} "
            f"bearer={int(signature is not None)}",
            flush=True,
        )
        self.answer(method, message.get("id"), tools)

    def answer(self, method: str, request_id: object, tools: list[dict]) -> None:
        if method == "notifications/initialized":
            self.reply(202, b"")
            return
        results = {
            "initialize": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-app", "version": "1"},
            },
            "tools/list": {"tools": tools},
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


def main() -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTIFICATE, PRIVATE_KEY)
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
