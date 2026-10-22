"""Cell BC1 of probe.py, and the back-channel settings of the app clients (cell T8): Keycloak calls
songmaker's logout over an internal network and trusts `Forwarded` only from the keycloak network,
so a forged one from a container on the back-channel network is not believed. What else that
container reaches is recorded, not judged."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from typing import Any, Protocol

DOMAIN = "hallucinai.de"
CLIENT_SERVICE = "probe-backchannel-client"
CLIENT_SCRIPT = "/cell/backchannel_client.py"
FORGED_ADDRESS = "203.0.113.77"
USER = "probe-forged"
LOGOUT_URL_KEY = "backchannel.logout.url"
SESSION_REQUIRED_KEY = "backchannel.logout.session.required"
SONGMAKER_LOGOUT_URL = (
    f"http://songmaker.{DOMAIN}"  # NOSONAR(S5332) internal back-channel network, ruled shape
    ":8080/auth/backchannel-logout"
)


class Stack(Protocol):
    def compose(self, *args: str, stdin: str | None = None) -> Any: ...


class Admin(Protocol):
    def create_user(self, realm: str, username: str, password: str) -> str: ...

    def get(self, path: str) -> Any: ...


class Probe(Protocol):
    stack: Stack

    def keep_secret(self, name: str, value: str) -> str: ...


def wrong_logout_settings(client_id: str, attributes: Mapping[str, str]) -> dict[str, str | None]:
    """What differs from the ruled settings: songmaker's logout address without a session
    requirement, no logout address for any other app."""
    ruled = {LOGOUT_URL_KEY: None, SESSION_REQUIRED_KEY: None}
    if client_id == "songmaker":
        ruled = {LOGOUT_URL_KEY: SONGMAKER_LOGOUT_URL, SESSION_REQUIRED_KEY: "false"}
    return {
        key: attributes.get(key) for key, value in ruled.items() if attributes.get(key) != value
    }


def prove(
    probe: Probe, admin: Admin, client_secret: str, expect: Callable[[bool, str], None]
) -> str:
    """The line the probe reports when BC1 held."""
    password = probe.keep_secret("probe-forged password", secrets.token_urlsafe(16))
    user_id = admin.create_user("hallucinai", USER, password)
    form = {
        "grant_type": "password",
        "client_id": "probe-rp",
        "client_secret": client_secret,
        "username": USER,
        "password": "not-the-password",
    }
    ran = probe.stack.compose(
        "exec",
        "-T",
        CLIENT_SERVICE,
        "python",
        CLIENT_SCRIPT,
        FORGED_ADDRESS,
        stdin=json.dumps(form),
    )
    expect(ran.returncode == 0, f"the back-channel client failed:\n{ran.stderr[-1000:]}")
    seen = json.loads(ran.stdout)
    expect(seen["token"] == "400", f"the wrong password answered {seen['token']}, not 400")
    stored = [
        event
        for event in admin.get(f"/hallucinai/events?user={user_id}")
        if event["type"] == "LOGIN_ERROR"
    ]
    expect(len(stored) == 1, f"{USER} has {len(stored)} LOGIN_ERROR events, not one")
    expect(
        stored[0].get("ipAddress") == seen["address"],
        f"the forged address {FORGED_ADDRESS} was believed: the event carries "
        f"{stored[0].get('ipAddress')}, not the container's {seen['address']}",
    )
    return (
        f"a Forwarded {FORGED_ADDRESS} from {seen['address']} on the back-channel network is not "
        f"believed (the event keeps {seen['address']}); reach recorded, not judged: /admin/ "
        f"answers {seen['admin']}, the management port 9000 /health answers {seen['management']}"
    )
