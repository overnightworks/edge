"""Keycloak probe: proves the edge's Keycloak service on a throwaway stack (README "Keycloak").

It starts its own compose project from this checkout with throwaway secrets in the scratch
directory, stands in for Cloudflare by sending `Host: auth.<domain>` and
`X-Forwarded-Proto: https` to its edge entrypoint, prints one line per proof, and always tears
the stack down, volumes included. Its Traefik entrypoints listen on free loopback ports it picks
and its keycloak network takes a free /24, so it runs beside the live edge, which stays up.

    flock /tmp/probe-stack.lock timeout --kill-after=50 540 \
        python3 probe/keycloak/probe.py --scratch DIR

`hold --scratch DIR` instead brings the same stack up without the proofs, prints one READY line
and keeps it until SIGTERM, SIGINT or EOF on stdin (see probe/keycloak/README.md).
"""

from __future__ import annotations

import argparse
import base64
import grp
import hashlib
import hmac
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy, parse_ns_headers
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

REPO = Path(__file__).resolve().parents[2]
# The probe runs from a bare checkout with the system python; edge.realm is standard library only.
sys.path.insert(0, str(REPO / "src"))
from edge.realm import OBSERVER_SERVICE, change_lines, new_digest_key  # noqa: E402
from edge.realm.observe import Inventory  # noqa: E402

PROJECT = "edge-kc-probe"
KEYCLOAK_PROFILE = "keycloak"
PROJECT_LABEL = f"com.docker.compose.project={PROJECT}"
DOMAIN = "hallucinai.de"
REALM = "hallucinai"
THEME = "hallucinai"
REALM_FILE = "realm.yaml"
CONFIG_FILES = ("master.yaml", REALM_FILE)
PUBLIC_HOST = f"auth.{DOMAIN}"
REALM_PATH = f"/realms/{REALM}"
PUBLIC_TOKEN_PATH = f"{REALM_PATH}/protocol/openid-connect/token"
PUBLIC_ISSUER = f"https://{PUBLIC_HOST}{REALM_PATH}"
LOOPBACK_ADDRESS = "127.0.0.1"
# The live entrypoint addresses in traefik.yml, which the probe's copy of it replaces.
LIVE_EDGE_ADDRESS = f"{LOOPBACK_ADDRESS}:8081"
LIVE_ADMIN_ADDRESS = f"{LOOPBACK_ADDRESS}:8090"
# What keycloak/go-live pins for the live edge, which no step over the probe's stack may reach.
LIVE_GO_LIVE_ADDRESSES = (
    "localhost:8090",
    "localhost:8081",
    LIVE_ADMIN_ADDRESS,
    LIVE_EDGE_ADDRESS,
)
TRAEFIK_CONFIG = "traefik.yml"
NETWORK_OVERLAY = "network.compose.yaml"
# Which containers the Docker provider routes: the live one takes any `edge.name` label that
# matches; the probe's takes its own project only, and its keycloak carries an `edge.name` the
# live provider refuses (compose.probe.yaml), so neither Traefik sees the other's containers.
LIVE_CONSTRAINTS = "LabelRegex(`edge.name`, `^[a-z0-9-]{1,63}$`)"
PROBE_CONSTRAINTS = f"Label(`com.docker.compose.project`, `{PROJECT}`)"
# The /24s a probe's keycloak network may take, clear of the live one (172.31.253.0/24).
SUBNET_CANDIDATES = tuple(f"172.31.{third}.0/24" for third in range(200, 250))
TRAEFIK_NOT_FOUND = b"404 page not found\n"


@dataclass(frozen=True)
class AppClient:
    """An app's relying party in realm.yaml as #11 lists it (#108, #161)."""

    client_id: str
    group: str  # the group of the user who signs in; its full path is in the ID token
    callbacks: tuple[str, ...]
    logout_redirect: str
    agent_workload: bool = False  # its service account is a workload of the agent service (#183)

    @property
    def default_scopes(self) -> set[str]:
        return AGENT_WORKLOAD_CLIENT_SCOPES if self.agent_workload else APP_DEFAULT_CLIENT_SCOPES

    @property
    def secret_file(self) -> str:
        return f"clients/{self.client_id}"

    @property
    def callback(self) -> str:
        return self.callbacks[0]

    @property
    def origin(self) -> str:
        return f"https://{self.client_id}.{DOMAIN}"


SONGMAKER = AppClient(
    "songmaker",
    "/apps/songmaker/admin",
    (f"https://songmaker.{DOMAIN}/auth/callback",),
    f"https://songmaker.{DOMAIN}/login",
    agent_workload=True,
)
HOPIN = AppClient(
    "hopin",
    "/apps/hopin",
    (f"https://hopin.{DOMAIN}/auth/callback",),
    f"https://hopin.{DOMAIN}/login",
)
PRESENTATOR = AppClient(
    "presentator",
    "/apps/presentator/admin",
    (
        f"https://presentator.{DOMAIN}/auth/callback",
        f"https://presentator-talk.{DOMAIN}/auth/callback",
    ),
    f"https://presentator.{DOMAIN}/login",
    agent_workload=True,
)
APP_CLIENTS = (SONGMAKER, HOPIN, PRESENTATOR)
APP_DEFAULT_CLIENT_SCOPES = {"basic", "profile", "groups"}
AGENT_WORKLOAD_CLIENT_SCOPES = APP_DEFAULT_CLIENT_SCOPES | {
    "email",
    "roles",
    "web-origins",
    "acr",
    "service_account",
    "agent-service-audience",
}
AGENT_SERVICE = "agent-service"
AGENT_WORKLOAD_ROLE = "agent-workload"
SECRET_FILES = (
    "db-password",
    "config-cli-secret",
    "bootstrap-admin-password",
    *(client.secret_file for client in APP_CLIENTS),
)
READY_TIMEOUT_SECONDS = 240
LOGOUT_TIMEOUT_SECONDS = 20
CHANGED_ACCESS_TOKEN_LIFESPAN = 360
CHANGED_GROUP = "probe-changed"
MEMBER_GROUP = "/apps/songmaker"
PUBLIC_AUTH_PATH = f"{REALM_PATH}/protocol/openid-connect/auth"
PUBLIC_LOGOUT_PATH = f"{REALM_PATH}/protocol/openid-connect/logout"
CLIENT_ADDRESS = "198.51.100.23"  # the person signing in, as Cloudflare saw it
CLIENT_ADDRESS_IPV6 = "2001:db8::23"  # the same, for a person on IPv6
FORGED_FORWARDED_FOR = "203.0.113.9, 192.0.2.7"  # a forged first value, then a proxy's
CONSOLE_PARENT = "/apps"
CONSOLE_SUBGROUP_NAME = "probe-console"
CONSOLE_SUBGROUP = f"{CONSOLE_PARENT}/{CONSOLE_SUBGROUP_NAME}"
# What an operator adds in the admin console before an apply (#66, the U1 measurement): the
# default apply removes the first five, the survivors stay, and the observer names exactly that.
CONSOLE_NAME = "probe-console"
CONSOLE_FLOW = "console-flow"
FORMS_FLOW = "browser-mfa-forms"
REMOVED_BY_THE_APPLY = [
    f"hallucinai: removal group {CONSOLE_SUBGROUP} (1 member)",
    f"hallucinai: removal protocol mapper scope groups/{CONSOLE_NAME}-mapper",
    f"master: removal flow {CONSOLE_FLOW}",
    f"master: removal flow step {FORMS_FLOW}/auth-otp-form#1",
    f"master: removal service-account realm role service-account-config-cli/{CONSOLE_NAME}-role",
]
CHANGED_CONFIG = "browser-mfa-without-passkey"
ESCAPING_GROUP = "probe\x1b[31mconsole\nname"
OBSERVER_SECRET = "/run/secrets/config_cli_secret"
OBSERVER_SCRIPT = "/opt/observer/observe.py"
OBSERVER_START_SECONDS = 60
GO_LIVE_ADMIN = "probe-owner"
ADMIN_REQUIRED_ACTIONS = ("CONFIGURE_TOTP", "webauthn-register")


