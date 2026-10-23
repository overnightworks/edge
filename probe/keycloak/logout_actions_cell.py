"""Cell LA1 of probe.py: which Keycloak console actions send a back-channel logout. For each
action (disable the user, delete the user, remove the user from a group) a fresh user signs in to a
client with hopin's ruled logout settings, the admin call runs, and probe-rp, the test relying
party the client calls, says whether a logout token for that user arrived. The result describes
Keycloak's behaviour; it proves nothing about an app's own erasure."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from typing import Any, Protocol

REALM = "hallucinai"
CLIENT_ID = "probe-logout-actions"
TOKEN_PATH = f"/realms/{REALM}/protocol/openid-connect/token"
LOGOUT_URL = "http://probe-rp:8000/backchannel-logout"  # NOSONAR(S5332) probe network, ruled shape
GROUP_PATH = "/apps/songmaker"
LOGOUT_LINE = "backchannel-logout "
ADMIN_SUCCESS = (200, 204)


class Answer(Protocol):
    status: int
    headers: Any

    def json(self) -> Any: ...


class Stack(Protocol):
    def logs(self, service: str) -> str: ...


class Admin(Protocol):
    def create_user(self, realm: str, username: str, password: str) -> str: ...

    def create(self, path: str, representation: dict[str, Any]) -> str: ...

    def get(self, path: str) -> Any: ...

    def call(self, method: str, path: str, json_body: Any = None) -> Answer: ...


class Probe(Protocol):
    stack: Stack

    def through_tunnel(self, method: str, path: str, **kwargs: Any) -> Answer: ...

    def tokens_from(self, holder: str, response: Answer) -> dict[str, Any]: ...

    def keep_secret(self, name: str, value: str) -> str: ...

    def admin_now(self) -> Admin: ...

    def held(self, proof: str, detail: str) -> None: ...


def register_client(probe: Probe, admin: Admin) -> str:
    """The client carries hopin's ruled logout settings; direct grants only give it sessions."""
    client_uuid = admin.create(
        f"/{REALM}/clients",
        {
            "clientId": CLIENT_ID,
            "enabled": True,
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": False,
            "frontchannelLogout": False,
            "attributes": {
                "backchannel.logout.url": LOGOUT_URL,
                "backchannel.logout.session.required": "false",
            },
        },
    )
    secret = admin.get(f"/{REALM}/clients/{client_uuid}/client-secret")["value"]
    return probe.keep_secret(f"{CLIENT_ID} client secret", secret)


def logout_arrived(probe: Probe, user_id: str, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        for line in probe.stack.logs("probe-rp").splitlines():
            if line.startswith(LOGOUT_LINE) and (
                json.loads(line.removeprefix(LOGOUT_LINE))["sub"] == user_id
            ):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def prove(probe: Probe, expect: Callable[[bool, str], None], timeout_seconds: int) -> None:
    """Each action waits out the logout timeout, which outlives the admin token: every action
    takes a fresh one."""
    admin = probe.admin_now()
    client_secret = register_client(probe, admin)
    group_id = admin.get(f"/{REALM}/group-by-path{GROUP_PATH}")["id"]
    actions = {
        "sign out (control)": ("POST", "/users/{user}/logout", None),
        "disable user": ("PUT", "/users/{user}", {"enabled": False}),
        "delete user": ("DELETE", "/users/{user}", None),
        "remove group membership": ("DELETE", "/users/{user}/groups/" + group_id, None),
    }
    for action, (method, path, body) in actions.items():
        admin = probe.admin_now()
        username = "probe-" + action.replace(" ", "-")
        password = probe.keep_secret(f"{username} password", secrets.token_urlsafe(16))
        user_id = admin.create_user(REALM, username, password)
        if action == "remove group membership":
            admin.call("PUT", f"/{REALM}/users/{user_id}/groups/{group_id}")
            member_of = [group["id"] for group in admin.get(f"/{REALM}/users/{user_id}/groups")]
            expect(group_id in member_of, f"{username} is not in {GROUP_PATH} before the removal")
        grant = probe.through_tunnel(
            "POST",
            TOKEN_PATH,
            form={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "client_secret": client_secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        probe.tokens_from(username, grant)
        status = admin.call(method, f"/{REALM}" + path.format(user=user_id), body).status
        if status not in ADMIN_SUCCESS:
            outcome = f"action failed (the admin call answered {status})"
        elif logout_arrived(probe, user_id, timeout_seconds):
            outcome = "logout seen"
        else:
            outcome = f"logout not seen within {timeout_seconds} s"
        probe.held("LA1", f"{action}, signed in to {CLIENT_ID}: {outcome}")
