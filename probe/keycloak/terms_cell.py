"""Cells P1 and TC1-TC6 of probe.py (#775): the hallucinai realm asks every new account once to
accept the terms of use. A user made through the admin API carries the pending action, the page
shows the theme's words in the browser's language, Decline signs nobody in and removes the stored
time, Accept stores the time only the admin sees, and the other apps ask no second time. Users
made with `Admin.create_user` carry no pending action, so the other proofs reach their callbacks."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from test_rp import token_claims

REALM = "hallucinai"
ACTION = "TERMS_AND_CONDITIONS"
ATTRIBUTE = "terms_and_conditions"
FIRST_CLIENT = "songmaker"
USERNAME = "probe-terms-member"
REPOSITORY = Path(__file__).resolve().parents[2]
REALM_FILE = REPOSITORY / "keycloak" / "realm.yaml"
MESSAGES = REPOSITORY / "keycloak" / "themes" / "hallucinai" / "login" / "messages"
AUTH_PATH = f"/realms/{REALM}/protocol/openid-connect/auth"
TOKEN_PATH = f"/realms/{REALM}/protocol/openid-connect/token"
ACCOUNT_PATH = f"/realms/{REALM}/account"
LANGUAGES = (("de", "de"), ("", "en"))  # the browser's Accept-Language and the page's locale


class Answer(Protocol):
    status: int
    headers: dict[str, str]
    text: str

    def json(self) -> Any: ...


class Admin(Protocol):
    def get(self, path: str) -> Any: ...

    def must(self, method: str, path: str, json_body: Any = None) -> None: ...

    def create(self, path: str, representation: dict[str, Any]) -> str: ...

    def create_user(self, realm: str, username: str, password: str) -> str: ...


class Client(Protocol):
    client_id: str
    secret_file: str

    @property
    def callback(self) -> str: ...


class Probe(Protocol):
    admin: Admin
    secret_values: dict[str, str]

    def through_tunnel(self, method: str, path: str, **kwargs: Any) -> Answer: ...

    def through_tunnel_url(self, method: str, url: str, **kwargs: Any) -> Answer: ...

    def keep_secret(self, name: str, value: str) -> str: ...

    def tokens_from(self, holder: str, response: Answer) -> dict[str, Any]: ...

    def browser(self, name: str) -> Any: ...

    def held(self, proof: str, detail: str) -> None: ...

    def app_client_authorization_query(self, client: Any, **changes: str) -> str: ...

    def app_client_sign_in(
        self, client: Any, opener: Any, username: str, password: str, client_secret: str
    ) -> dict[str, Any]: ...


def without_pending_terms(admin: Admin, user_id: str) -> str:
    """Keycloak adds the default action to a new account whatever the request lists; it is
    cleared with the full representation, because a partial PUT risks wiping profile fields."""
    path = f"/{REALM}/users/{user_id}"
    admin.must("PUT", path, {**admin.get(path), "requiredActions": []})
    return user_id


class TermsPage(HTMLParser):
    """The heading, the terms text and the links in it, from a Keycloak page or a message."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.words: list[str] = []
        self.links: list[tuple[str | None, str | None]] = []
        self.depth = 0
        self.in_title = False

    @property
    def text(self) -> str:
        return " ".join("".join(self.words).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.in_title = self.in_title or tag == "h1"
        if not self.depth and attributes.get("id") == "kc-terms-text":
            self.depth = 1
        elif self.depth:
            self.depth += tag == "div"
            if tag == "a":
                self.links.append((attributes.get("href"), attributes.get("target")))

    def handle_endtag(self, tag: str) -> None:
        self.in_title = self.in_title and tag != "h1"
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title = " ".join(f"{self.title} {data}".split())
        if self.depth:
            self.words.append(data)


def message(locale: str, key: str) -> str:
    lines = (MESSAGES / f"messages_{locale}.properties").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))[key]


def page_of(html: str) -> TermsPage:
    page = TermsPage()
    page.feed(html)
    return page


def declared_priority() -> int:
    found = re.search(
        rf"alias: {ACTION}\n(?:    .*\n)*?    priority: (\d+)\n", REALM_FILE.read_text()
    )
    if not found:
        raise ValueError(f"realm.yaml declares no priority for {ACTION}")
    return int(found.group(1))


@dataclass(frozen=True)
class User:
    id: str
    name: str
    password: str


@dataclass(frozen=True)
class Step:
    """A browser that entered the password and the page it was given."""

    browser: Any
    page: Answer
    verifier: str
    headers: dict[str, str]


