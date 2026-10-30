"""The fake provider of the LiteLLM probe (probe/agent-service/litellm.py): a TLS server that
stands in for api.anthropic.com or auth.hallucinai.de behind the gateway, inside the probe's
internal `internet` network. Standard library only; it runs in the service image.

    python -I -B fake_anthropic.py {provider|issuer}

It reads `/fake/cert.pem`, `/fake/key.pem` and `/fake/expect.json` (the scratch provider key, the
scratch service credential, the scratch identity strings and the answer marker; all of them
throwaway values). The `issuer` role answers `{}` to anything. The `provider` role answers an
Anthropic message with the marker and prints one JSON line per request on standard output: the
model, whether the key matched, and counts of what must not arrive here. It prints a verdict and
never a value of the request.
"""

import http.server
import json
import re
import ssl
import sys

PORT = 443
JWT = re.compile(rb"eyJ[\w-]+\.[\w-]+\.[\w-]+")
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def provider_record(expect: dict, request: http.server.BaseHTTPRequestHandler, body: bytes) -> dict:
    raw = str(request.headers).encode() + body
    try:
        model = json.loads(body)["model"]
    except (ValueError, KeyError, TypeError):
        model = None
    return {
        "path": request.path,
        "model": model,
        "key_ok": request.headers.get("x-api-key") == expect["provider_key"],
        "authorization": "authorization" in request.headers,
        "credential_bytes": expect["credential"].encode() in raw,
        "identity_hits": sum(raw.count(value.encode()) for value in expect["identity"]),
        "jwt_hits": len(JWT.findall(raw)),
        "uuid_hits": len(UUID.findall(raw)),
    }


def message(expect: dict, model: str | None) -> bytes:
    return json.dumps({
        "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": expect["marker"]}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode()  # fmt: skip


def handler(role: str, expect: dict) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            reply = b"{}"
            if role == "provider":
                record = provider_record(expect, self, body)
                print(json.dumps(record), flush=True)  # noqa: T201 - this in-container program prints its own request log
                reply = message(expect, record["model"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        do_GET = do_POST

        def log_message(self, *_: object) -> None:
            return

    return Handler


def main(role: str) -> None:
    with open("/fake/expect.json", encoding="utf-8") as file:
        expect = json.load(file)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler(role, expect))  # noqa: S104 - a container-internal listener on an internal network
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/fake/cert.pem", "/fake/key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1])
