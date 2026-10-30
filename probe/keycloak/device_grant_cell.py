"""Cell DG1 of probe.py (#297): only songmaker allows the device grant, and Keycloak starts one only
for a request that carries songmaker's client secret, so a stranger cannot make the operator
confirm a link of their choosing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

DEVICE_CLIENT = "songmaker"
DEVICE_PATH = "/realms/hallucinai/protocol/openid-connect/auth/device"
REFUSED_STATUSES = (400, 401)
PKCE = {"code_challenge": "x" * 43, "code_challenge_method": "S256"}  # the app clients require PKCE


class Answer(Protocol):
    status: int
    text: str

    def json(self) -> Any: ...


class Probe(Protocol):
    def through_tunnel(self, method: str, path: str, **kwargs: Any) -> Answer: ...

    def held(self, proof: str, detail: str) -> None: ...


def prove(
    probe: Probe, client_id: str, client_secret: str, expect: Callable[[bool, str], None]
) -> None:
    """Reports DG1 for one app client."""

    def ask(**credentials: str) -> Answer:
        form = {"client_id": client_id, "scope": "openid", **PKCE, **credentials}
        return probe.through_tunnel("POST", DEVICE_PATH, form=form)

    def refused(answer: Answer, what: str) -> None:
        expect(
            answer.status in REFUSED_STATUSES and "device_code" not in answer.text,
            f"a device request for {client_id} {what} answered {answer.status}",
        )

    accepted = ask(client_secret=client_secret)
    if client_id != DEVICE_CLIENT:
        refused(accepted, "with its secret")
        probe.held("DG1", f"{client_id} refuses a device request even with its secret")
        return
    started = accepted.json() if accepted.status == 200 else {}
    missing = [key for key in ("device_code", "user_code") if not started.get(key)]
    expect(
        not missing,
        f"a device request for {client_id} with its secret answered {accepted.status} "
        f"without {' and '.join(missing)}",
    )
    refused(ask(), "without its secret")
    refused(ask(client_secret="not-the-secret"), "with a wrong secret")  # noqa: S106 - a wrong secret on purpose to prove the refusal
    probe.held(
        "DG1",
        f"{client_id} starts a device sign-in only with its secret (code lives "
        f"{started.get('expires_in')} s, poll every {started.get('interval')} s)",
    )
