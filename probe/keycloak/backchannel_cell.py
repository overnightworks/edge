"""Cell BC1 of probe.py, and the back-channel settings of the app clients (cell T8): Keycloak calls
songmaker's, HopIn's and presentator's logout over an internal network and trusts `Forwarded` only
from the keycloak network, so a forged one from a container on the back-channel network is not
believed. What else that container reaches is recorded, not judged."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml

CLIENT_SERVICE = "probe-backchannel-client"
CLIENT_SCRIPT = "/cell/backchannel_client.py"
FORGED_ADDRESS = "203.0.113.77"
USER = "probe-forged"
LOGOUT_URL_KEY = "backchannel.logout.url"
SESSION_REQUIRED_KEY = "backchannel.logout.session.required"
REFRESH_TOKENS_KEY = "use.refresh.tokens"
REALM_FILE = Path(__file__).resolve().parents[2] / "keycloak" / "realm.yaml"
SETTING_KEYS = (LOGOUT_URL_KEY, SESSION_REQUIRED_KEY, REFRESH_TOKENS_KEY)


def _declared_attributes() -> dict[str, dict[str, str]]:
    clients = yaml.safe_load(REALM_FILE.read_text())["clients"]
    return {client["clientId"]: client.get("attributes", {}) for client in clients}


class Stack(Protocol):
    def compose(self, *args: str, stdin: str | None = None) -> Any: ...


class Admin(Protocol):
    def create_user(self, realm: str, username: str, password: str) -> str: ...

    def get(self, path: str) -> Any: ...


class Probe(Protocol):
    stack: Stack

    def keep_secret(self, name: str, value: str) -> str: ...


def ruled_settings(client_id: str) -> dict[str, str]:
    """The back-channel attributes the realm declaration the stack applies gives the client."""
    declared = _declared_attributes().get(client_id, {})
    return {key: declared[key] for key in SETTING_KEYS if key in declared}


def wrong_logout_settings(client_id: str, attributes: Mapping[str, str]) -> dict[str, str | None]:
    """What differs from the ruled settings: the logout address, its session requirement and the
    refresh-token line of the apps that have them, none of the three for any other app."""
    ruled = dict.fromkeys(SETTING_KEYS) | ruled_settings(client_id)
    return {
        key: attributes.get(key) for key, value in ruled.items() if attributes.get(key) != value
    }


def refresh_token_follows_the_ruling(
    client_id: str, tokens: Mapping[str, Any], expect: Callable[[bool, str], None]
) -> None:
    """Cell T8: a client gets a refresh token exactly when its ruled settings do not turn it off."""
    issued = ruled_settings(client_id).get(REFRESH_TOKENS_KEY) != "false"
    expect(
        ("refresh_token" in tokens) == issued,
        f"{client_id}'s token response {'lacks' if issued else 'has'} a refresh token",
    )


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
    expect(seen["token"] == "400", f"the wrong password answered {seen['token']}, not 400")  # noqa: S105 - compares a status code and holds no credential
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