@dataclass(frozen=True)
class TermsCell:
    probe: Probe
    expect: Callable[[bool, str], None]
    form_with: Callable[[Any, str], tuple[str, dict[str, str]]]
    client: Client

    @property
    def admin(self) -> Admin:
        return self.probe.admin

    def user(self, user: User) -> dict[str, Any]:
        return self.admin.get(f"/{REALM}/users/{user.id}")

    def stored_time(self, user: User) -> int | None:
        values = self.user(user).get("attributes", {}).get(ATTRIBUTE)
        return int(values[0]) if values else None

    def ask_again(self, user: User) -> None:
        self.admin.must(
            "PUT", f"/{REALM}/users/{user.id}", {**self.user(user), "requiredActions": [ACTION]}
        )

    def begin(self, user: User, language: str = "") -> Step:
        """The authorization request and the password, up to the page the realm answers with."""
        name = self.client.client_id
        verifier = self.probe.keep_secret(f"{name} terms PKCE verifier", secrets.token_urlsafe(48))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = self.probe.app_client_authorization_query(
            self.client,
            code_challenge=challenge.rstrip(b"=").decode(),
            code_challenge_method="S256",
        )
        browser = self.probe.browser(f"{name} terms")
        headers = {"Accept-Language": language} if language else {}
        login = self.probe.through_tunnel(
            "GET", f"{AUTH_PATH}?{query}", opener=browser, headers=headers
        )
        action, fields = self.form_with(login, "username")
        page = self.probe.through_tunnel_url(
            "POST",
            action,
            form=fields | {"username": user.name, "password": user.password},
            opener=browser,
            headers=headers,
        )
        return Step(browser, page, verifier, headers)

    def asks(self, user: User, language: str = "") -> Step:
        step = self.begin(user, language)
        self.expect(
            step.page.status == 200 and "kc-terms-text" in step.page.text,
            f"the sign-in of {user.name} answered {step.page.status}, not the terms page",
        )
        return step

    def answer(self, step: Step, button: str) -> dict[str, list[str]]:
        action, inputs = self.form_with(step.page, button)
        answer = self.probe.through_tunnel_url(
            "POST", action, form={button: inputs[button]}, opener=step.browser, headers=step.headers
        )
        location = answer.headers.get("Location", "")
        self.expect(
            answer.status == 302 and location.startswith(f"{self.client.callback}?"),
            f"{button} answered {answer.status} with {location.partition('?')[0]!r}",
        )
        return parse_qs(urlsplit(location).query)

    def tokens_for(self, step: Step, user: User, code: str) -> dict[str, Any]:
        name = self.client.client_id
        secret = self.probe.secret_values[self.client.secret_file]
        code = self.probe.keep_secret(f"{name} terms authorization code", code)
        return self.probe.tokens_from(
            user.name,
            self.probe.through_tunnel(
                "POST",
                TOKEN_PATH,
                form={
                    "grant_type": "authorization_code",
                    "client_id": name,
                    "client_secret": secret,
                    "redirect_uri": self.client.callback,
                    "code": code,
                    "code_verifier": step.verifier,
                },
            ),
        )

    def action_is_default(self) -> None:
        action = self.admin.get(f"/{REALM}/authentication/required-actions/{ACTION}")
        priority = declared_priority()
        self.expect(
            action["enabled"] and action["defaultAction"] and action["priority"] == priority,
            f"{ACTION} is enabled {action['enabled']}, default {action['defaultAction']}, "
            f"priority {action['priority']} (declared {priority})",
        )
        self.probe.held("P1", f"{ACTION} is enabled, default and priority {priority} as declared")

    def new_user(self) -> User:
        password = self.probe.keep_secret(f"{USERNAME} password", secrets.token_urlsafe(16))
        created = self.admin.create(
            f"/{REALM}/users",
            {
                "username": USERNAME,
                "enabled": True,
                "email": f"{USERNAME}@probe.invalid",
                "emailVerified": True,
                "firstName": "Probe",
                "lastName": USERNAME,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
        )
        return User(created, USERNAME, password)

    def new_accounts_are_asked(self) -> User:
        user = self.new_user()
        pending = self.user(user)["requiredActions"]
        control = self.admin.create_user(
            REALM,
            f"{USERNAME}-control",
            self.probe.keep_secret("control password", secrets.token_urlsafe(16)),
        )
        unasked = self.admin.get(f"/{REALM}/users/{control}")["requiredActions"]
        self.expect(
            (pending, unasked) == ([ACTION], []),
            f"requiredActions: Admin.create {pending}, Admin.create_user {unasked}",
        )
        self.probe.held("TC1", f"a new account carries {ACTION}; Admin.create_user clears it")
        return user

    def page_shows_the_theme_words(self, user: User) -> None:
        kept = []
        for language, locale in LANGUAGES:
            step = self.asks(user, language)
            title, text = message(locale, "termsTitle"), message(locale, "termsText")
            page, wanted = page_of(step.page.text), page_of(f'<div id="kc-terms-text">{text}</div>')
            self.expect(
                (page.title, page.text, page.links) == (title, wanted.text, wanted.links),
                f"{locale} page: title {page.title!r}, text {page.text!r}, links {page.links}; "
                f"wanted {title!r}, {wanted.text!r}, {wanted.links}",
            )
            for button in ("accept", "cancel"):
                self.form_with(step.page, button)
            kept.append(f"{locale} {page.links}")
        self.probe.held("TC2", f"the page speaks the theme's words with accept and cancel: {kept}")

    def decline_signs_nobody_in(self, user: User) -> None:
        step = self.asks(user)
        query = self.answer(step, "cancel")
        sessions = self.admin.get(f"/{REALM}/users/{user.id}/sessions")
        self.expect(
            query.get("error") == ["access_denied"]
            and "code" not in query
            and not sessions
            and self.stored_time(user) is None
            and ACTION in self.user(user)["requiredActions"],
            f"decline: {query}, {len(sessions)} sessions, time {self.stored_time(user)}",
        )
        self.probe.held("TC3", "decline ends in access_denied, no session, no time, still pending")

    def accept_stores_a_time_only_the_admin_sees(self, user: User) -> int:
        step, started = self.asks(user), int(time.time())
        query = self.answer(step, "accept")
        stored = self.stored_time(user)
        tokens = self.tokens_for(step, user, query["code"][0])
        self.expect(
            stored is not None and started <= stored <= int(time.time()),
            f"accept stored {stored} for a window starting {started}",
        )
        self.expect(ACTION not in self.user(user)["requiredActions"], "accept left it pending")
        account = self.probe.through_tunnel(
            "GET",
            ACCOUNT_PATH,
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json",
            },
        )
        shown = [account.status, ATTRIBUTE in account.text]
        carried = [ATTRIBUTE in token_claims(tokens[kind]) for kind in ("id_token", "access_token")]
        self.expect(
            shown == [200, False] and not any(carried),
            f"the account REST answered {shown[0]}, time shown {shown[1]}; tokens carry {carried}",
        )
        known = self.user(user)
        forged = {**known.get("attributes", {}), ATTRIBUTE: [str(started + 1000)]}
        self.admin.must("PUT", f"/{REALM}/users/{user.id}", {**known, "attributes": forged})
        self.expect(self.stored_time(user) == stored, "an admin PUT changed the stored time")
        self.probe.held(
            "TC4",
            f"accept stores {stored}: an admin reads it; the account REST, both tokens and an "
            "admin PUT do not show or change it",
        )
        return stored

    def asked_again_forgets_the_old_time(self, user: User, first: int) -> None:
        self.ask_again(user)
        self.answer(self.asks(user), "cancel")
        forgotten = self.stored_time(user)
        self.answer(self.asks(user), "accept")
        later = self.stored_time(user)
        self.expect(
            forgotten is None and later is not None and later >= first,
            f"the old time {first}: after decline {forgotten}, after accept {later}",
        )
        self.probe.held("TC6", f"asked again, decline removed {first} and accept stored {later}")

    def other_apps_ask_no_second_time(self) -> None:
        password = self.probe.secret_values.get(f"{USERNAME} password")
        self.expect(password is not None, f"{FIRST_CLIENT} did not come first: no {USERNAME}")
        self.probe.app_client_sign_in(
            self.client,
            self.probe.browser(f"{self.client.client_id} terms"),
            USERNAME,
            password or "",
            self.probe.secret_values[self.client.secret_file],
        )
        self.probe.held(
            "TC5",
            f"{USERNAME}, accepted at {FIRST_CLIENT}, signs in to {self.client.client_id} "
            "with no terms page",
        )


def prove(
    probe: Probe,
    expect: Callable[[bool, str], None],
    form_with: Callable[[Any, str], tuple[str, dict[str, str]]],
    client: Client,
) -> None:
    cell = TermsCell(probe, expect, form_with, client)
    if client.client_id != FIRST_CLIENT:
        cell.other_apps_ask_no_second_time()
        return
    cell.action_is_default()
    user = cell.new_accounts_are_asked()
    cell.page_shows_the_theme_words(user)
    cell.decline_signs_nobody_in(user)
    first = cell.accept_stores_a_time_only_the_admin_sees(user)
    cell.asked_again_forgets_the_old_time(user, first)
