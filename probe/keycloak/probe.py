"""Keycloak probe: proves the edge's Keycloak service on a throwaway stack (README "Keycloak").

It starts its own compose project from this checkout with throwaway secrets in the scratch
directory, stands in for Cloudflare by sending `Host: auth.<domain>` and
`X-Forwarded-Proto: https` to 127.0.0.1:8081, prints one line per proof, and always tears the
stack down, volumes included. It needs 127.0.0.1:8081 and :8090 free, so the live edge is stopped.

    flock /tmp/probe-stack.lock timeout 540 python3 probe/keycloak/probe.py --scratch DIR
"""

from __future__ import annotations

import argparse
import base64
import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

REPO = Path(__file__).resolve().parents[2]
PROJECT = "edge-kc-probe"
DOMAIN = "hallucinai.de"
PUBLIC_HOST = f"auth.{DOMAIN}"
PUBLIC_ISSUER = f"https://{PUBLIC_HOST}/realms/hallucinai"
EDGE_URL = "http://127.0.0.1:8081"
ADMIN_URL = "http://localhost:8090"
MASTER_ISSUER = f"{ADMIN_URL}/realms/master"
ADMIN_CONSOLE_REDIRECT = f"{ADMIN_URL}/admin/master/console/"
TRAEFIK_NOT_FOUND = b"404 page not found\n"
SECRET_FILES = ("db-password", "config-cli-secret", "bootstrap-admin-password")
CONFIG_STATE_PREFIX = "de.adorsys.keycloak.config"
READY_TIMEOUT_SECONDS = 240
LOGOUT_TIMEOUT_SECONDS = 20
CHANGED_ACCESS_TOKEN_LIFESPAN = 360
CHANGED_GROUP = "probe-changed"
MEMBER_GROUP = "/apps/songmaker"


class ProbeFailure(Exception):
    """A proof did not hold; the message names what was expected and what came back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def held(proof: str, detail: str) -> None:
    print(f"PROOF {proof} held: {detail}", flush=True)


# --- HTTP -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LocalhostIsSecure(DefaultCookiePolicy):
    """Sends Secure cookies to http://localhost, as browsers do for a loopback origin."""

    def return_ok_secure(self, cookie: Cookie, request: Request) -> bool:
        return urlsplit(request.full_url).hostname == "localhost" or super().return_ok_secure(
            cookie, request
        )


def browser_opener() -> OpenerDirector:
    return new_opener(CookieJar(policy=LocalhostIsSecure()))


def new_opener(cookies: CookieJar | None = None) -> OpenerDirector:
    handlers: list[Any] = [NoRedirect()]
    if cookies is not None:
        handlers.append(HTTPCookieProcessor(cookies))
    return build_opener(*handlers)


PLAIN = new_opener()


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    json_body: Any = None,
    bearer: str | None = None,
    opener: OpenerDirector = PLAIN,
) -> Response:
    request_headers = dict(headers or {})
    data = None
    if form is not None:
        data = urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        request_headers["Content-Type"] = "application/json"
    if bearer:
        request_headers["Authorization"] = f"Bearer {bearer}"
    request = Request(url, data=data, method=method, headers=request_headers)
    try:
        with opener.open(request, timeout=15) as reply:
            return Response(reply.status, dict(reply.headers), reply.read())
    except HTTPError as error:
        return Response(error.code, dict(error.headers), error.read())


def through_tunnel(method: str, path: str, *, host: str = PUBLIC_HOST, **kwargs: Any) -> Response:
    """What cloudflared sends for a public request: the public host and the original scheme."""
    headers = {"Host": host, "X-Forwarded-Proto": "https"}
    return http(method, EDGE_URL + path, headers=headers, **kwargs)


def claims(jwt: str) -> dict[str, Any]:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# --- The throwaway stack --------------------------------------------------------------------


