"""Test relying party for the Keycloak probe: records every OIDC back-channel logout it receives.

It prints one line per logout token with the claims the probe checks; it never verifies the
signature, because it only proves that Keycloak reaches the RP over the compose network.
"""

import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

LOGOUT_PATH = "/backchannel-logout"
PORT = 8000


def token_claims(jwt: str) -> dict:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class BackchannelLogoutHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != LOGOUT_PATH:
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        logout_token = parse_qs(body).get("logout_token", [""])[0]
        if not logout_token:
            self.send_error(400, "no logout_token")
            return
        claims = token_claims(logout_token)
        record = {key: claims.get(key) for key in ("iss", "sub", "sid", "events")}
        print("backchannel-logout " + json.dumps(record, sort_keys=True), flush=True)  # noqa: T201 - this in-container program answers on standard output
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    # Plain HTTP on purpose: the throwaway probe network has no CA to issue this RP a certificate.
    HTTPServer(("", PORT), BackchannelLogoutHandler).serve_forever()  # NOSONAR(S5332)