class ProbeFailure(Exception):
    """A proof did not hold; the message names what was expected and what came back."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


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


LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LocalhostIsSecure(DefaultCookiePolicy):
    """Sends Secure cookies to a loopback origin (http://localhost, or the edge entrypoint on
    127.0.0.1). The edge entrypoint with `X-Forwarded-Proto: https` stands in for the https
    origin auth.hallucinai.de, where a browser does send them."""

    def return_ok_secure(self, cookie: Cookie, request: Request) -> bool:
        return urlsplit(request.full_url).hostname in LOOPBACK_HOSTS or super().return_ok_secure(
            cookie, request
        )


class ReceivedCookies(CookieJar):
    """A browser's cookie jar that hands every nonempty value a response sets to `keep` as it
    arrives, so proof 7 looks for it even after a later response replaces or expires it."""

    def __init__(self, keep: Callable[[str, str], object]) -> None:
        super().__init__(policy=LocalhostIsSecure())
        self.keep = keep
        self.received = 0

    def extract_cookies(self, response: Any, request: Request) -> None:
        for name_and_value, *_attributes in parse_ns_headers(
            response.info().get_all("Set-Cookie", [])
        ):
            name, value = name_and_value
            if value:
                self.received += 1
                self.keep(f"cookie {name} #{self.received}", value)
        super().extract_cookies(response, request)


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


def tunnel_request(
    edge_url: str,
    method: str,
    path: str,
    *,
    host: str = PUBLIC_HOST,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    """What cloudflared sends to the edge entrypoint at `edge_url` for a public request: the
    public host and the original scheme."""
    sent = {"Host": host, "X-Forwarded-Proto": "https", **(headers or {})}
    return http(method, edge_url + path, headers=sent, **kwargs)


def tunnel_request_url(edge_url: str, method: str, url: str, **kwargs: Any) -> Response:
    """Follows an absolute URL Keycloak gave out on the public host, through the tunnel."""
    target = urlsplit(url)
    expect(
        target.scheme == "https" and target.hostname == PUBLIC_HOST,
        f"{url} is not https on {PUBLIC_HOST}",
    )
    return tunnel_request(
        edge_url, method, urlunsplit(("", "", target.path, target.query, "")), **kwargs
    )


def cloudflare_forwarded(address: str) -> str:
    """The `Forwarded` value external/cloudflare.tf sets: RFC 7239 brackets and quotes IPv6."""
    node = f'"[{address}]"' if ipaddress.ip_address(address).version == 6 else address
    return f"for={node};proto=https"


def stored_event_types(events: list[dict[str, Any]], request_address: str) -> list[str]:
    """The sorted types of stored user events, each proven to carry `request_address`.

    The address is the one Cloudflare put in `Forwarded`, which Traefik and Keycloak pass on and
    read; a dropped link would store the loopback address instead.
    """
    for event in events:
        address = event.get("ipAddress")
        expect(
            address == request_address,
            f"the stored {event['type']} event carries the IP address {address}, "
            f"not the request's {request_address}",
        )
    return sorted(event["type"] for event in events)


def claims(jwt: str) -> dict[str, Any]:
    payload = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def audience_of(access: dict[str, Any]) -> list[str]:
    audience = access.get("aud", [])
    return [audience] if isinstance(audience, str) else audience


def is_workload_identity_of(access: dict[str, Any], client_id: str) -> bool:
    """The shape agent-service accepts: the workload role and audience, `client_id` and `azp`
    both naming the app, and no group."""
    roles = access.get("realm_access", {}).get("roles", [])
    return (
        AGENT_WORKLOAD_ROLE in roles
        and AGENT_SERVICE in audience_of(access)
        and access.get("client_id") == client_id
        and access.get("azp") == client_id
        and not access.get("groups")
    )


# --- The throwaway stack --------------------------------------------------------------------


@dataclass
class Stack:
    scratch: Path
    admin_url: str = ""  # a test's stand-in; empty, the admin entrypoint's own free port
    edge_url: str = field(init=False)
    edge_port: int = field(init=False)
    admin_port: int = field(init=False)
    traefik_config: Path = field(init=False)
    network_overlay: Path = field(init=False)
    keycloak_subnet: str = field(init=False)
    secrets_dir: Path = field(init=False)
    config_dir: Path = field(init=False)
    # Everything a secret could leak into, for proof 7: each container's Config.Env as it was
    # seen, and the output of every compose command (applies and service logs included).
    seen_env: list[tuple[str, list[str]]] = field(init=False, default_factory=list)
    outputs: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.secrets_dir = self.scratch / "secrets"
        self.config_dir = self.scratch / "config"
        self.traefik_config = self.scratch / TRAEFIK_CONFIG
        self.edge_port, self.admin_port = free_loopback_ports(2)
        self.edge_url = f"http://127.0.0.1:{self.edge_port}"
        self.admin_url = self.admin_url or f"http://localhost:{self.admin_port}"
        self.keycloak_subnet = free_subnet()
        self.network_overlay = self.scratch / NETWORK_OVERLAY
        self.write_network_overlay()

    @property
    def master_issuer(self) -> str:
        return f"{self.admin_url}/realms/master"

    @property
    def master_token_url(self) -> str:
        return f"{self.master_issuer}/protocol/openid-connect/token"

    @property
    def admin_console_redirect(self) -> str:
        return f"{self.admin_url}/admin/master/console/"

    def environment(self, config_dir: Path) -> dict[str, str]:
        return {
            **os.environ,
            "EDGE_DOMAIN": DOMAIN,
            "DOCKER_GID": os.environ.get("DOCKER_GID") or str(grp.getgrnam("docker").gr_gid),
            "PROBE_SECRETS_DIR": str(self.secrets_dir),
            "PROBE_CONFIG_DIR": str(config_dir),
            "PROBE_TRAEFIK_CONFIG": str(self.traefik_config),
            "PROBE_ADMIN_URL": self.admin_url,
            "PROBE_KEYCLOAK_SUBNET": self.keycloak_subnet,
        }

    def write_network_overlay(self) -> None:
        """The keycloak network on this stack's free subnet instead of the live one. A compose
        file cannot replace the base file's list of subnets, so the base file's is dropped with
        compose's own `!override` tag here, which the repository's YAML checks never read."""
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.network_overlay.write_text(
            "networks:\n  keycloak:\n    ipam:\n      config: !override\n"
            f"        - subnet: {self.keycloak_subnet}\n"
        )

    def write_traefik_config(self) -> None:
        """The repository's traefik.yml with the entrypoints on the probe's free ports and the
        Docker provider limited to the probe's own project, so the probe runs beside the live
        edge (which holds 8081 and 8090) and neither Traefik routes to the other's containers."""
        text = (REPO / TRAEFIK_CONFIG).read_text()
        for live, probe in {
            LIVE_EDGE_ADDRESS: f"{LOOPBACK_ADDRESS}:{self.edge_port}",
            LIVE_ADMIN_ADDRESS: f"{LOOPBACK_ADDRESS}:{self.admin_port}",
            LIVE_CONSTRAINTS: PROBE_CONSTRAINTS,
        }.items():
            expect(live in text, f"{TRAEFIK_CONFIG} no longer holds {live}")
            text = text.replace(live, probe)
        self.traefik_config.write_text(text)

    def compose(
        self,
        *args: str,
        bootstrap: bool = False,
        config_dir: Path | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [*self.compose_command(bootstrap), *args],
            env=self.environment(config_dir or self.config_dir),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
        self.outputs.append(result.stdout + result.stderr)
        return result

    def compose_command(self, bootstrap: bool = False) -> list[str]:
        files = ["compose.yaml"]
        if bootstrap:
            files.append("keycloak/bootstrap.compose.yaml")
        files.append("probe/keycloak/compose.probe.yaml")
        command = [
            "docker",
            "compose",
            "--project-name",
            PROJECT,
            "--project-directory",
            str(REPO),
            "--profile",
            KEYCLOAK_PROFILE,
        ]
        for compose_file in files:
            command += ["--file", str(REPO / compose_file)]
        return [*command, "--file", str(self.network_overlay)]

    def write_secrets(self) -> dict[str, str]:
        self.secrets_dir.mkdir(mode=0o700, parents=True)
        values = {}
        for name in SECRET_FILES:
            values[name] = secrets.token_urlsafe(32)
            path = self.secrets_dir / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_text(values[name])
            # Readable by the container users (postgres, keycloak-config-cli); the directory
            # stays 0700, so no other host user reaches the file.
            path.chmod(0o644)
        return values

    def copy_config(self, target: Path) -> Path:
        target.mkdir(parents=True)
        for name in CONFIG_FILES:
            shutil.copy(REPO / "keycloak" / name, target / name)
        return target

    def apply(
        self, *, bootstrap: bool = False, config_dir: Path | None = None, cache: bool = True
    ) -> subprocess.CompletedProcess[str]:
        # Kept until inspected, so proof 7 sees the one-off container's Config.Env too.
        container = f"{PROJECT}-apply-{secrets.token_hex(4)}"
        options = ["--no-deps", "--name", container]
        if not cache:
            options += ["--env", "IMPORT_CACHE_ENABLED=false"]
        result = self.compose(
            "run", *options, "keycloak-config-cli", bootstrap=bootstrap, config_dir=config_dir
        )
        self.see_env(container)
        subprocess.run(["docker", "rm", container], capture_output=True, check=True)
        return result

    def observe(self, digest_key: str, *, keep_as: str = "") -> subprocess.CompletedProcess[str]:
        """One observe as `edge apply` runs it; `keep_as` names and keeps the container, so its
        Config.Env and hardening can be inspected, instead of `--rm`."""
        options = ["--name", keep_as] if keep_as else ["--rm"]
        return self.compose("run", "-T", *options, OBSERVER_SERVICE, stdin=digest_key + "\n")

    def observe_stopped_by(
        self, digest_key: str, stop_signal: signal.Signals, moment: Callable[[Observing], bool]
    ) -> list[str]:
        """Sends `stop_signal` to an observe once `moment` holds; names the containers left."""
        observing = subprocess.Popen(
            [*self.compose_command(), "run", "-T", "--rm", OBSERVER_SERVICE],
            env=self.environment(self.config_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert observing.stdin is not None
        observing.stdin.write(digest_key + "\n")
        observing.stdin.flush()
        deadline = time.monotonic() + OBSERVER_START_SECONDS
        while not moment(observing) and observing.poll() is None:
            expect(time.monotonic() < deadline, f"the observe never reached {moment.__name__}")
            time.sleep(0.05)
        observing.send_signal(stop_signal)
        stdout, stderr = observing.communicate(timeout=OBSERVER_START_SECONDS)
        self.outputs.append(stdout + stderr)
        # A stopped compose stops relaying at once, so stdout may hold part of an inventory and
        # only the exit tells it from one; a signal compose takes this early may be lost, and the
        # observe completes. The inventory is one JSON object on one line
        # (json.dump escapes every newline), so it is whole exactly when it ends in "}\n".
        expect(
            observing.returncode != 0 or stdout.endswith("}\n"),
            f"the observe stopped by {stop_signal.name} {moment.__name__} exited 0 with "
            f"{len(stdout)} bytes of a cut inventory",
        )
        deadline = time.monotonic() + LOGOUT_TIMEOUT_SECONDS
        while observer_containers() and time.monotonic() < deadline:
            time.sleep(0.5)
        return observer_containers()

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

    def down(self) -> bool:
        """Removes the stack, volumes included, and says whether nothing of it is left."""
        remove_one_offs()
        result = self.compose("down", "--volumes", "--remove-orphans", bootstrap=True)
        left = leftovers()
        removed = result.returncode == 0 and not left
        if removed:
            status = "removed"
        elif result.returncode != 0:
            status = f"FAILED ({tail(result.stderr)})"
        else:
            status = f"FAILED (still there: {', '.join(left)})"
        print(f"teardown: stack {PROJECT} {status}", flush=True)
        return removed

    def see_env(self, container: str) -> None:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}} {{json .Config.Env}}", container],
            capture_output=True,
            text=True,
            check=True,
        )
        name, environment = inspected.stdout.split(" ", 1)
        self.seen_env.append((name.lstrip("/"), json.loads(environment)))

    def see_running(self) -> None:
        """Records the Config.Env and the logs of every container the stack runs right now."""
        listed = self.compose("ps", "--quiet")
        expect(listed.returncode == 0, f"compose ps failed:\n{tail(listed.stderr)}")
        container_ids = listed.stdout.split()
        expect(bool(container_ids), "compose ps lists no running container")
        for container_id in container_ids:
            self.see_env(container_id)
        logs = self.compose("logs", "--no-log-prefix")
        expect(logs.returncode == 0, f"compose logs failed:\n{tail(logs.stderr)}")

    def logs(self, service: str) -> str:
        return self.compose("logs", "--no-log-prefix", service).stdout


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def observer_containers(*states: str) -> list[str]:
    """The observer's containers, of any state unless `states` names some."""
    listed = docker(
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"label={PROJECT_LABEL}",
        "--filter",
        f"label=com.docker.compose.service={OBSERVER_SERVICE}",
        *(option for state in states for option in ("--filter", f"status={state}")),
    )
    return listed.stdout.split()


Observing = subprocess.Popen[str]


# Not as soon as the container exists: a signal compose takes while it still creates the
# container can leave that container behind.
def as_its_container_runs(_observing: Observing) -> bool:
    return bool(observer_containers("running"))


def as_the_inventory_arrives(observing: Observing) -> bool:
    return bool(select.select([observing.stdout], [], [], 0)[0])