@dataclass
class Stack:
    scratch: Path
    secrets_dir: Path = field(init=False)
    config_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.secrets_dir = self.scratch / "secrets"
        self.config_dir = self.scratch / "config"

    def environment(self, config_dir: Path) -> dict[str, str]:
        return {
            **os.environ,
            "EDGE_DOMAIN": DOMAIN,
            "DOCKER_GID": str(grp.getgrnam("docker").gr_gid),
            "PROBE_SECRETS_DIR": str(self.secrets_dir),
            "PROBE_CONFIG_DIR": str(config_dir),
        }

    def compose(
        self, *args: str, bootstrap: bool = False, config_dir: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        files = ["compose.yaml"]
        if bootstrap:
            files.append("keycloak/bootstrap.compose.yaml")
        files.append("probe/keycloak/compose.probe.yaml")
        command = ["docker", "compose", "--project-name", PROJECT, "--project-directory", str(REPO)]
        for compose_file in files:
            command += ["--file", str(REPO / compose_file)]
        return subprocess.run(
            [*command, *args],
            env=self.environment(config_dir or self.config_dir),
            capture_output=True,
            text=True,
            check=False,
        )

    def write_secrets(self) -> dict[str, str]:
        self.secrets_dir.mkdir(mode=0o700, parents=True)
        values = {}
        for name in SECRET_FILES:
            values[name] = secrets.token_urlsafe(32)
            path = self.secrets_dir / name
            path.write_text(values[name])
            # Readable by the container users (postgres, keycloak-config-cli); the directory
            # stays 0700, so no other host user reaches the file.
            path.chmod(0o644)
        return values

    def copy_config(self, target: Path) -> Path:
        target.mkdir(parents=True)
        for name in ("master.yaml", "realm.yaml"):
            shutil.copy(REPO / "keycloak" / name, target / name)
        return target

    def apply(
        self, *, bootstrap: bool = False, config_dir: Path | None = None, cache: bool = True
    ) -> subprocess.CompletedProcess[str]:
        options = ["--rm", "--no-deps"]
        if not cache:
            options += ["--env", "IMPORT_CACHE_ENABLED=false"]
        return self.compose(
            "run", *options, "keycloak-config-cli", bootstrap=bootstrap, config_dir=config_dir
        )

    def must_apply(self, label: str, **kwargs: Any) -> str:
        result = self.apply(**kwargs)
        output = result.stdout + result.stderr
        expect(result.returncode == 0, f"{label} exited {result.returncode}:\n{tail(output)}")
        return output

    def move_master_to_admin_entrypoint(self) -> None:
        result = self.compose(
            "exec", "-T", "keycloak", "/opt/edge/admin-frontend.sh", bootstrap=True
        )
        output = result.stdout + result.stderr
        expect(
            result.returncode == 0, f"admin-frontend.sh exited {result.returncode}:\n{tail(output)}"
        )

    def up(self, *services: str, bootstrap: bool = False) -> None:
        result = self.compose("up", "--detach", "--wait", *services, bootstrap=bootstrap)
        expect(result.returncode == 0, f"compose up failed:\n{tail(result.stderr)}")

    def down(self) -> None:
        result = self.compose("down", "--volumes", "--remove-orphans", bootstrap=True)
        status = "removed" if result.returncode == 0 else f"FAILED ({tail(result.stderr)})"
        print(f"teardown: stack {PROJECT} {status}", flush=True)

    def container_env(self, service: str) -> list[str]:
        container_id = self.compose("ps", "--quiet", service).stdout.strip()
        expect(bool(container_id), f"{service} is not running")
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container_id],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(inspected.stdout)

    def logs(self, service: str) -> str:
        return self.compose("logs", "--no-log-prefix", service).stdout


def tail(output: str, lines: int = 15) -> str:
    """The last lines of a command's output, without Java stack frames."""
    meaningful = [line for line in output.strip().splitlines() if not line.startswith("\tat ")]
    return "\n".join(meaningful[-lines:])


def ports_free(*ports: int) -> list[int]:
    taken = []
    for port in ports:
        with socket.socket() as probe_socket:
            if probe_socket.connect_ex(("127.0.0.1", port)) == 0:
                taken.append(port)
    return taken


