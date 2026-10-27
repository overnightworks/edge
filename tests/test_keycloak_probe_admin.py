"""The keycloak probe's Admin: it mints a fresh token once when Keycloak answers 401."""

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import test_keycloak_probe

keycloak_probe = test_keycloak_probe.probe  # the fixture that loads probe.py


def answering(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> list[str | None]:
    """Makes the probe's http answer the statuses in order and returns the bearers it saw."""
    bearers: list[str | None] = []

    def http(_method: str, _url: str, **kwargs: Any) -> Any:
        bearers.append(kwargs["bearer"])
        return probe.Response(statuses[len(bearers) - 1], {}, b"{}")

    monkeypatch.setattr(probe, "http", http)
    return bearers


def admin_minting(probe: ModuleType) -> tuple[Any, list[int]]:
    minted: list[int] = []

    def mint(count: int) -> str:
        minted.append(count)
        return f"token-{count}"

    return probe.Admin(probe.AdminToken(mint), "http://keycloak"), minted


def test_expired_token_is_replaced_once_and_the_call_is_repeated(
    keycloak_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 of #456: a 401 mints one fresh token and repeats the call with it."""
    bearers = answering(keycloak_probe, monkeypatch, [401, 200])
    admin, minted = admin_minting(keycloak_probe)
    assert minted == [1]

    response = admin.call("GET", "/realm")

    assert response.status == 200
    assert minted == [1, 2]
    assert bearers == ["token-1", "token-2"]


def test_a_401_after_the_fresh_token_stays_the_failure(
    keycloak_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 of #456: a 401 after the fresh token is the failure, with no third mint."""
    answering(keycloak_probe, monkeypatch, [401] * 6)
    admin, minted = admin_minting(keycloak_probe)

    assert admin.call("GET", "/realm").status == 401
    assert minted == [1, 2]
    with pytest.raises(keycloak_probe.ProbeFailure, match="GET /realm answered 401"):
        admin.get("/realm")
    with pytest.raises(keycloak_probe.ProbeFailure, match="DELETE /realm answered 401"):
        admin.must("DELETE", "/realm")


def test_every_admin_mint_is_kept_as_a_secret_under_its_own_name(
    keycloak_probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant of #456: each admin token is registered for proof 7, the renewed one too."""
    minted = iter(["first-access", "second-access"])
    answers = iter([401, 200])

    def http(_method: str, url: str, **_kwargs: Any) -> Any:
        if url == "http://keycloak/token":
            return keycloak_probe.Response(
                200, {}, f'{{"access_token": "{next(minted)}"}}'.encode()
            )
        return keycloak_probe.Response(next(answers), {}, b"{}")

    monkeypatch.setattr(keycloak_probe, "http", http)
    stack = SimpleNamespace(master_token_url="http://keycloak/token", admin_url="http://keycloak")
    probe = keycloak_probe.Probe(stack)

    assert probe.admin_with_client("s").call("GET", "/realm").status == 200

    assert sorted(probe.secret_values.values()) == ["first-access", "second-access"]
