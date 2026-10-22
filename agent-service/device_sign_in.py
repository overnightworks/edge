"""The live smoke's sign-in: the person confirms a link and a code (OAuth device grant, RFC 8628)
and the smoke carries on by itself, so nothing is copied back (edge#297, after edge#184 D6).

Keycloak accepts a device request for the `songmaker` client only with its client secret, which
lives on the operator's machine alone. No token and no secret is printed. Only the standard
library is used.
"""

from __future__ import annotations

import base64
import hashlib
import http
import http.client
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping

from smoke import Tokens

OIDC = "https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect"
CLIENT = "songmaker"
TOKEN_TIMEOUT_SECONDS = 30
# Cloudflare in front of the token endpoint refuses the default Python-urllib agent (error 1010)
USER_AGENT = "edge-live-smoke/1"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN_SECONDS = 5  # RFC 8628, 3.5: the interval grows by at least five seconds


class SignInFailed(Exception):
    """The sign-in cannot go on; the message is the one line to print, free of any secret."""


def post_form(
    path: str, form: Mapping[str, str], client_secret: str, urlopen=urllib.request.urlopen
) -> tuple[int, dict]:
    """The status and the JSON object Keycloak answered, whether it accepted or refused."""
    body = urllib.parse.urlencode({**form, "client_id": CLIENT, "client_secret": client_secret})
    request = urllib.request.Request(
        f"{OIDC}/{path}", body.encode(), headers={"User-Agent": USER_AGENT}
    )
    try:
        try:
            with urlopen(request, timeout=TOKEN_TIMEOUT_SECONDS) as answer:
                status, document = http.HTTPStatus.OK, json.load(answer)
        except urllib.error.HTTPError as refusal:
            status, document = refusal.code, json.load(refusal)
    except (OSError, http.client.HTTPException, ValueError):
        raise SignInFailed(f"the {path} endpoint gave no answer to read") from None
    if not isinstance(document, dict):
        raise SignInFailed(f"the {path} endpoint gave no answer to read")
    return status, document


def access_token(
    form: Mapping[str, str], client_secret: str, urlopen=urllib.request.urlopen
) -> str:
    status, document = post_form("token", form, client_secret, urlopen)
    if status != 200:
        raise SignInFailed(f"the token endpoint answered {status}")
    token = document.get("access_token")
    if not isinstance(token, str):
        raise SignInFailed("the token endpoint gave no access token")
    return token


def pkce_pair() -> tuple[str, str]:
    """The client requires PKCE (S256) for every flow, the device grant included."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def device_authorization(client_secret: str, challenge: str, urlopen) -> dict:
    form = {"scope": "openid", "code_challenge": challenge, "code_challenge_method": "S256"}
    status, document = post_form("auth/device", form, client_secret, urlopen)
    keys = ("device_code", "user_code", "verification_uri_complete", "expires_in", "interval")
    if status != 200 or not all(key in document for key in keys):
        raise SignInFailed(f"the device endpoint answered {status} and started no sign-in")
    return document


def person_token(
    authorization: Mapping,
    verifier: str,
    client_secret: str,
    urlopen,
    pause: Callable[[float], None],
) -> str:
    interval = authorization["interval"]
    waited = 0
    while waited <= authorization["expires_in"]:
        pause(interval)
        waited += interval
        status, document = post_form(
            "token",
            {
                "grant_type": DEVICE_GRANT,
                "device_code": authorization["device_code"],
                "code_verifier": verifier,
            },
            client_secret,
            urlopen,
        )
        if status == 200 and isinstance(document.get("access_token"), str):
            return document["access_token"]
        match document.get("error"):
            case "authorization_pending":
                continue
            case "slow_down":
                interval += SLOW_DOWN_SECONDS
            case "access_denied":
                raise SignInFailed("the sign-in was refused in the browser (access_denied)")
            case "expired_token":
                break
            case _:
                raise SignInFailed(f"the token endpoint answered {status}")
    raise SignInFailed("the code ran out before it was confirmed; run the smoke again")


def sign_in(
    client_secret: str,
    urlopen=urllib.request.urlopen,
    pause: Callable[[float], None] = time.sleep,
    open_link: Callable[[str], object] = webbrowser.open,
) -> Tokens:
    verifier, challenge = pkce_pair()
    authorization = device_authorization(client_secret, challenge, urlopen)
    link = authorization["verification_uri_complete"]
    print(f"Open {link} and confirm the code {authorization['user_code']}", file=sys.stderr)
    open_link(link)  # best effort: over SSH the link opens on any other device
    person = person_token(authorization, verifier, client_secret, urlopen, pause)
    workload = access_token({"grant_type": "client_credentials"}, client_secret, urlopen)
    return Tokens(person=person, workload=workload)