def wait_until_ready() -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if http("GET", f"{ADMIN_URL}/realms/master").status == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    raise ProbeFailure(f"Keycloak was not ready on {ADMIN_URL} after {READY_TIMEOUT_SECONDS}s")


# --- Admin API --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admin:
    token: str

    @classmethod
    def with_password(cls, username: str, password: str) -> Admin:
        form = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        }
        return cls(token_from(f"{ADMIN_URL}/realms/master/protocol/openid-connect/token", form))

    @classmethod
    def with_client(cls, secret: str) -> Admin:
        form = {
            "grant_type": "client_credentials",
            "client_id": "config-cli",
            "client_secret": secret,
        }
        return cls(token_from(f"{ADMIN_URL}/realms/master/protocol/openid-connect/token", form))

    def call(self, method: str, path: str, json_body: Any = None) -> Response:
        return http(
            method, f"{ADMIN_URL}/admin/realms{path}", json_body=json_body, bearer=self.token
        )

    def get(self, path: str) -> Any:
        response = self.call("GET", path)
        expect(response.status == 200, f"GET {path} answered {response.status}")
        return response.json()

    def create(self, path: str, representation: dict[str, Any]) -> str:
        response = self.call("POST", path, representation)
        expect(response.status == 201, f"POST {path} answered {response.status}: {response.text}")
        return response.headers["Location"].rsplit("/", 1)[-1]

    def must(self, method: str, path: str, json_body: Any = None) -> None:
        response = self.call(method, path, json_body)
        expect(response.status in (200, 204), f"{method} {path} answered {response.status}")

    def create_user(self, realm: str, username: str, password: str) -> str:
        return self.create(
            f"/{realm}/users",
            {
                "username": username,
                "enabled": True,
                "email": f"{username}@probe.invalid",
                "emailVerified": True,
                "firstName": "Probe",
                "lastName": username,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
        )

    def join_group(self, user_id: str, group_path: str) -> None:
        group = self.get(f"/hallucinai/group-by-path{group_path}")
        self.must("PUT", f"/hallucinai/users/{user_id}/groups/{group['id']}")

    def group_paths(self, user_id: str) -> list[str]:
        return sorted(group["path"] for group in self.get(f"/hallucinai/users/{user_id}/groups"))

    def realm_export(self) -> dict[str, Any]:
        response = self.call(
            "POST", "/hallucinai/partial-export?exportClients=true&exportGroupsAndRoles=true"
        )
        expect(response.status == 200, f"partial export answered {response.status}")
        return response.json()


def token_from(url: str, form: dict[str, str], opener: OpenerDirector = PLAIN) -> str:
    response = http("POST", url, form=form, opener=opener)
    expect(response.status == 200, f"token request answered {response.status}: {response.text}")
    return response.json()["access_token"]


def without_config_cli_state(export: dict[str, Any]) -> dict[str, Any]:
    """The export minus config-cli's own checksum and state attributes."""
    attributes = {
        key: value
        for key, value in export.get("attributes", {}).items()
        if not key.startswith(CONFIG_STATE_PREFIX)
    }
    return {**export, "attributes": attributes}


IDENTITY_KEYS = ("clientId", "alias", "name", "id")


def keyed(items: list[Any]) -> dict[str, Any] | None:
    """A list of representations keyed by their identity, or None for a plain list."""
    if not all(isinstance(item, dict) for item in items):
        return None
    for identity in IDENTITY_KEYS:
        keys = [item.get(identity) for item in items]
        if None not in keys and len(set(keys)) == len(keys):
            return {str(key): item for key, item in zip(keys, items, strict=True)}
    return None


def differences(before: Any, after: Any, path: str = "") -> list[str]:
    """The JSON paths at which two exports differ; lists of representations match by identity."""
    if isinstance(before, list) and isinstance(after, list):
        before_keyed, after_keyed = keyed(before), keyed(after)
        if before_keyed is not None and after_keyed is not None:
            before, after = before_keyed, after_keyed
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            difference
            for key in sorted(before.keys() | after.keys())
            for difference in differences(before.get(key), after.get(key), f"{path}/{key}")
        ]
    return [] if before == after else [path]


