"""Cell LA1 of probe.py: which Keycloak console actions send a back-channel logout. A sign-out of
the user's sessions runs first as the control and gates the measurement: it must succeed and its
logout must arrive. Then, for each action (disable the user, delete the user, remove the user from
a group), a fresh user signs in to a client with HopIn's ruled logout and refresh settings, the
admin call runs, and probe-rp, the test relying party the client calls, says whether a logout token
for that user arrived. The result describes Keycloak's behaviour; it proves nothing about an app's
own erasure."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, Protocol

import backchannel_cell

REALM = "hallucinai"
CLIENT_ID = "probe-logout-actions"
TOKEN_PATH = f"/realms/{REALM}/protocol/openid-connect/token"
LOGOUT_URL = "http://probe-rp:8000/backchannel-logout"  # NOSONAR(S5332) probe network, ruled shape
ADMIN_SUCCESS = (200, 204)


class Answer(Protocol):
    status: int


class Admin(Protocol):
    def create_user(self, realm: str, username: str, password: str) -> str: ...

    def create(self, path: str, representation: dict[str, Any]) -> str: ...

    def get(self, path: str) -> Any: ...

    def call(self, method: str, path: str, json_body: Any = None) -> Answer: ...

    def join_group(self, user_id: str, group_path: str) -> None: ...

    def group_paths(self, user_id: str) -> list[str]: ...


class Probe(Protocol):
    def through_tunnel(self, method: str, path: str, **kwargs: Any) -> Answer: ...

    def tokens_from(self, holder: str, response: Answer) -> dict[str, Any]: ...

    def keep_secret(self, name: str, value: str) -> str: ...

    def admin_now(self) -> Admin: ...

    logout_timeout_seconds: int

    def logout_record(self, user_id: str) -> dict[str, Any] | None: ...

    def held(self, proof: str, detail: str) -> None: ...


def register_client(probe: Probe, admin: Admin) -> str:
    """The client carries HopIn's ruled logout and refresh settings; direct grants make sessions."""
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
                **backchannel_cell.ruled_settings("hopin"),
                backchannel_cell.LOGOUT_URL_KEY: LOGOUT_URL,
            },
        },
    )
    secret = admin.get(f"/{REALM}/clients/{client_uuid}/client-secret")["value"]
    return probe.keep_secret(f"{CLIENT_ID} client secret", secret)


def new_signed_in_user(probe: Probe, client_secret: str, action: str) -> tuple[Admin, str, str]:
    """Each action waits out the logout timeout, which outlives the admin token: every action
    takes a fresh one."""
    admin = probe.admin_now()
    username = "probe-" + action.replace(" ", "-")
    password = probe.keep_secret(f"{username} password", secrets.token_urlsafe(16))
    user_id = admin.create_user(REALM, username, password)
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
    return admin, username, user_id


def prove(
    probe: Probe,
    expect: Callable[[bool, str], None],
    member_group: str,
) -> None:
    client_secret = register_client(probe, probe.admin_now())
    control = "control sign out"
    admin, username, user_id = new_signed_in_user(probe, client_secret, control)
    status = admin.call("POST", f"/{REALM}/users/{user_id}/logout").status
    expect(status in ADMIN_SUCCESS, f"the control's admin call answered {status}")
    expect(
        probe.logout_record(user_id) is not None,
        f"the control's logout did not arrive within {probe.logout_timeout_seconds} s: "
        "nothing below measures",
    )
    probe.held("LA1", f"{control}, signed in to {CLIENT_ID}: logout seen")
    group_id = probe.admin_now().get(f"/{REALM}/group-by-path{member_group}")["id"]
    actions = {
        "disable user": ("PUT", "/users/{user}", {"enabled": False}),
        "delete user": ("DELETE", "/users/{user}", None),
        "remove group membership": ("DELETE", "/users/{user}/groups/" + group_id, None),
    }
    for action, (method, path, body) in actions.items():
        admin, username, user_id = new_signed_in_user(probe, client_secret, action)
        if action == "remove group membership":
            admin.join_group(user_id, member_group)
            expect(
                member_group in admin.group_paths(user_id),
                f"{username} is not in {member_group} before the removal",
            )
        status = admin.call(method, f"/{REALM}" + path.format(user=user_id), body).status
        if status not in ADMIN_SUCCESS:
            outcome = f"action failed (the admin call answered {status})"
        elif probe.logout_record(user_id) is not None:
            outcome = "logout seen"
        else:
            outcome = f"no matching logout was observed within {probe.logout_timeout_seconds} s"
        probe.held("LA1", f"{action}, signed in to {CLIENT_ID}: {outcome}")