def remove_one_offs() -> None:
    # `compose down` skips a one-off `compose run` container that is still running, as an apply
    # is when a SIGTERM kills its compose client.
    listed = docker(
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"label={PROJECT_LABEL}",
        "--filter",
        "label=com.docker.compose.oneoff=True",
    )
    one_offs = listed.stdout.split()
    if one_offs:
        docker("container", "rm", "--force", *one_offs)


def leftovers() -> list[str]:
    """Names every container, network and volume of the probe's project that still exists."""
    left = []
    for kind, all_flag, name in (
        ("container", ["--all"], "{{.Names}}"),
        ("network", [], "{{.Name}}"),
        ("volume", [], "{{.Name}}"),
    ):
        listed = docker(
            kind, "ls", *all_flag, "--filter", f"label={PROJECT_LABEL}", "--format", name
        )
        if listed.returncode != 0:
            left.append(f"{kind}s unlisted ({tail(listed.stderr)})")
        left += [f"{kind} {found}" for found in listed.stdout.split()]
    return left


def tail(output: str, lines: int = 15) -> str:
    """The last lines of a command's output, without Java stack frames."""
    meaningful = [line for line in output.strip().splitlines() if not line.startswith("\tat ")]
    return "\n".join(meaningful[-lines:])


def free_loopback_ports(count: int) -> list[int]:
    """`count` distinct loopback ports nothing listens on, which the kernel handed out together."""
    sockets = [socket.socket() for _ in range(count)]
    try:
        for held in sockets:
            held.bind((LOOPBACK_ADDRESS, 0))
        return [held.getsockname()[1] for held in sockets]
    finally:
        for held in sockets:
            held.close()


def free_subnet() -> str:
    """The first of SUBNET_CANDIDATES that no Docker network on this host overlaps."""
    listed = docker("network", "ls", "--quiet")
    expect(listed.returncode == 0, f"docker network ls failed:\n{tail(listed.stderr)}")
    ids = listed.stdout.split()
    taken: list[ipaddress.IPv4Network] = []
    if ids:
        inspected = docker(
            "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids
        )
        expect(
            inspected.returncode == 0, f"docker network inspect failed:\n{tail(inspected.stderr)}"
        )
        taken = [
            network
            for network in map(ipaddress.ip_network, inspected.stdout.split())
            if isinstance(network, ipaddress.IPv4Network)
        ]
    for candidate in SUBNET_CANDIDATES:
        if not any(ipaddress.ip_network(candidate).overlaps(network) for network in taken):
            return candidate
    raise ProbeFailure(f"no free /24 among {SUBNET_CANDIDATES[0]} ... {SUBNET_CANDIDATES[-1]}")