# --- Browser login (admin console) ------------------------------------------------------------


class FormCollector(HTMLParser):
    """Collects each form's action and its named input values from a Keycloak page."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "form":
            self.forms.append((attributes.get("action", ""), {}))
        elif tag == "input" and self.forms and attributes.get("name"):
            self.forms[-1][1][attributes["name"]] = attributes.get("value", "")


def form_with(page: Response, field_name: str) -> tuple[str, dict[str, str]]:
    collector = FormCollector()
    collector.feed(page.text)
    for action, inputs in collector.forms:
        if field_name in inputs:
            return action, inputs
    raise ProbeFailure(f"page {page.status} has no form with a '{field_name}' field")


def page_message(page: Response) -> str:
    """The feedback a Keycloak page shows the user, for a failure message."""
    found = re.search(
        r'class="[^"]*(?:kc-feedback-text|alert|error)[^"]*"[^>]*>\s*([^<]{3,200})', page.text
    )
    return found.group(1).strip() if found else f"no feedback text ({len(page.body)} bytes)"


def totp(secret: str, period_offset: int = 0) -> str:
    """RFC 6238 code for Keycloak's default policy: HmacSHA1, 6 digits, 30 s, the raw secret."""
    counter = int(time.time()) // 30 + period_offset
    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def admin_console_login(username: str, password: str, second_factor: Any) -> dict[str, Any]:
    """Signs in to the admin console like a browser does and returns the access token's claims.

    `second_factor(page)` answers the page that follows the password: it returns the form
    action and the fields to post.
    """
    opener = browser_opener()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = urlencode(
        {
            "client_id": "security-admin-console",
            "redirect_uri": ADMIN_CONSOLE_REDIRECT,
            "response_type": "code",
            "scope": "openid",
            "state": secrets.token_urlsafe(8),
            "nonce": secrets.token_urlsafe(8),
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
    )
    login_page = http("GET", f"{MASTER_ISSUER}/protocol/openid-connect/auth?{query}", opener=opener)
    expect(login_page.status == 200, f"admin login page answered {login_page.status}")
    action, fields = form_with(login_page, "username")
    fields |= {"username": username, "password": password}
    factor_page = http("POST", action, form=fields, opener=opener)
    expect(
        factor_page.status == 200,
        f"password step answered {factor_page.status}: {page_message(factor_page)}",
    )
    action, fields = second_factor(factor_page)
    done = http("POST", action, form=fields, opener=opener)
    location = done.headers.get("Location", "")
    expect(
        done.status == 302 and location.startswith(ADMIN_CONSOLE_REDIRECT),
        f"second factor answered {done.status} without the console redirect: {page_message(done)}",
    )
    code = parse_qs(urlsplit(location).query)["code"][0]
    token_form = {
        "grant_type": "authorization_code",
        "client_id": "security-admin-console",
        "redirect_uri": ADMIN_CONSOLE_REDIRECT,
        "code": code,
        "code_verifier": verifier,
    }
    return claims(token_from(f"{MASTER_ISSUER}/protocol/openid-connect/token", token_form))


# --- Proofs -------------------------------------------------------------------------------------


@dataclass
class Probe:
    stack: Stack
    secret_values: dict[str, str] = field(default_factory=dict)
    member_password: str = field(default_factory=lambda: secrets.token_urlsafe(16))

    def run(self) -> None:
        self.secret_values = self.stack.write_secrets()
        self.stack.copy_config(self.stack.config_dir)
        self.stack.up(bootstrap=True)
        wait_until_ready()
        self.stack.move_master_to_admin_entrypoint()
        print("stack: up with the temporary bootstrap admin, master on the admin entrypoint")

        bootstrap_admin = Admin.with_password(
            "temp-admin", self.secret_values["bootstrap-admin-password"]
        )
        self.missing_variable_fails_loudly(bootstrap_admin)
        self.stack.must_apply("fresh apply", bootstrap=True)

        self.stack.up("keycloak")
        wait_until_ready()
        # The config-cli client exists only if the fresh apply created the master configuration.
        admin = Admin.with_client(self.secret_values["config-cli-secret"])
        expect(admin.get("/hallucinai")["realm"] == "hallucinai", "no hallucinai realm")
        held(
            "3a",
            "a fresh apply created the master configuration (the config-cli client signs in "
            "after a restart without the bootstrap overlay) and the hallucinai realm",
        )

        member_id = self.seed_member(admin)
        self.reapply_is_idempotent(admin, member_id)
        self.public_issuer_and_paths()
        self.admin_console_only_on_loopback(admin)
        self.changed_yaml_applies(admin, member_id)
        rp_secret = self.create_probe_rp(admin)
        self.member_token_carries_groups_and_audience(rp_secret)
        self.disabled_user_refused_on_refresh(admin, rp_secret)
        self.backchannel_logout_reaches_rp(admin, rp_secret)
        self.no_secret_in_config_env()

    def missing_variable_fails_loudly(self, admin: Admin) -> None:
        broken = self.stack.copy_config(self.stack.scratch / "config-missing-variable")
        realm_file = broken / "realm.yaml"
        realm_file.write_text(
            realm_file.read_text() + "displayName: $(env:PROBE_UNDEFINED_VARIABLE)\n"
        )
        result = self.stack.apply(bootstrap=True, config_dir=broken)
        output = result.stdout + result.stderr
        expect(result.returncode != 0, "an apply with an undefined variable exited 0")
        expect("PROBE_UNDEFINED_VARIABLE" in output, "the failure does not name the variable")
        expect(admin.call("GET", "/hallucinai").status == 404, "the failed apply left a realm")
        held(
            "3b",
            f"an undefined variable fails the apply (exit {result.returncode}, names "
            "PROBE_UNDEFINED_VARIABLE) and creates no realm",
        )

    def seed_member(self, admin: Admin) -> str:
        """Stands in for a person and a membership the operator manages in the admin console."""
        member_id = admin.create_user("hallucinai", "probe-member", self.member_password)
        admin.join_group(member_id, MEMBER_GROUP)
        print(f"seed: probe-member in {MEMBER_GROUP} through the admin API", flush=True)
        return member_id

    def reapply_is_idempotent(self, admin: Admin, member_id: str) -> None:
        before = without_config_cli_state(admin.realm_export())
        output = self.stack.must_apply("second apply", cache=False)
        expect("checksum" not in output.lower(), "the second apply consulted the checksum cache")
        after = without_config_cli_state(admin.realm_export())
        changed = differences(before, after)
        expect(not changed, f"the second apply changed the realm export: {changed}")
        expect(admin.group_paths(member_id) == [MEMBER_GROUP], "the seeded membership changed")
        held(
            "3c",
            "a second apply with IMPORT_CACHE_ENABLED=false leaves the realm export unchanged "
            f"and probe-member in {MEMBER_GROUP}",
        )
        self.realm_settings_as_decided(admin, after)

    def realm_settings_as_decided(self, admin: Admin, export: dict[str, Any]) -> None:
        realm = admin.get("/hallucinai")
        master = admin.get("/master")
        expected = {
            "webAuthnPolicyRpId": DOMAIN,
            "webAuthnPolicyPasswordlessRpId": DOMAIN,
            "accessTokenLifespan": 300,
            "ssoSessionMaxLifespan": 2592000,
            "ssoSessionIdleTimeout": 604800,
            "bruteForceProtected": True,
            "eventsEnabled": True,
            "eventsExpiration": 2592000,
            "adminEventsEnabled": True,
        }
        wrong = {key: realm.get(key) for key, value in expected.items() if realm.get(key) != value}
        admin_expiry = realm.get("attributes", {}).get("adminEventsExpiration")
        if admin_expiry != "31536000":
            wrong["attributes.adminEventsExpiration"] = admin_expiry
        providers = export.get("identityProviders", [])
        if any(provider.get("trustEmail") for provider in providers):
            wrong["identityProviders.trustEmail"] = True
        master_expected = {
            "bruteForceProtected": True,
            "browserFlow": "browser-mfa",
            "webAuthnPolicyRpId": "localhost",
        }
        for key, value in master_expected.items():
            if master.get(key) != value:
                wrong[f"master.{key}"] = master.get(key)
        expect(not wrong, f"realm settings differ from the decisions: {wrong}")
        held(
            "8",
            "rpId hallucinai.de, access token 300 s, SSO max 30 d / idle 7 d, login events "
            "30 d, admin events 1 y, brute force on in both realms, "
            f"{len(providers)} identity providers (so no trustEmail=true), master browser flow "
            "browser-mfa with rpId localhost",
        )

    def public_issuer_and_paths(self) -> None:
        discovery = through_tunnel("GET", "/realms/hallucinai/.well-known/openid-configuration")
        expect(discovery.status == 200, f"public discovery answered {discovery.status}")
        issuer = discovery.json()["issuer"]
        expect(issuer == PUBLIC_ISSUER, f"public issuer is {issuer}")
        login_page = through_tunnel(
            "GET",
            "/realms/hallucinai/protocol/openid-connect/auth?"
            + urlencode(
                {
                    "client_id": "account-console",
                    "redirect_uri": f"https://{PUBLIC_HOST}/realms/hallucinai/account/",
                    "response_type": "code",
                    "scope": "openid",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                }
            ),
        )
        expect(login_page.status == 200, f"public login page answered {login_page.status}")
        expect("HTTPS required" not in login_page.text, "the login page demands HTTPS")
        stylesheet = re.search(r'href="(/resources/[^"]+\.css)"', login_page.text)
        expect(stylesheet is not None, "the login page links no /resources/ stylesheet")
        resource = through_tunnel("GET", stylesheet.group(1))
        expect(resource.status == 200, f"{stylesheet.group(1)} answered {resource.status}")
        held("1", f"public discovery issuer {issuer}; the login page renders without HTTPS error")

        refused = [
            (PUBLIC_HOST, "/realms/master/.well-known/openid-configuration"),
            (PUBLIC_HOST, "/realms/master/protocol/openid-connect/token"),
            (PUBLIC_HOST, "/admin/"),
            (PUBLIC_HOST, "/admin/master/console/"),
            (PUBLIC_HOST, "/metrics"),
            (PUBLIC_HOST, "/health"),
            (PUBLIC_HOST, "/"),
            (PUBLIC_HOST, "/realms/hallucinai/../master/.well-known/openid-configuration"),
            ("localhost", "/realms/hallucinai/.well-known/openid-configuration"),
            ("localhost", "/admin/master/console/"),
        ]
        for host, path in refused:
            answer = through_tunnel("GET", path, host=host)
            expect(
                answer.status == 404 and answer.body == TRAEFIK_NOT_FOUND,
                f"8081 {host}{path} answered {answer.status}, not Traefik's 404",
            )
        # Inside the admitted prefix, so only the entrypoint's encoded-character rule stops it.
        encoded = through_tunnel("GET", "/realms/hallucinai/..%2Fmaster/")
        expect(encoded.status == 400, f"an encoded slash on 8081 answered {encoded.status}")
        held(
            "2a",
            f"8081 answers /realms/hallucinai/ and /resources/; Traefik refuses {len(refused)} "
            "master, admin, metrics, health, root, traversal and Host: localhost requests (404) "
            "and an encoded slash inside the admitted prefix (400)",
        )

    def admin_console_only_on_loopback(self, admin: Admin) -> None:
        master_discovery = http("GET", f"{MASTER_ISSUER}/.well-known/openid-configuration")
        expect(
            master_discovery.status == 200, f"master discovery answered {master_discovery.status}"
        )
        issuer = master_discovery.json()["issuer"]
        expect(issuer == MASTER_ISSUER, f"master issuer is {issuer}")
        foreign_host = http("GET", f"{ADMIN_URL}/realms/hallucinai/", headers={"Host": PUBLIC_HOST})
        expect(
            foreign_host.status == 404, f"8090 with the public host answered {foreign_host.status}"
        )
        encoded = http("GET", f"{ADMIN_URL}/admin%2Fmaster/console/")
        expect(encoded.status == 400, f"an encoded slash on 8090 answered {encoded.status}")

        password = secrets.token_urlsafe(16)
        admin_id = admin.create_user("master", "probe-admin", password)
        admin_role = admin.get("/master/roles/admin")
        admin.must("POST", f"/master/users/{admin_id}/role-mappings/realm", [admin_role])
        enrolled: dict[str, str] = {}

        def enrol_totp(page: Response) -> tuple[str, dict[str, str]]:
            action, fields = form_with(page, "totpSecret")
            enrolled["secret"] = fields["totpSecret"]
            return action, fields | {"totp": totp(fields["totpSecret"]), "userLabel": "probe"}

        first = admin_console_login("probe-admin", password, enrol_totp)
        expect(first["iss"] == MASTER_ISSUER, f"admin token issuer is {first['iss']}")

        def answer_otp(page: Response) -> tuple[str, dict[str, str]]:
            action, fields = form_with(page, "otp")
            # The next period's code: the first one was spent, and Keycloak's look-around
            # window of one period accepts the next.
            return action, fields | {"otp": totp(enrolled["secret"], period_offset=1)}

        second = admin_console_login("probe-admin", password, answer_otp)
        expect(second["iss"] == MASTER_ISSUER, f"second admin token issuer is {second['iss']}")
        held(
            "2b",
            f"on localhost:8090 the master issuer is {issuer}; a new admin must enrol TOTP "
            "before the console login completes, the next login demands the code; 8090 "
            "refuses the public host (404) and an encoded slash (400)",
        )

    def changed_yaml_applies(self, admin: Admin, member_id: str) -> None:
        changed = self.stack.copy_config(self.stack.scratch / "config-changed")
        realm_file = changed / "realm.yaml"
        text = realm_file.read_text()
        expect("accessTokenLifespan: 300 " in text, "realm.yaml no longer sets 300 s as expected")
        text = text.replace(
            "accessTokenLifespan: 300 ", f"accessTokenLifespan: {CHANGED_ACCESS_TOKEN_LIFESPAN} "
        )
        text = text.replace("  - name: ki-abo\n", f"  - name: ki-abo\n  - name: {CHANGED_GROUP}\n")
        realm_file.write_text(text)
        self.stack.must_apply("changed apply", config_dir=changed)
        realm = admin.get("/hallucinai")
        expect(
            realm["accessTokenLifespan"] == CHANGED_ACCESS_TOKEN_LIFESPAN,
            f"access token lifespan is {realm['accessTokenLifespan']} after the change",
        )
        expect(
            admin.call("GET", f"/hallucinai/group-by-path/{CHANGED_GROUP}").status == 200,
            f"group {CHANGED_GROUP} was not created",
        )
        expect(admin.group_paths(member_id) == [MEMBER_GROUP], "the seeded membership changed")
        held(
            "3d",
            f"a changed realm.yaml applies (access token {CHANGED_ACCESS_TOKEN_LIFESPAN} s, "
            f"group /{CHANGED_GROUP}); probe-member is still in {MEMBER_GROUP}",
        )

    def create_probe_rp(self, admin: Admin) -> str:
        representation = json.loads((REPO / "probe/keycloak/probe-rp-client.json").read_text())
        client_id = admin.create("/hallucinai/clients", representation)
        return admin.get(f"/hallucinai/clients/{client_id}/client-secret")["value"]

    def password_grant(self, username: str, password: str, rp_secret: str) -> dict[str, Any]:
        response = through_tunnel(
            "POST",
            "/realms/hallucinai/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": "probe-rp",
                "client_secret": rp_secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        expect(response.status == 200, f"password grant for {username} answered {response.status}")
        return response.json()

    def member_token_carries_groups_and_audience(self, rp_secret: str) -> None:
        tokens = self.password_grant("probe-member", self.member_password, rp_secret)
        access = claims(tokens["access_token"])
        audience = access.get("aud", [])
        audience = [audience] if isinstance(audience, str) else audience
        expect(access.get("iss") == PUBLIC_ISSUER, f"access token issuer is {access.get('iss')}")
        expect(MEMBER_GROUP in access.get("groups", []), f"groups claim is {access.get('groups')}")
        expect("agent-service" in audience, f"audience is {audience}")
        held(
            "4",
            f"probe-member's access token: iss {access['iss']}, groups {access['groups']}, "
            f"aud {audience}",
        )

    def disabled_user_refused_on_refresh(self, admin: Admin, rp_secret: str) -> None:
        password = secrets.token_urlsafe(16)
        user_id = admin.create_user("hallucinai", "probe-disabled", password)
        tokens = self.password_grant("probe-disabled", password, rp_secret)
        admin.must("PUT", f"/hallucinai/users/{user_id}", {"enabled": False})
        refresh = through_tunnel(
            "POST",
            "/realms/hallucinai/protocol/openid-connect/token",
            form={
                "grant_type": "refresh_token",
                "client_id": "probe-rp",
                "client_secret": rp_secret,
                "refresh_token": tokens["refresh_token"],
            },
        )
        error = refresh.json().get("error") if refresh.status == 400 else None
        expect(error == "invalid_grant", f"refresh after disabling answered {refresh.status}")
        held("5", "a disabled user's refresh is refused (400 invalid_grant)")

    def backchannel_logout_reaches_rp(self, admin: Admin, rp_secret: str) -> None:
        password = secrets.token_urlsafe(16)
        user_id = admin.create_user("hallucinai", "probe-logout", password)
        self.password_grant("probe-logout", password, rp_secret)
        admin.must("POST", f"/hallucinai/users/{user_id}/logout")
        deadline = time.monotonic() + LOGOUT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for line in self.stack.logs("probe-rp").splitlines():
                if line.startswith("backchannel-logout "):
                    record = json.loads(line.removeprefix("backchannel-logout "))
                    if record["sub"] == user_id:
                        expect(record["iss"] == PUBLIC_ISSUER, f"logout iss is {record['iss']}")
                        held(
                            "6",
                            "an admin logout of probe-logout reached probe-rp over the compose "
                            f"network (iss {record['iss']}, sid present: {bool(record['sid'])})",
                        )
                        return
            time.sleep(1)
        raise ProbeFailure(f"probe-rp saw no back-channel logout within {LOGOUT_TIMEOUT_SECONDS}s")

    def no_secret_in_config_env(self) -> None:
        counts = {}
        for service in ("keycloak", "keycloak-db"):
            environment = self.stack.container_env(service)
            leaked = [
                name
                for name, value in self.secret_values.items()
                if any(value in entry for entry in environment)
            ]
            expect(not leaked, f"{service} Config.Env holds the value of {leaked}")
            counts[service] = len(environment)
        held(
            "7",
            f"Config.Env of keycloak ({counts['keycloak']} entries) and keycloak-db "
            f"({counts['keycloak-db']} entries) holds none of the {len(SECRET_FILES)} secrets",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True, help="an empty or new directory")
    scratch = parser.parse_args().scratch.resolve()
    if scratch.exists() and any(scratch.iterdir()):
        parser.error(f"{scratch} is not empty")
    taken = ports_free(8081, 8090)
    if taken:
        print(f"probe refused: 127.0.0.1 port(s) {taken} in use; stop the live edge first")
        return 2
    # `timeout` sends SIGTERM; turn it into an exception so the teardown below still runs.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit("probe stopped by SIGTERM"))
    stack = Stack(scratch)
    try:
        Probe(stack).run()
    except ProbeFailure as failure:
        print(f"PROBE FAILED: {failure}", flush=True)
        return 1
    finally:
        stack.down()
    print("PROBE PASSED: proofs 1-8 hold", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