def wait_until_ready(admin_url: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if http("GET", f"{admin_url}/realms/master").status == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    raise ProbeFailure(f"Keycloak was not ready on {admin_url} after {READY_TIMEOUT_SECONDS}s")


# --- Admin API --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admin:
    token: str
    admin_url: str

    def call(self, method: str, path: str, json_body: Any = None) -> Response:
        return http(
            method, f"{self.admin_url}/admin/realms{path}", json_body=json_body, bearer=self.token
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
        group = self.get(f"/{REALM}/group-by-path{group_path}")
        self.must("PUT", f"/{REALM}/users/{user_id}/groups/{group['id']}")

    def group_paths(self, user_id: str) -> list[str]:
        return sorted(group["path"] for group in self.get(f"/{REALM}/users/{user_id}/groups"))

    def admin_event_count(self, realm: str) -> int:
        return len(self.get(f"/{realm}/admin-events?first=0&max=100000"))

    def find(self, path: str, key: str, value: str) -> dict[str, Any]:
        found = [item for item in self.get(path) if item.get(key) == value]
        expect(len(found) == 1, f"GET {path} holds {len(found)} items with {key} {value}")
        return found[0]


def hardcoded_claim(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {"claim.name": name, "claim.value": "console", "access.token.claim": "true"},
    }


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


def has_field(page: Response, field_name: str) -> bool:
    collector = FormCollector()
    collector.feed(page.text)
    return any(field_name in inputs for _action, inputs in collector.forms)


def load_go_live() -> ModuleType:
    """keycloak/go-live as a module (it has no .py suffix), as the unit tests load it."""
    loader = importlib.machinery.SourceFileLoader(
        "keycloak_go_live", str(REPO / "keycloak/go-live")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through it
    loader.exec_module(module)
    return module


def page_message(page: Response) -> str:
    """The feedback a Keycloak page shows the user, for a failure message."""
    found = re.search(
        r'(?:kc-feedback-text|kc-error-message|class="instruction")[^>]*>\s*(?:<[^>]+>\s*)*'
        r"([^<]{3,200})",
        page.text,
    )
    return found.group(1).strip() if found else f"no feedback text ({len(page.body)} bytes)"


def totp(secret: str, period_offset: int = 0) -> str:
    """RFC 6238 code for Keycloak's default policy: HmacSHA1, 6 digits, 30 s, the raw secret."""
    counter = int(time.time()) // 30 + period_offset
    # Keycloak's default OTP policy (RFC 6238's default) is HmacSHA1; the probe answers it and
    # chooses no hash.
    digest = hmac.new(
        secret.encode(),
        struct.pack(">Q", counter),
        hashlib.sha1,  # NOSONAR(S4790)
    ).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def within_keycloak(
    response: Response, opener: OpenerDirector, admin_console_redirect: str
) -> Response:
    """Follows Keycloak's own redirects (a required action, say) as a browser would, and stops
    at the redirect back to the admin console."""
    while response.status in (302, 303):
        location = response.headers["Location"]
        if location.startswith(admin_console_redirect):
            break
        response = http("GET", location, opener=opener)
    return response


# --- Proofs -------------------------------------------------------------------------------------


@dataclass
class Probe:
    stack: Stack
    secret_values: dict[str, str] = field(default_factory=dict)
    member_password: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    evidence: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        """Prints a line of evidence and keeps it, so proof 7 can check it for secrets."""
        self.evidence.append(line)
        print(line, flush=True)

    def held(self, proof: str, detail: str) -> None:
        self.say(f"PROOF {proof} held: {detail}")

    def through_tunnel(self, method: str, path: str, **kwargs: Any) -> Response:
        return tunnel_request(self.stack.edge_url, method, path, **kwargs)

    def through_tunnel_url(self, method: str, url: str, **kwargs: Any) -> Response:
        return tunnel_request_url(self.stack.edge_url, method, url, **kwargs)

    def keep_secret(self, name: str, value: str) -> str:
        """Registers a credential the probe made or received, so proof 7 looks for it too."""
        self.secret_values[name] = value
        return value

    def tokens_from(self, holder: str, response: Response) -> dict[str, Any]:
        """Reads a token response and registers every token in it, so proof 7 looks for each."""
        expect(
            response.status == 200,
            f"the token request for {holder} answered {response.status}: {response.text}",
        )
        tokens = response.json()
        for kind, value in tokens.items():
            if kind.endswith("_token"):
                self.keep_secret(f"{holder} {kind}", value)
        return tokens

    def admin_with_password(self, username: str, password: str) -> Admin:
        form = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        }
        tokens = self.tokens_from(username, http("POST", self.stack.master_token_url, form=form))
        return Admin(tokens["access_token"], self.stack.admin_url)

    def admin_with_client(self, secret: str) -> Admin:
        form = {
            "grant_type": "client_credentials",
            "client_id": "config-cli",
            "client_secret": secret,
        }
        tokens = self.tokens_from(
            "config-cli", http("POST", self.stack.master_token_url, form=form)
        )
        return Admin(tokens["access_token"], self.stack.admin_url)

    def start_with_bootstrap_admin(self) -> Admin:
        self.secret_values = self.stack.write_secrets()
        self.stack.copy_config(self.stack.config_dir)
        self.stack.write_traefik_config()
        self.stack.up(bootstrap=True)
        wait_until_ready(self.stack.admin_url)
        self.stack.move_master_to_admin_entrypoint()
        self.say("stack: up with the temporary bootstrap admin, master on the admin entrypoint")
        return self.admin_with_password(
            "temp-admin", self.secret_values["bootstrap-admin-password"]
        )

    def apply_and_restart(self) -> Admin:
        self.stack.must_apply("fresh apply", bootstrap=True)
        # The bootstrap keycloak container and its logs disappear when it is recreated below.
        self.stack.see_running()

        self.stack.up("keycloak")
        wait_until_ready(self.stack.admin_url)
        # The config-cli client exists only if the fresh apply created the master configuration.
        admin = self.admin_with_client(self.secret_values["config-cli-secret"])
        expect(admin.get(f"/{REALM}")["realm"] == REALM, f"no {REALM} realm")
        return admin

    def run(self) -> None:
        bootstrap_admin = self.start_with_bootstrap_admin()
        self.missing_variable_fails_loudly(bootstrap_admin)
        admin = self.apply_and_restart()
        self.held(
            "3a",
            "a fresh apply created the master configuration (the config-cli client signs in "
            "after a restart without the bootstrap overlay) and the hallucinai realm",
        )

        member_id = self.seed_member(admin)
        self.apply_removes_what_the_observer_names(admin, member_id)
        self.public_issuer_and_paths()
        self.go_live_creates_the_admin(admin)
        self.admin_console_only_on_loopback(admin)
        self.changed_yaml_applies(admin, member_id)
        rp_secret = self.create_probe_rp(admin)
        self.member_token_carries_groups_and_audience(rp_secret)
        self.disabled_user_refused_on_refresh(admin, rp_secret)
        self.backchannel_logout_reaches_rp(admin, rp_secret)
        self.only_logins_and_failed_logins_are_stored(admin, rp_secret)
        for client in APP_CLIENTS:
            self.app_client(admin, client)
        self.agent_service_stays_audience_only(admin)
        self.the_observer_door(admin)
        self.no_secret_leaked()

    def hold(self) -> None:
        """Brings the stack up as `run` does, runs no proof and keeps it until told to stop."""
        self.start_with_bootstrap_admin()
        self.apply_and_restart()
        keep_stack_until_told_to_stop(self.ready_line())

    def ready_line(self) -> str:
        details = {
            "admin_url": self.stack.admin_url,
            "edge_url": self.stack.edge_url,
            "realm": REALM,
            "public_issuer": PUBLIC_ISSUER,
            "admin_client_id": "config-cli",
            "admin_client_secret_file": str(self.stack.secrets_dir / "config-cli-secret"),
        }
        return f"{READY_MARK} {json.dumps(details)}"

    def missing_variable_fails_loudly(self, admin: Admin) -> None:
        broken = self.stack.copy_config(self.stack.scratch / "config-missing-variable")
        realm_file = broken / REALM_FILE
        realm_file.write_text(
            realm_file.read_text() + "displayName: $(env:PROBE_UNDEFINED_VARIABLE)\n"
        )
        result = self.stack.apply(bootstrap=True, config_dir=broken)
        output = result.stdout + result.stderr
        expect(result.returncode != 0, "an apply with an undefined variable exited 0")
        expect("PROBE_UNDEFINED_VARIABLE" in output, "the failure does not name the variable")
        expect(admin.call("GET", f"/{REALM}").status == 404, "the failed apply left a realm")
        self.held(
            "3b",
            f"an undefined variable fails the apply (exit {result.returncode}, names "
            "PROBE_UNDEFINED_VARIABLE) and creates no realm",
        )

    def seed_member(self, admin: Admin) -> str:
        """Stands in for a person and a membership the operator manages in the admin console."""
        password = self.keep_secret("probe-member password", self.member_password)
        member_id = admin.create_user(REALM, "probe-member", password)
        admin.join_group(member_id, MEMBER_GROUP)
        self.say(f"seed: probe-member in {MEMBER_GROUP} through the admin API")
        return member_id

    def observe(self, digest_key: str) -> Inventory:
        result = self.stack.observe(digest_key)
        expect(
            result.returncode == 0, f"an observe exited {result.returncode}: {tail(result.stderr)}"
        )
        return json.loads(result.stdout)

    def apply_removes_what_the_observer_names(self, admin: Admin, member_id: str) -> None:
        digest_key = self.keep_secret("the observer's digest key", new_digest_key())
        events = admin.admin_event_count(REALM)
        first, second = self.observe(digest_key), self.observe(digest_key)
        expect(change_lines(first, second) == [], "two observes of an unchanged Keycloak differ")
        expect(admin.admin_event_count(REALM) == events, f"an observe left admin events in {REALM}")

        self.make_console_changes(admin)
        before = self.observe(digest_key)
        output = self.stack.must_apply("second apply", cache=False)
        expect("checksum" not in output.lower(), "the second apply consulted the checksum cache")
        after = self.observe(digest_key)
        removed = change_lines(before, after)
        expect(
            removed == REMOVED_BY_THE_APPLY,
            f"the observer lists {removed}, not the console changes the apply removes",
        )
        expect(admin.group_paths(member_id) == [MEMBER_GROUP], "the seeded membership changed")
        self.stack.must_apply("third apply", cache=False)
        reapplied = self.observe(digest_key)
        again = change_lines(after, reapplied)
        expect(again == [], f"a re-apply with IMPORT_CACHE_ENABLED=false changed {again}")
        for line in removed:
            self.say(f"observer: {line}")
        self.held(
            "3c",
            "two observes are equal and leave no admin event; a second apply with "
            f"IMPORT_CACHE_ENABLED=false removes exactly the {len(removed)} console changes the "
            "observer lists above in both realms, keeps the console client, identity provider, "
            "component, realm roles and the agent-service mapper, and keeps probe-member in "
            f"{MEMBER_GROUP}; a third apply lists nothing",
        )
        self.realm_settings_as_decided(admin, reapplied)

    def make_console_changes(self, admin: Admin) -> None:
        """What an operator does in the admin console: five changes inside declared objects or
        realms that the apply removes, and five additions that survive it."""
        groups_scope = admin.find(f"/{REALM}/client-scopes", "name", "groups")
        admin.create(
            f"/{REALM}/client-scopes/{groups_scope['id']}/protocol-mappers/models",
            hardcoded_claim(f"{CONSOLE_NAME}-mapper"),
        )
        for realm in ("master", REALM):
            admin.create(f"/{realm}/roles", {"name": f"{CONSOLE_NAME}-role"})
        config_cli = admin.find("/master/clients?clientId=config-cli", "clientId", "config-cli")
        account = admin.get(f"/master/clients/{config_cli['id']}/service-account-user")
        console_role = admin.get(f"/master/roles/{CONSOLE_NAME}-role")
        admin.must("POST", f"/master/users/{account['id']}/role-mappings/realm", [console_role])
        admin.create(
            f"/master/authentication/flows/{FORMS_FLOW}/executions/execution",
            {"provider": "auth-otp-form"},
        )
        admin.create(
            "/master/authentication/flows",
            {"alias": CONSOLE_FLOW, "providerId": "basic-flow", "topLevel": True, "builtIn": False},
        )
        parent = admin.get(f"/{REALM}/group-by-path{CONSOLE_PARENT}")
        admin.create(f"/{REALM}/groups/{parent['id']}/children", {"name": CONSOLE_SUBGROUP_NAME})
        password = self.keep_secret("probe-console-member password", secrets.token_urlsafe(16))
        admin.join_group(
            admin.create_user(REALM, "probe-console-member", password), CONSOLE_SUBGROUP
        )
        self.make_survivors(admin)

    def make_survivors(self, admin: Admin) -> None:
        attribute = self.keep_secret("probe-console client attribute", secrets.token_urlsafe(24))
        client_id = admin.create(
            f"/{REALM}/clients",
            {
                "clientId": f"{CONSOLE_NAME}-client",
                "publicClient": False,
                "serviceAccountsEnabled": False,
                "attributes": {"probe.console.secret": attribute},
            },
        )
        secret = admin.get(f"/{REALM}/clients/{client_id}/client-secret")["value"]
        self.keep_secret("probe-console client secret", secret)
        idp_secret = self.keep_secret("console-idp clientSecret", secrets.token_urlsafe(24))
        admin.create(
            f"/{REALM}/identity-provider/instances",
            {
                "alias": "console-idp",
                "providerId": "oidc",
                "enabled": True,
                "trustEmail": False,
                "config": {
                    "clientId": "console",
                    "clientSecret": idp_secret,
                    "clientAuthMethod": "client_secret_post",
                    "authorizationUrl": "https://idp.probe.invalid/auth",
                    "tokenUrl": "https://idp.probe.invalid/token",
                },
            },
        )
        admin.create(
            f"/{REALM}/components",
            {
                "name": f"{CONSOLE_NAME}-component",
                "providerId": "max-clients",
                "providerType": (
                    "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
                ),
                "subType": "anonymous",
                "parentId": admin.get(f"/{REALM}")["id"],
                "config": {"max-clients": ["50"]},
            },
        )
        agent = admin.find(f"/{REALM}/clients?clientId=agent-service", "clientId", "agent-service")
        admin.create(
            f"/{REALM}/clients/{agent['id']}/protocol-mappers/models",
            hardcoded_claim(f"{CONSOLE_NAME}-agent-mapper"),
        )

    def realm_settings_as_decided(self, admin: Admin, inventory: Inventory) -> None:
        realm = admin.get(f"/{REALM}")
        master = admin.get("/master")
        expected = {
            "webAuthnPolicyRpId": DOMAIN,
            "webAuthnPolicyPasswordlessRpId": DOMAIN,
            "accessTokenLifespan": 300,
            "ssoSessionMaxLifespan": 2592000,
            "ssoSessionIdleTimeout": 604800,
            "bruteForceProtected": True,
            "eventsEnabled": True,
            "eventsExpiration": 7776000,
            "adminEventsEnabled": True,
            "loginTheme": THEME,
            "accountTheme": THEME,
            "internationalizationEnabled": True,
            "defaultLocale": "en",
        }
        wrong = {key: realm.get(key) for key, value in expected.items() if realm.get(key) != value}
        if sorted(realm.get("supportedLocales", [])) != ["de", "en"]:
            wrong["supportedLocales"] = realm.get("supportedLocales")
        admin_expiry = realm.get("attributes", {}).get("adminEventsExpiration")
        if admin_expiry != "31536000":
            wrong["attributes.adminEventsExpiration"] = admin_expiry
        if sorted(realm.get("enabledEventTypes", [])) != ["LOGIN", "LOGIN_ERROR"]:
            wrong["enabledEventTypes"] = realm.get("enabledEventTypes")
        if realm.get("adminEventsDetailsEnabled"):
            wrong["adminEventsDetailsEnabled"] = True
        providers = inventory[REALM].get("identity provider", {}).values()
        if any(provider["fields"]["trustEmail"] for provider in providers):
            wrong["identityProviders.trustEmail"] = True
        master_expected = {
            "bruteForceProtected": True,
            "browserFlow": "browser-mfa",
            "directGrantFlow": "direct-grant-mfa",
            "webAuthnPolicyRpId": "localhost",
            "loginTheme": None,
            "accountTheme": None,
            "internationalizationEnabled": False,
        }
        for key, value in master_expected.items():
            if master.get(key) != value:
                wrong[f"master.{key}"] = master.get(key)
        expect(not wrong, f"realm settings differ from the decisions: {wrong}")
        self.held(
            "8",
            "rpId hallucinai.de, access token 300 s, SSO max 30 d / idle 7 d, login events "
            "90 d (LOGIN and LOGIN_ERROR only), admin events 1 y without details (no client "
            "representation in them), "
            "brute force on in both realms, "
            f"{len(providers)} identity providers (so no trustEmail=true), master browser flow "
            "browser-mfa, direct grant direct-grant-mfa, rpId localhost, "
            "master theme and language untouched; hallucinai themes and de/en as decided",
        )

    def public_issuer_and_paths(self) -> None:
        discovery = self.through_tunnel("GET", f"{REALM_PATH}/.well-known/openid-configuration")
        expect(discovery.status == 200, f"public discovery answered {discovery.status}")
        issuer = discovery.json()["issuer"]
        expect(issuer == PUBLIC_ISSUER, f"public issuer is {issuer}")
        login_path = f"{REALM_PATH}/protocol/openid-connect/auth?" + urlencode(
            {
                "client_id": "account-console",
                "redirect_uri": f"https://{PUBLIC_HOST}{REALM_PATH}/account/",
                "response_type": "code",
                "scope": "openid",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
            }
        )
        login_page = self.through_tunnel("GET", login_path)
        expect(login_page.status == 200, f"public login page answered {login_page.status}")
        expect("HTTPS required" not in login_page.text, "the login page demands HTTPS")
        login_resource = self.themed_resource(login_page, "login")
        german = self.through_tunnel("GET", login_path, headers={"Accept-Language": "de"})
        expect(
            "Passwort" in german.text and 'lang="de"' in german.text,
            "the login page does not speak German for Accept-Language: de",
        )
        expect(self.has_language_switcher(german), "the German login page has no language switcher")
        for asked in ({"Accept-Language": "fr"}, {}):
            english = self.through_tunnel("GET", login_path, headers=asked)
            expect(
                'lang="en"' in english.text and "Password" in english.text,
                f"the login page does not speak English for {asked or 'no language'}",
            )
        account_page = self.through_tunnel("GET", f"{REALM_PATH}/account/")
        expect(account_page.status == 200, f"the account console answered {account_page.status}")
        account_resource = self.themed_resource(account_page, "account")
        self.held(
            "1",
            f"public discovery issuer {issuer}; the login page renders without HTTPS error, "
            f"links {login_resource} and the account console links {account_resource} "
            "(both 200); German for Accept-Language de with a language switcher, English for "
            "fr and for none",
        )

        localization = self.through_tunnel("GET", f"/resources/{REALM}/account/en")
        expect(
            localization.status == 200,
            f"the hallucinai account localization answered {localization.status}",
        )

        refused = [
            (PUBLIC_HOST, "/realms/master/.well-known/openid-configuration"),
            (PUBLIC_HOST, "/realms/master/protocol/openid-connect/token"),
            (PUBLIC_HOST, "/resources/master/login/en"),
            (PUBLIC_HOST, "/resources/master/admin/en"),
            (PUBLIC_HOST, "/admin/"),
            (PUBLIC_HOST, "/admin/master/console/"),
            (PUBLIC_HOST, "/metrics"),
            (PUBLIC_HOST, "/health"),
            (PUBLIC_HOST, "/realms/hallucinai/../master/.well-known/openid-configuration"),
            ("localhost", f"{REALM_PATH}/.well-known/openid-configuration"),
            ("localhost", "/admin/master/console/"),
        ]
        for host, path in refused:
            answer = self.through_tunnel("GET", path, host=host)
            expect(
                answer.status == 404 and answer.body == TRAEFIK_NOT_FOUND,
                f"the edge entrypoint {host}{path} answered {answer.status}, not Traefik's 404",
            )
        account_page = f"https://{PUBLIC_HOST}{REALM_PATH}/account"
        for bare_address in ("/", "/?x=1"):
            redirect = self.through_tunnel("GET", bare_address)
            expect(
                (redirect.status, redirect.headers.get("Location")) == (302, account_page),
                f"the edge entrypoint {PUBLIC_HOST}{bare_address} answered {redirect.status} "
                f"to {redirect.headers.get('Location')}, not 302 to {account_page}",
            )
        # Inside the admitted prefix, so only the entrypoint's encoded-character rule stops it.
        encoded = self.through_tunnel("GET", "/realms/hallucinai/..%2Fmaster/")
        expect(
            encoded.status == 400,
            f"an encoded slash on the edge entrypoint answered {encoded.status}",
        )
        self.held(
            "2a",
            "the edge entrypoint answers /realms/hallucinai/, its localization and the versioned "
            f"static resources; Traefik refuses {len(refused)} master realm and master resources, "
            "admin, metrics, health, traversal and Host: localhost requests (404), redirects the "
            "bare / and /?x=1 to the account page (302) "
            "and refuses an encoded slash inside the admitted prefix (400)",
        )

    @staticmethod
    def has_language_switcher(page: Response) -> bool:
        return "login-select-toggle" in page.text

    def themed_resource(self, page: Response, area: str) -> str:
        """The first /resources/<version>/<area>/hallucinai/ link of a page, fetched and 200."""
        link = re.search(
            rf'(?:href|src)="([^"]*/resources/[a-z0-9]{{5}}/{area}/{THEME}/[^"]+)"', page.text
        )
        expect(link is not None, f"the {area} page links nothing under {area}/{THEME}/")
        path = link.group(1)
        resource = self.through_tunnel("GET", path[path.index("/resources/") :])
        expect(resource.status == 200, f"{path} answered {resource.status}")
        self.say(f"{path} Cache-Control: {resource.headers.get('Cache-Control')}")
        return path

    def admin_console_password_step(
        self, holder: str, username: str, password: str
    ) -> tuple[OpenerDirector, str, Response]:
        """Opens the admin console's login and posts the password like a browser does; returns the
        session's opener, the PKCE verifier and the page that follows the password. The verifier
        and the session cookies are credentials as well, so proof 7 looks for them too."""
        opener = new_opener(
            ReceivedCookies(lambda name, value: self.keep_secret(f"{holder} {name}", value))
        )
        verifier = self.keep_secret(f"{holder} PKCE verifier", secrets.token_urlsafe(48))
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        query = urlencode(
            {
                "client_id": "security-admin-console",
                "redirect_uri": self.stack.admin_console_redirect,
                "response_type": "code",
                "scope": "openid",
                "state": secrets.token_urlsafe(8),
                "nonce": secrets.token_urlsafe(8),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        login_page = http(
            "GET", f"{self.stack.master_issuer}/protocol/openid-connect/auth?{query}", opener=opener
        )
        expect(login_page.status == 200, f"admin login page answered {login_page.status}")
        action, fields = form_with(login_page, "username")
        fields |= {"username": username, "password": password}
        factor_page = within_keycloak(
            http("POST", action, form=fields, opener=opener),
            opener,
            self.stack.admin_console_redirect,
        )
        expect(
            factor_page.status == 200,
            f"password step answered {factor_page.status}: {page_message(factor_page)}",
        )
        return opener, verifier, factor_page

    def admin_console_login(
        self, holder: str, username: str, password: str, second_factor: Any
    ) -> dict[str, Any]:
        """Signs in to the admin console like a browser does and returns the token response.

        `second_factor(page)` answers the page that follows the password: it returns the form
        action and the fields to post. The authorization code is a credential too.
        """
        opener, verifier, factor_page = self.admin_console_password_step(holder, username, password)
        action, fields = second_factor(factor_page)
        done = within_keycloak(
            http("POST", action, form=fields, opener=opener),
            opener,
            self.stack.admin_console_redirect,
        )
        location = done.headers.get("Location", "")
        expect(
            done.status == 302 and location.startswith(self.stack.admin_console_redirect),
            f"second factor answered {done.status} without the console redirect: "
            f"{page_message(done)}",
        )
        code = parse_qs(urlsplit(location).query)["code"][0]
        self.keep_secret(f"{holder} authorization code", code)
        token_form = {
            "grant_type": "authorization_code",
            "client_id": "security-admin-console",
            "redirect_uri": self.stack.admin_console_redirect,
            "code": code,
            "code_verifier": verifier,
        }
        return self.tokens_from(
            holder, http("POST", self.stack.master_token_url, form=token_form, opener=opener)
        )

    def go_live_creates_the_admin(self, admin: Admin) -> None:
        """Proof 11 (#129): go-live's admin step, driven as its own steps (never `run()`, whose
        compose calls would reach the operator's project), makes an admin a real Keycloak accepts.
        Runs before proof 2b, whose probe-admin would make the step's done-state true."""
        listed = admin.get("/master/authentication/required-actions")
        enabled = {action["alias"]: action["enabled"] for action in listed}
        expect(
            all(enabled.get(alias) is True for alias in ADMIN_REQUIRED_ACTIONS),
            f"required actions enabled in master: "
            f"{ {alias: enabled.get(alias) for alias in ADMIN_REQUIRED_ACTIONS} }",
        )
        password = self.keep_secret("go-live admin password", secrets.token_urlsafe(16))
        step = self.go_live_admin_step(password, typed_answers=True)
        expect(
            not step.is_done(),
            "go-live's admin step is done before it ran: config-cli's service account is no admin",
        )
        step.do()
        expect(step.is_done(), "go-live's admin step is not done after it ran")

        user = admin.find(
            f"/master/users?username={GO_LIVE_ADMIN}&exact=true", "username", GO_LIVE_ADMIN
        )
        roles = [
            role["name"] for role in admin.get(f"/master/users/{user['id']}/role-mappings/realm")
        ]
        expect(
            user["enabled"] and "admin" in roles,
            f"go-live's admin: enabled {user['enabled']}, roles {roles}",
        )
        expect(
            sorted(user["requiredActions"]) == sorted(ADMIN_REQUIRED_ACTIONS),
            f"go-live's admin has the required actions {user['requiredActions']}",
        )
        grant = http(
            "POST",
            self.stack.master_token_url,
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": GO_LIVE_ADMIN,
                "password": password,
            },
        )
        expect(
            grant.status == 401 and b"access_token" not in grant.body,
            f"a password grant of go-live's admin without TOTP answered {grant.status}",
        )

        opener, _verifier, first_page = self.admin_console_password_step(
            "go-live admin console", GO_LIVE_ADMIN, password
        )
        action, fields = form_with(first_page, "totpSecret")
        self.keep_secret("go-live admin TOTP seed", fields["totpSecret"])
        after_totp = within_keycloak(
            http(
                "POST",
                action,
                form=fields | {"totp": totp(fields["totpSecret"]), "userLabel": "probe"},
                opener=opener,
            ),
            opener,
            self.stack.admin_console_redirect,
        )
        expect(
            after_totp.status == 200 and has_field(after_totp, "attestationObject"),
            f"after the TOTP the next page was not the passkey page: {after_totp.status}, "
            f"{page_message(after_totp)}",
        )
        expect(
            not has_field(after_totp, "password-new"),
            "the sign-in asks for a new password although the password is permanent",
        )

        rerun = self.go_live_admin_step(password, typed_answers=False)
        expect(rerun.is_done(), "go-live's admin step is not done on its second run")
        again = self.admin_console_password_step(
            "go-live admin second console", GO_LIVE_ADMIN, password
        )[2]
        expect(
            not has_field(again, "password"), "the typed password is refused on the second sign-in"
        )
        expect(
            len(admin.get(f"/master/users?username={GO_LIVE_ADMIN}")) == 1,
            "the second run made another user",
        )

        master = admin.get("/master")
        expect(
            not master.get("adminEventsEnabled") and not master.get("adminEventsDetailsEnabled"),
            "master stores admin events, so the create body could be kept",
        )
        stored = json.dumps(admin.get("/master/admin-events?first=0&max=100000"))
        expect(password not in stored, "the typed password is in master's admin events")
        self.held(
            "11",
            f"go-live's admin step (step 6, driven on its own) created {GO_LIVE_ADMIN} with the "
            "realm role admin and CONFIGURE_TOTP and webauthn-register pending (both enabled in "
            "master); the password grant without TOTP is refused (401); the browser sign-in with "
            "the typed password shows the TOTP enrolment, then the passkey page and no password "
            "update; a second run asks nothing, adds no user and the password still signs in; "
            "master's admin events are off and hold no password. Passkey registration itself is "
            "not driven (no authenticator): the operator's go-live is its proof",
        )

    def go_live_admin_step(self, password: str, *, typed_answers: bool) -> Any:
        """Step 6 of keycloak/go-live over the probe's stack. Its `run` raises on any call: no
        compose command may run, because go-live's compose targets the operator's project. A
        step that must not ask makes any prompt a failure."""
        go_live = load_go_live()
        # go-live pins the live entrypoint addresses; this stack's are its own.
        go_live.ADMIN_URL = self.stack.admin_url
        go_live.EDGE_URL = self.stack.edge_url
        go_live.MASTER_ISSUER = self.stack.master_issuer
        go_live.MASTER_TOKEN_URL = self.stack.master_token_url
        go_live.ADMIN_REALMS = f"{self.stack.admin_url}/admin/realms"
        for name, value in vars(go_live).items():
            expect(
                not isinstance(value, str)
                or not any(live in value for live in LIVE_GO_LIVE_ADDRESSES),
                f"go-live's {name} still names the live edge: {value}",
            )

        def no_command(*_args: object, **_kwargs: object) -> object:
            raise ProbeFailure("go-live's admin step ran a command")

        def answer(prompt: str) -> str:
            expect(typed_answers, f"go-live's admin step asked: {prompt}")
            return ""

        def answer_hidden(prompt: str) -> str:
            expect(typed_answers, "go-live's admin step asked for a password")
            return password

        tools = go_live.Tools(
            run=no_command,
            ask=answer,
            ask_hidden=answer_hidden,
            default_user=lambda: GO_LIVE_ADMIN,
            say=self.say,
        )
        marks = go_live.switches.Switches(self.stack.scratch / "switches")
        steps = go_live.GoLive(tools, self.stack.secrets_dir, marks).steps()
        return next(step for step in steps if step.number == "6")

    def admin_console_only_on_loopback(self, admin: Admin) -> None:
        master_discovery = http(
            "GET", f"{self.stack.master_issuer}/.well-known/openid-configuration"
        )
        expect(
            master_discovery.status == 200, f"master discovery answered {master_discovery.status}"
        )
        issuer = master_discovery.json()["issuer"]
        expect(issuer == self.stack.master_issuer, f"master issuer is {issuer}")
        foreign_host = http(
            "GET", f"{self.stack.admin_url}/realms/hallucinai/", headers={"Host": PUBLIC_HOST}
        )
        expect(
            foreign_host.status == 404,
            f"the admin entrypoint with the public host answered {foreign_host.status}",
        )
        encoded = http("GET", f"{self.stack.admin_url}/admin%2Fmaster/console/")
        expect(
            encoded.status == 400,
            f"an encoded slash on the admin entrypoint answered {encoded.status}",
        )

        password = self.keep_secret("probe-admin password", secrets.token_urlsafe(16))
        admin_id = admin.create_user("master", "probe-admin", password)
        admin_role = admin.get("/master/roles/admin")
        admin.must("POST", f"/master/users/{admin_id}/role-mappings/realm", [admin_role])
        password_only = http(
            "POST",
            self.stack.master_token_url,
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "probe-admin",
                "password": password,
            },
        )
        expect(
            password_only.status == 401 and b"access_token" not in password_only.body,
            f"an admin-cli password grant without TOTP answered {password_only.status}",
        )
        enrolled: dict[str, str] = {}

        def enrol_totp(page: Response) -> tuple[str, dict[str, str]]:
            action, fields = form_with(page, "totpSecret")
            enrolled["secret"] = self.keep_secret("probe-admin TOTP seed", fields["totpSecret"])
            return action, fields | {"totp": totp(fields["totpSecret"]), "userLabel": "probe"}

        first_login = self.admin_console_login(
            "probe-admin console", "probe-admin", password, enrol_totp
        )
        first = claims(first_login["access_token"])
        expect(first["iss"] == self.stack.master_issuer, f"admin token issuer is {first['iss']}")

        def answer_otp(page: Response) -> tuple[str, dict[str, str]]:
            action, fields = form_with(page, "otp")
            # The next period's code: the first one was spent, and Keycloak's look-around
            # window of one period accepts the next.
            return action, fields | {"otp": totp(enrolled["secret"], period_offset=1)}

        second_login = self.admin_console_login(
            "probe-admin second console", "probe-admin", password, answer_otp
        )
        second = claims(second_login["access_token"])
        expect(
            second["iss"] == self.stack.master_issuer,
            f"second admin token issuer is {second['iss']}",
        )
        self.held(
            "2b",
            f"on the admin entrypoint the master issuer is {issuer}; a new admin's admin-cli "
            "password grant without TOTP is refused (401), the admin must enrol TOTP before the "
            "console login completes, the next login demands the code; it refuses the public host "
            "(404) and an encoded slash (400)",
        )

    def changed_yaml_applies(self, admin: Admin, member_id: str) -> None:
        changed = self.stack.copy_config(self.stack.scratch / "config-changed")
        realm_file = changed / REALM_FILE
        text = realm_file.read_text()
        expect("accessTokenLifespan: 300 " in text, "realm.yaml no longer sets 300 s as expected")
        text = text.replace(
            "accessTokenLifespan: 300 ", f"accessTokenLifespan: {CHANGED_ACCESS_TOKEN_LIFESPAN} "
        )
        text = text.replace("  - name: ki-abo\n", f"  - name: ki-abo\n  - name: {CHANGED_GROUP}\n")
        realm_file.write_text(text)
        # Stands in for a subgroup the operator makes in the admin console; README warns that the
        # apply deletes it, because realm.yaml does not list it.
        parent = admin.get(f"/{REALM}/group-by-path{CONSOLE_PARENT}")
        admin.create(f"/{REALM}/groups/{parent['id']}/children", {"name": CONSOLE_SUBGROUP_NAME})
        self.stack.must_apply("changed apply", config_dir=changed)
        realm = admin.get(f"/{REALM}")
        expect(
            realm["accessTokenLifespan"] == CHANGED_ACCESS_TOKEN_LIFESPAN,
            f"access token lifespan is {realm['accessTokenLifespan']} after the change",
        )
        expect(
            admin.call("GET", f"/{REALM}/group-by-path/{CHANGED_GROUP}").status == 200,
            f"group {CHANGED_GROUP} was not created",
        )
        expect(
            admin.call("GET", f"/{REALM}/group-by-path{CONSOLE_SUBGROUP}").status == 404,
            f"the console-made subgroup {CONSOLE_SUBGROUP} survived the apply",
        )
        expect(admin.group_paths(member_id) == [MEMBER_GROUP], "the seeded membership changed")
        self.held(
            "3d",
            f"a changed realm.yaml applies (access token {CHANGED_ACCESS_TOKEN_LIFESPAN} s, "
            f"group /{CHANGED_GROUP}) and deletes the console-made {CONSOLE_SUBGROUP} it does "
            f"not list; probe-member is still in {MEMBER_GROUP}",
        )

    def create_probe_rp(self, admin: Admin) -> str:
        representation = json.loads((REPO / "probe/keycloak/probe-rp-client.json").read_text())
        client_id = admin.create(f"/{REALM}/clients", representation)
        secret = admin.get(f"/{REALM}/clients/{client_id}/client-secret")["value"]
        return self.keep_secret("probe-rp client secret", secret)

    def password_grant(
        self,
        username: str,
        password: str,
        rp_secret: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.through_tunnel(
            "POST",
            PUBLIC_TOKEN_PATH,
            headers=headers,
            form={
                "grant_type": "password",
                "client_id": "probe-rp",
                "client_secret": rp_secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        return self.tokens_from(username, response)

    def member_token_carries_groups_and_audience(self, rp_secret: str) -> None:
        tokens = self.password_grant("probe-member", self.member_password, rp_secret)
        access = claims(tokens["access_token"])
        audience = audience_of(access)
        expect(access.get("iss") == PUBLIC_ISSUER, f"access token issuer is {access.get('iss')}")
        expect(MEMBER_GROUP in access.get("groups", []), f"groups claim is {access.get('groups')}")
        expect("agent-service" in audience, f"audience is {audience}")
        self.held(
            "4",
            f"probe-member's access token: iss {access['iss']}, groups {access['groups']}, "
            f"aud {audience}",
        )

    def disabled_user_refused_on_refresh(self, admin: Admin, rp_secret: str) -> None:
        password = self.keep_secret("probe-disabled password", secrets.token_urlsafe(16))
        user_id = admin.create_user(REALM, "probe-disabled", password)
        tokens = self.password_grant("probe-disabled", password, rp_secret)
        admin.must("PUT", f"/{REALM}/users/{user_id}", {"enabled": False})
        refresh = self.through_tunnel(
            "POST",
            PUBLIC_TOKEN_PATH,
            form={
                "grant_type": "refresh_token",
                "client_id": "probe-rp",
                "client_secret": rp_secret,
                "refresh_token": tokens["refresh_token"],
            },
        )
        error = refresh.json().get("error") if refresh.status == 400 else None
        expect(error == "invalid_grant", f"refresh after disabling answered {refresh.status}")
        self.held("5", "a disabled user's refresh is refused (400 invalid_grant)")

    def backchannel_logout_reaches_rp(self, admin: Admin, rp_secret: str) -> None:
        password = self.keep_secret("probe-logout password", secrets.token_urlsafe(16))
        user_id = admin.create_user(REALM, "probe-logout", password)
        self.password_grant("probe-logout", password, rp_secret)
        admin.must("POST", f"/{REALM}/users/{user_id}/logout")
        deadline = time.monotonic() + LOGOUT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for line in self.stack.logs("probe-rp").splitlines():
                if line.startswith("backchannel-logout "):
                    record = json.loads(line.removeprefix("backchannel-logout "))
                    if record["sub"] == user_id:
                        expect(record["iss"] == PUBLIC_ISSUER, f"logout iss is {record['iss']}")
                        self.held(
                            "6",
                            "an admin logout of probe-logout reached probe-rp over the compose "
                            f"network (iss {record['iss']}, sid present: {bool(record['sid'])})",
                        )
                        return
            time.sleep(1)
        raise ProbeFailure(f"probe-rp saw no back-channel logout within {LOGOUT_TIMEOUT_SECONDS}s")

    def only_logins_and_failed_logins_are_stored(self, admin: Admin, rp_secret: str) -> None:
        password = self.keep_secret("probe-events password", secrets.token_urlsafe(16))
        user_id = admin.create_user(REALM, "probe-events", password)
        forwarded = {
            "Forwarded": cloudflare_forwarded(CLIENT_ADDRESS),
            "X-Forwarded-For": FORGED_FORWARDED_FOR,
        }

        def wrong_password(headers: dict[str, str]) -> Response:
            return self.through_tunnel(
                "POST",
                PUBLIC_TOKEN_PATH,
                headers=headers,
                form={
                    "grant_type": "password",
                    "client_id": "probe-rp",
                    "client_secret": rp_secret,
                    "username": "probe-events",
                    "password": "not-the-password",
                },
            )

        tokens = self.password_grant("probe-events", password, rp_secret, forwarded)
        refused = wrong_password(forwarded)
        expect(refused.status == 400, f"a wrong password answered {refused.status}")
        logout = self.through_tunnel(
            "POST",
            PUBLIC_LOGOUT_PATH,
            form={
                "client_id": "probe-rp",
                "client_secret": rp_secret,
                "refresh_token": tokens["refresh_token"],
            },
        )
        expect(logout.status == 204, f"the logout answered {logout.status}")
        events_path = f"/{REALM}/events?user={user_id}"
        stored = stored_event_types(admin.get(events_path), CLIENT_ADDRESS)
        expect(
            stored == ["LOGIN", "LOGIN_ERROR"],
            f"probe-events has the stored events {stored}, not one LOGIN and one LOGIN_ERROR",
        )
        forwarded_ipv6 = {**forwarded, "Forwarded": cloudflare_forwarded(CLIENT_ADDRESS_IPV6)}
        refused_ipv6 = wrong_password(forwarded_ipv6)
        expect(refused_ipv6.status == 400, f"a wrong password answered {refused_ipv6.status}")
        newest = max(admin.get(events_path), key=lambda event: event["time"])
        stored_ipv6 = f"[{CLIENT_ADDRESS_IPV6}]"  # Keycloak keeps RFC 7239's brackets
        expect(
            newest["type"] == "LOGIN_ERROR" and newest.get("ipAddress") == stored_ipv6,
            f"the newest {newest['type']} event carries the IP address {newest.get('ipAddress')}, "
            f"not {stored_ipv6}",
        )
        self.held(
            "EV2",
            "a sign-in stored one LOGIN, a wrong password one LOGIN_ERROR, both with the "
            f"Forwarded client address {CLIENT_ADDRESS} and not the forged X-Forwarded-For "
            f"{FORGED_FORWARDED_FOR!r}, a wrong password from {CLIENT_ADDRESS_IPV6} one more "
            "LOGIN_ERROR with that IPv6 address in brackets, a logout none",
        )

    def app_client(self, admin: Admin, client: AppClient) -> None:
        """Proof 10 (#108, #161): an app's client does what #11 lists, through the public tunnel."""
        name = client.client_id
        username = f"probe-{name}-member"
        password = self.keep_secret(f"{username} password", secrets.token_urlsafe(16))
        admin.join_group(admin.create_user(REALM, username, password), client.group)
        client_secret = self.secret_values[client.secret_file]
        browser = new_opener(
            ReceivedCookies(lambda cookie, value: self.keep_secret(f"{name} {cookie}", value))
        )
        tokens = self.app_client_sign_in(client, browser, username, password, client_secret)
        groups = claims(tokens["id_token"]).get("groups", [])
        expect(client.group in groups, f"{name}'s ID token groups are {groups}")
        if client.agent_workload:
            self.person_token_is_no_workload(client, tokens["access_token"])
        self.app_client_refuses_what_it_does_not_use(client, client_secret)
        self.app_client_logout_pages(client, browser)
        self.app_client_representation(admin, client)
        if client.agent_workload:
            self.workload_token_carries_the_role(admin, client_secret, client)
        self.held(
            "10",
            f"a member of {client.group} signs in to {name} with PKCE and gets a code "
            f"at exactly {client.callback}, exchanged with the scratch secret for an ID token "
            f"with groups {groups}; no code challenge, a callback with a suffix, plain http, the "
            f"password grant, {'' if client.agent_workload else 'client_credentials, '}and the "
            "offline_access scope are refused; the "
            f"end-session page asks before logging out for {client.logout_redirect} and refuses "
            "an unregistered address and the origin root; the client has no front- or back-channel "
            "logout or optional scope, a base URL and shows in the console"
            + (
                f"; a person's token has the {AGENT_SERVICE} audience and no {AGENT_WORKLOAD_ROLE} "
                f"role, and its client_credentials token has {AGENT_WORKLOAD_ROLE}, "
                f"client_id and azp {name}, no group and the realm's access token lifespan to "
                "live (W1 to W3)"
                if client.agent_workload
                else "; it has no service account (W5)"
            ),
        )

    @staticmethod
    def app_client_authorization_query(client: AppClient, **changes: str) -> str:
        parameters = {
            "client_id": client.client_id,
            "redirect_uri": client.callback,
            "response_type": "code",
            "scope": "openid",
            "state": secrets.token_urlsafe(8),
            "nonce": secrets.token_urlsafe(8),
        }
        return urlencode({**parameters, **changes})

    def app_client_sign_in(
        self,
        client: AppClient,
        opener: OpenerDirector,
        username: str,
        password: str,
        client_secret: str,
    ) -> dict[str, Any]:
        """The authorization-code flow with S256 up to the ID token (cells T1, T2)."""
        name = client.client_id
        verifier = self.keep_secret(f"{name} PKCE verifier", secrets.token_urlsafe(48))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        query = self.app_client_authorization_query(
            client, code_challenge=challenge.rstrip(b"=").decode(), code_challenge_method="S256"
        )
        login_page = self.through_tunnel("GET", f"{PUBLIC_AUTH_PATH}?{query}", opener=opener)
        expect(login_page.status == 200, f"{name}'s login page answered {login_page.status}")
        action, fields = form_with(login_page, "username")
        signed_in = self.through_tunnel_url(
            "POST",
            action,
            form=fields | {"username": username, "password": password},
            opener=opener,
        )
        location = signed_in.headers.get("Location", "")
        callback = urlsplit(location)
        expect(
            signed_in.status == 302 and location.startswith(f"{client.callback}?"),
            f"sign-in answered {signed_in.status} with {callback._replace(query='').geturl()!r}, "
            f"not a code at {client.callback}: {page_message(signed_in)}",
        )
        code = self.keep_secret(f"{name} authorization code", parse_qs(callback.query)["code"][0])
        return self.tokens_from(
            username,
            self.through_tunnel(
                "POST",
                PUBLIC_TOKEN_PATH,
                form={
                    "grant_type": "authorization_code",
                    "client_id": name,
                    "client_secret": client_secret,
                    "redirect_uri": client.callback,
                    "code": code,
                    "code_verifier": verifier,
                },
            ),
        )

    def app_client_refuses_what_it_does_not_use(
        self, client: AppClient, client_secret: str
    ) -> None:
        """Cells T3 to T5: PKCE is required, the callback is exact, no other grant works."""
        without_challenge = self.through_tunnel(
            "GET", f"{PUBLIC_AUTH_PATH}?{self.app_client_authorization_query(client)}"
        )
        error = parse_qs(urlsplit(without_challenge.headers.get("Location", "")).query)
        expect(
            without_challenge.status == 302 and error.get("error") == ["invalid_request"],
            f"an auth request without code_challenge answered {without_challenge.status} "
            f"with {error}",
        )
        for redirect_uri in (
            f"{client.callback}/x",
            client.callback.replace("https", "http"),
        ):
            challenge = {"code_challenge": "x" * 43, "code_challenge_method": "S256"}
            query = self.app_client_authorization_query(
                client, redirect_uri=redirect_uri, **challenge
            )
            page = self.through_tunnel("GET", f"{PUBLIC_AUTH_PATH}?{query}")
            expect(
                page.status == 400 and "Invalid parameter: redirect_uri" in page.text,
                f"redirect_uri {redirect_uri} answered {page.status}: {page_message(page)}",
            )
        self.app_client_refuses_offline_access(client)
        refused_grants = [self.password_grant_of_the_member()]
        if not client.agent_workload:
            refused_grants.insert(0, {"grant_type": "client_credentials"})
        for grant in refused_grants:
            refused = self.through_tunnel(
                "POST",
                PUBLIC_TOKEN_PATH,
                form={"client_id": client.client_id, "client_secret": client_secret, **grant},
            )
            error = refused.json().get("error") if refused.status in (400, 401) else None
            expect(
                error == "unauthorized_client",
                f"{grant['grant_type']} on {client.client_id} answered {refused.status}",
            )

    @staticmethod
    def person_token_is_no_workload(client: AppClient, access_token: str) -> None:
        """Cell W3: a person's token from the app's login has the audience and groups, never the
        workload role."""
        access = claims(access_token)
        roles = access.get("realm_access", {}).get("roles", [])
        expect(
            AGENT_SERVICE in audience_of(access)
            and client.group in access.get("groups", [])
            and AGENT_WORKLOAD_ROLE not in roles,
            f"a person's access token from {client.client_id} has aud {audience_of(access)}, "
            f"groups {access.get('groups')}, realm roles {roles}",
        )

    def workload_token_carries_the_role(
        self, admin: Admin, client_secret: str, client: AppClient
    ) -> None:
        """Cells W1, W2: the client-credentials token is the app's workload identity and lives as
        long as the realm says (300 s, which proof 8 pins; the client has no override)."""
        name = client.client_id
        override = admin.find(f"/{REALM}/clients", "clientId", name)["attributes"].get(
            "access.token.lifespan"
        )
        realm_lifespan = admin.get(f"/{REALM}")["accessTokenLifespan"]
        tokens = self.tokens_from(
            f"{name} workload",
            self.through_tunnel(
                "POST",
                PUBLIC_TOKEN_PATH,
                form={
                    "grant_type": "client_credentials",
                    "client_id": name,
                    "client_secret": client_secret,
                },
            ),
        )
        access = claims(tokens["access_token"])
        roles = access.get("realm_access", {}).get("roles", [])
        lifespan = access["exp"] - access["iat"]
        expect(
            is_workload_identity_of(access, name) and not override and lifespan == realm_lifespan,
            f"{name}'s workload token has realm roles {roles}, aud {audience_of(access)}, "
            f"client_id {access.get('client_id')}, azp {access.get('azp')}, groups "
            f"{access.get('groups')}, lifespan {lifespan}s (realm {realm_lifespan}s, client "
            f"override {override})",
        )

    def agent_service_stays_audience_only(self, admin: Admin) -> None:
        """Cell W4: agent-service has every flow off."""
        found = admin.find(f"/{REALM}/clients", "clientId", AGENT_SERVICE)
        flows = (
            "standardFlowEnabled",
            "implicitFlowEnabled",
            "directAccessGrantsEnabled",
            "serviceAccountsEnabled",
        )
        on = [flow for flow in flows if found.get(flow)]
        expect(not on, f"{AGENT_SERVICE} has flows on: {on}")
        self.held("10", f"{AGENT_SERVICE} still has every flow off (W4)")

    def app_client_refuses_offline_access(self, client: AppClient) -> None:
        """Cell T13: no optional scope is attached, so no offline token can be asked for."""
        challenge = {"code_challenge": "x" * 43, "code_challenge_method": "S256"}
        query = self.app_client_authorization_query(
            client, scope="openid offline_access", **challenge
        )
        refused = self.through_tunnel("GET", f"{PUBLIC_AUTH_PATH}?{query}")
        location = refused.headers.get("Location", "")
        error = parse_qs(urlsplit(location).query)
        expect(
            refused.status == 302
            and error.get("error") == ["invalid_scope"]
            and "code" not in error,
            f"scope offline_access answered {refused.status} with {error}: {page_message(refused)}",
        )

    def password_grant_of_the_member(self) -> dict[str, str]:
        return {
            "grant_type": "password",
            "username": "probe-member",
            "password": self.member_password,
        }

    def app_client_logout_pages(self, client: AppClient, browser: OpenerDirector) -> None:
        """Cells T6, T7: with a session, the registered logout address asks before logging out and
        others fail."""

        def end_session(post_logout_redirect_uri: str) -> Response:
            query = urlencode(
                {
                    "client_id": client.client_id,
                    "post_logout_redirect_uri": post_logout_redirect_uri,
                }
            )
            return self.through_tunnel("GET", f"{PUBLIC_LOGOUT_PATH}?{query}", opener=browser)

        registered = end_session(client.logout_redirect)
        expect(
            registered.status == 200 and "Do you want to log out?" in registered.text,
            f"end session with {client.logout_redirect} answered {registered.status} to "
            f"{registered.headers.get('Location')}: {page_message(registered)}",
        )
        for unregistered_address in (f"{client.origin}/elsewhere", f"{client.origin}/"):
            unregistered = end_session(unregistered_address)
            expect(
                unregistered.status == 400,
                f"end session with unregistered {unregistered_address} answered "
                f"{unregistered.status}",
            )

    @staticmethod
    def app_client_representation(admin: Admin, client: AppClient) -> None:
        """Cell T8, #161 SA3-1: flags, callbacks, logout, base URL and console listing."""
        found = admin.find(f"/{REALM}/clients", "clientId", client.client_id)
        backchannel_url = found["attributes"].get("backchannel.logout.url")
        wrong = {
            key: found.get(key)
            for key, value in {
                "publicClient": False,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": client.agent_workload,
                "frontchannelLogout": False,
                "baseUrl": f"{client.origin}/",
                "alwaysDisplayInConsole": True,
                "optionalClientScopes": [],
            }.items()
            if found.get(key) != value
        } | {
            key: found.get(key)
            for key, value in {
                "redirectUris": set(client.callbacks),
                "defaultClientScopes": client.default_scopes,
            }.items()
            if set(found.get(key) or ()) != value
        }
        expect(
            not wrong and not backchannel_url,
            f"{client.client_id}'s representation differs: {wrong}, "
            f"back-channel url {backchannel_url}",
        )

    def the_observer_door(self, admin: Admin) -> None:
        """Proof 9 (#66): the observer's container, its console-change lines and its stops. Runs
        last but for proof 7, when every credential proof 7 looks for exists."""
        password = self.keep_secret("smtp password", secrets.token_urlsafe(16))
        smtp = {
            "host": "smtp.probe.invalid",
            "port": "587",
            "from": "probe@probe.invalid",
            "auth": "true",
            "user": "probe",
        }
        admin.must("PUT", f"/{REALM}", {"smtpServer": smtp | {"password": password}})
        digest_key = self.keep_secret("the observer's second digest key", new_digest_key())
        kept = f"{PROJECT}-observe-{secrets.token_hex(4)}"
        result = self.stack.observe(digest_key, keep_as=kept)
        expect(result.returncode == 0, f"the kept observe exited {result.returncode}")
        before = json.loads(result.stdout)
        door = self.observer_container(kept)

        original = self.change_config_in_console(admin)
        group_id = admin.create(f"/{REALM}/groups", {"name": ESCAPING_GROUP})
        lines = change_lines(before, self.observe(digest_key))
        admin.must("PUT", f"/master/authentication/config/{original['id']}", original)
        admin.must("DELETE", f"/{REALM}/groups/{group_id}")
        expected = [
            f"{REALM}: addition group /probe\\x1b[31mconsole\\nname (0 members)",
            f"master: change authenticator config {CHANGED_CONFIG} config.check_result: "
            '"not-executed" → "executed"',
        ]
        expect(lines == expected, f"the console changes are listed as {lines}")

        left = {
            f"{stop.name} {moment.__name__}": self.stack.observe_stopped_by(
                digest_key, stop, moment
            )
            for stop in (signal.SIGINT, signal.SIGTERM)
            for moment in (as_its_container_runs, as_the_inventory_arrives)
        }
        expect(not any(left.values()), f"a stopped observe left containers: {left}")

        self.stack.see_running()
        stopped = self.stack.compose("stop", "keycloak")
        expect(stopped.returncode == 0, f"compose stop keycloak failed: {tail(stopped.stderr)}")
        refused = self.stack.observe(digest_key)
        running = self.stack.compose("ps", "--status", "running", "--services").stdout.split()
        expect(
            refused.returncode != 0 and "does not answer" in refused.stderr,
            f"an observe without Keycloak exited {refused.returncode}: {tail(refused.stderr)}",
        )
        expect("keycloak" not in running, "an observe started Keycloak")
        self.held(
            "9",
            f"the observer runs as {door}; a console change of {CHANGED_CONFIG} is one change "
            "line and a group named with ESC and a newline one escaped line; SIGINT and SIGTERM "
            "as its container runs and as the inventory arrives leave no container, and an exit "
            "0 carries the whole inventory; with Keycloak stopped it says 'does not answer', exits "
            f"{refused.returncode} and starts nothing",
        )

    def observer_container(self, name: str) -> str:
        """Checks the kept observer container's door, records its Config.Env, removes it."""
        self.stack.see_env(name)
        inspected = docker("inspect", name)
        docker("rm", name)
        expect(inspected.returncode == 0, f"docker inspect {name} failed")
        container = json.loads(inspected.stdout)[0]
        host, config = container["HostConfig"], container["Config"]
        mounts = {mount["Destination"]: mount["RW"] for mount in container["Mounts"]}
        door = {
            "user": config["User"],
            "read-only": host["ReadonlyRootfs"],
            "cap_drop": host["CapDrop"],
            "no-new-privileges": "no-new-privileges:true" in (host["SecurityOpt"] or []),
            "ports": host["PortBindings"] or {},
            "networks": sorted(container["NetworkSettings"]["Networks"]),
            "mounts": mounts,
        }
        wanted = {
            "user": "65534:65534",
            "read-only": True,
            "cap_drop": ["ALL"],
            "no-new-privileges": True,
            "ports": {},
            "networks": [f"{PROJECT}_keycloak"],
            "mounts": {OBSERVER_SECRET: False, OBSERVER_SCRIPT: False},
        }
        expect(door == wanted, f"the observer container is {door}")
        return (
            "65534, read-only, cap_drop ALL, no-new-privileges, no port, the keycloak network "
            "alone, its secret a read-only file"
        )

    def change_config_in_console(self, admin: Admin) -> dict[str, Any]:
        """Changes one authenticator config as an operator would; returns it as it was."""
        steps = admin.get("/master/authentication/flows/browser-mfa/executions")
        config_id = next(
            step["authenticationConfig"] for step in steps if step.get("alias") == CHANGED_CONFIG
        )
        original = admin.get(f"/master/authentication/config/{config_id}")
        changed = {**original, "config": {**original["config"], "check_result": "executed"}}
        admin.must("PUT", f"/master/authentication/config/{config_id}", changed)
        return original

    def no_secret_leaked(self) -> None:
        self.stack.see_running()
        places = [(f"Config.Env of {name}", "\n".join(env)) for name, env in self.stack.seen_env]
        places += [
            ("the compose, apply and service log output", "\n".join(self.stack.outputs)),
            ("the probe's own output", "\n".join(self.evidence)),
        ]
        leaks = [
            f"{secret_name} in {place}"
            for place, text in places
            for secret_name, value in self.secret_values.items()
            if value in text
        ]
        expect(not leaks, f"credentials leaked: {leaks}")
        containers = sorted({name for name, _ in self.stack.seen_env})
        self.held(
            "7",
            f"none of the {len(self.secret_values)} credentials (secret files, passwords, the "
            "TOTP seed, the RP secret, access, refresh, ID and admin tokens, the admin console's "
            "PKCE verifiers, authorization codes and session cookies) is in the Config.Env of the "
            f"{len(containers)} containers seen ({', '.join(containers)}), in any "
            "compose, apply or service log output, or in the probe's own output",
        )


READY_MARK = "READY"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# Per mode: the Probe method that runs it and the line that reports it ended well.
MODES = {
    "run": ("run", "PROBE PASSED: proofs 1-11 hold"),
    "hold": ("hold", "HOLD ENDED"),
}


class StopHolding(Exception):
    """A held stack was told to stop; unlike ProbeFailure this is the normal end."""


def stop_as_failure(signum: int, _frame: object) -> None:
    ignore_stop_signals()
    raise ProbeFailure(f"stopped by {signal.Signals(signum).name}")


def stop_holding(_signum: int, _frame: object) -> None:
    ignore_stop_signals()
    raise StopHolding


def ignore_stop_signals() -> None:
    """Ends the stop signals' effect; blocked meanwhile, so none can raise between the two.

    The stop handlers call it before they raise and main calls it as the run ends, inside the
    try that catches their raise, so only the first stop signal acts and the teardown that
    follows cannot be interrupted by a second one."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        for stopping in STOP_SIGNALS:
            signal.signal(stopping, signal.SIG_IGN)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def keep_stack_until_told_to_stop(ready_line: str) -> None:
    """Prints the READY line, then blocks until SIGTERM, SIGINT or EOF on stdin."""
    # A signal that arrives while the handler is swapped waits until READY is out, so one
    # before READY is a failure and one after it is a stop, never the other way round.
    # Unblocking delivers a pending signal inside pthread_sigmask; main ends the hold on it.
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        for stopping in STOP_SIGNALS:
            signal.signal(stopping, stop_holding)
        print(ready_line, flush=True)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
    sys.stdin.read()


def main(make_stack: Callable[[Path], Stack] = Stack) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        choices=tuple(MODES),
        default="run",
        help="run: the proofs (default); hold: keep the stack up for another repository's proof",
    )
    parser.add_argument("--scratch", type=Path, required=True, help="an empty or new directory")
    arguments = parser.parse_args()
    scratch = arguments.scratch.resolve()
    if scratch.exists() and (not scratch.is_dir() or any(scratch.iterdir())):
        parser.error(f"{scratch} is not an empty directory")
    # `timeout` sends SIGTERM and Ctrl-C sends SIGINT; turn both into a failure so even a stop
    # while the stack is being built ends with the one FAILED line, and the teardown below still
    # runs for a stack that exists.
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, stop_as_failure)
    failures = []
    stack = None
    method_name, success_line = MODES[arguments.mode]
    try:
        try:
            try:
                stack = make_stack(scratch)
            except OSError as failure:
                raise ProbeFailure(str(failure)) from failure
            getattr(Probe(stack), method_name)()
        finally:
            # Inside the try whose handlers take a stop signal: one landing while this swaps
            # the handlers still ends the run as a stop or a failure, never as a skipped teardown.
            ignore_stop_signals()
    except StopHolding:
        pass  # any stop signal after READY, however late, is the normal end of a hold
    except ProbeFailure as failure:
        failures.append(str(failure))
        # A stop signal landing in the inner finally replaced the error it interrupted; name both.
        # The same replacement turns a non-ProbeFailure raised after READY into a StopHolding,
        # which ends the hold quietly; that error is not reported.
        if isinstance(failure.__context__, ProbeFailure):
            failures.append(str(failure.__context__))
    finally:
        # The teardown must finish and say so; the SIGKILL of `timeout --kill-after` bounds it.
        if stack is not None and not stack.down():
            failures.append(f"stack {PROJECT} is left behind; README says how to remove it")
    if failures:
        print(f"PROBE FAILED: {'; '.join(failures)}", flush=True)
        return 1
    print(success_line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
