"""The realm observer (#66) against a fake Keycloak 26.8.1 admin API; each test names its cell of
the item's scenario matrix (U8-U29) or its rule."""

import copy
import io
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

from edge.realm import change_lines, new_digest_key
from edge.realm import observe as observer

REPOSITORY = Path(__file__).resolve().parents[1]
SECRET = "the-config-cli-secret"
TOKEN = "an-access-token-from-the-fake"
REFUSAL_BODY = "invalid_client: the reply body the observer must not print"
KEY = new_digest_key()
# Values the observer reads but must never let out (rule 4).
PLANTED = {
    "client secret": "planted-client-secret",
    "registration access token": "planted-registration-token",
    "rotated secret": "planted-rotated-secret",
    "saml private key": "planted-saml-private-key",
    "idp client secret": "planted-idp-client-secret",
    "smtp password": "planted-smtp-password",
    "component config": "planted-component-key",
    "unlisted client attribute": "planted-unlisted-attribute-value",
}


def uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(parts)))


# --- Representations as Keycloak 26.8.1 answers them ---------------------------------------


def realm_rep(name: str) -> dict[str, Any]:
    return {
        "id": uid(name),
        "realm": name,
        "enabled": True,
        "sslRequired": "external",
        "accessTokenLifespan": 300 if name == "hallucinai" else 60,
        "ssoSessionIdleTimeout": 604800,
        "ssoSessionMaxLifespan": 2592000,
        "bruteForceProtected": True,
        "failureFactor": 30,
        "eventsEnabled": True,
        "eventsListeners": ["jboss-logging"],
        "enabledEventTypes": [],
        "adminEventsEnabled": True,
        "browserFlow": "browser-mfa" if name == "master" else "browser",
        "directGrantFlow": "direct-grant-mfa" if name == "master" else "direct grant",
        "webAuthnPolicyRpId": "localhost" if name == "master" else "hallucinai.de",
        "notBefore": 0,
        "defaultRole": {"id": uid(name, "default-roles"), "name": f"default-roles-{name}"},
        "smtpServer": {"host": "smtp.invalid", "port": "587", "password": PLANTED["smtp password"]},
        "browserSecurityHeaders": {"xFrameOptions": "SAMEORIGIN"},
        "attributes": {
            "frontendUrl": "http://localhost:8090" if name == "master" else "",
            "adminEventsExpiration": "31536000",
            "de.adorsys.keycloak.config.import-checksum-default": "a-checksum",
            "cibaInterval": "5",
        },
    }


def mapper_rep(owner: str, name: str, mapper: str, **config: str) -> dict[str, Any]:
    return {
        "id": uid(owner, name),
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": mapper,
        "consentRequired": False,
        "config": config,
    }


def client_rep(realm: str, client_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": uid(realm, client_id),
        "clientId": client_id,
        "name": client_id,
        "surrogateAuthRequired": False,
        "enabled": True,
        "alwaysDisplayInConsole": False,
        "clientAuthenticatorType": "client-secret",
        "secret": PLANTED["client secret"],
        "registrationAccessToken": PLANTED["registration access token"],
        "redirectUris": ["https://b.invalid/cb", "https://a.invalid/cb"],
        "webOrigins": [],
        "notBefore": 0,
        "bearerOnly": False,
        "consentRequired": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "publicClient": False,
        "frontchannelLogout": False,
        "protocol": "openid-connect",
        "attributes": {
            "post.logout.redirect.uris": "+",
            "client.secret.creation.time": "1760000000",
            "client.secret.rotated": PLANTED["rotated secret"],
            "saml.signing.private.key": PLANTED["saml private key"],
            "probe.unlisted": PLANTED["unlisted client attribute"],
        },
        "authenticationFlowBindingOverrides": {},
        "fullScopeAllowed": True,
        "nodeReRegistrationTimeout": -1,
        "protocolMappers": [],
        "defaultClientScopes": ["web-origins", "acr", "profile", "roles", "basic", "email"],
        "optionalClientScopes": ["address", "phone", "offline_access", "microprofile-jwt"],
        "access": {"view": True, "configure": True, "manage": True},
        **changes,
    }


def role_rep(realm: str, container: str, name: str, *, client: bool = False) -> dict[str, Any]:
    return {
        "id": uid(realm, container, name),
        "name": name,
        "description": f"${{role_{name}}}",
        "composite": False,
        "clientRole": client,
        "containerId": uid(realm, container) if client else uid(realm),
        "attributes": {},
    }


def step_rep(
    realm: str, flow: str, level: int, index: int, *, authenticator: str = "", sub_flow: str = ""
) -> dict[str, Any]:
    step = {
        "id": uid(realm, flow, str(level), str(index), authenticator or sub_flow),
        "requirement": "REQUIRED",
        "displayName": sub_flow or authenticator.replace("-", " ").title(),
        "requirementChoices": ["REQUIRED", "ALTERNATIVE", "DISABLED"],
        "configurable": False,
        "level": level,
        "index": index,
        "priority": (index + 1) * 10,
    }
    if sub_flow:
        step |= {
            "authenticationFlow": True,
            "description": f"the {sub_flow} sub-flow",
            "providerId": "basic-flow",
            "flowId": uid(realm, sub_flow),
        }
    else:
        step |= {"authenticationFlow": False, "providerId": authenticator}
    return step


def user_rep(realm: str, username: str, client_id: str) -> dict[str, Any]:
    return {
        "id": uid(realm, username),
        "username": username,
        "emailVerified": False,
        "createdTimestamp": 1760000000000,
        "enabled": True,
        "totp": False,
        "serviceAccountClientId": client_id,
        "disableableCredentialTypes": [],
        "requiredActions": [],
        "notBefore": 0,
        "access": {"manage": True},
    }


@dataclass
class Group:
    members: list[str] = field(default_factory=list)
    realm_roles: list[str] = field(default_factory=list)


@dataclass
class FakeRealm:
    """One realm's state, from which the fake answers every admin endpoint."""

    name: str
    realm: dict[str, Any]
    groups: dict[str, Group] = field(default_factory=dict)
    roles: list[dict[str, Any]] = field(default_factory=list)
    composites: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    clients: list[dict[str, Any]] = field(default_factory=list)
    client_roles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    service_accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    role_mappings: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_groups: dict[str, list[str]] = field(default_factory=dict)
    client_scopes: list[dict[str, Any]] = field(default_factory=list)
    default_scopes: list[str] = field(default_factory=list)
    optional_scopes: list[str] = field(default_factory=list)
    flows: list[dict[str, Any]] = field(default_factory=list)
    executions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    required_actions: list[dict[str, Any]] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    provider_mappers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    components: list[dict[str, Any]] = field(default_factory=list)

    def group_rep(self, path: str) -> dict[str, Any]:
        group = self.groups[path]
        children = self.children_of(path)
        return {
            "id": uid(self.name, "group", path),
            "name": path.rsplit("/", 1)[1],
            "path": path,
            "parentId": uid(self.name, "group", path.rsplit("/", 1)[0]),
            "subGroupCount": len(children),
            "subGroups": [],
            "attributes": {},
            "realmRoles": group.realm_roles,
            "clientRoles": {},
            "access": {"view": True, "manage": True},
        }

    def children_of(self, parent: str) -> list[str]:
        return [path for path in self.groups if path.rsplit("/", 1)[0] == parent]


def hallucinai() -> FakeRealm:
    realm = FakeRealm("hallucinai", realm_rep("hallucinai"))
    for path in ("/apps", "/apps/songmaker", "/apps/presentator", "/apps/hopin", "/ki-abo"):
        realm.groups[path] = Group()
    realm.groups["/apps/hopin"].members = [uid("hallucinai", "anna"), uid("hallucinai", "ben")]
    realm.roles = [role_rep("hallucinai", "", name) for name in ("offline_access", "uma")]
    agent = client_rep("hallucinai", "agent-service", bearerOnly=True)
    agent["protocolMappers"] = [
        mapper_rep(
            "agent-service", "audience", "oidc-audience-mapper", **{"access.token.claim": "true"}
        )
    ]
    realm.clients = [agent, client_rep("hallucinai", "account", publicClient=True)]
    realm.client_roles = {
        agent["id"]: [role_rep("hallucinai", "agent-service", "use", client=True)]
    }
    groups_scope = {
        "id": uid("hallucinai", "scope", "groups"),
        "name": "groups",
        "description": "The user's groups as full paths",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        "protocolMappers": [
            mapper_rep(
                "groups",
                "groups",
                "oidc-group-membership-mapper",
                **{"claim.name": "groups", "full.path": "true"},
            )
        ],
    }
    realm.client_scopes = [groups_scope]
    realm.default_scopes = ["groups"]
    realm.flows = [
        {
            "id": uid("hallucinai", "browser"),
            "alias": "browser",
            "description": "Browser based authentication",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": True,
            "authenticationExecutions": [],
        }
    ]
    realm.executions = {
        "browser": [step_rep("hallucinai", "browser", 0, 0, authenticator="auth-cookie")]
    }
    realm.required_actions = [
        {
            "alias": "CONFIGURE_TOTP",
            "name": "Configure OTP",
            "providerId": "CONFIGURE_TOTP",
            "enabled": True,
            "defaultAction": False,
            "priority": 10,
            "config": {},
        }
    ]
    realm.providers = [
        {
            "alias": "console-idp",
            "internalId": uid("hallucinai", "idp"),
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": False,
            "config": {"clientId": "edge", "clientSecret": PLANTED["idp client secret"]},
        }
    ]
    realm.provider_mappers = {
        "console-idp": [
            {
                "id": uid("hallucinai", "idp-mapper"),
                "name": "email",
                "identityProviderAlias": "console-idp",
                "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                "config": {"claim": "email", "syncMode": "INHERIT"},
            }
        ]
    }
    realm.components = [
        component("hallucinai", "rsa-generated", "org.keycloak.keys.KeyProvider", "rsa-generated"),
        component(
            "hallucinai",
            "Allowed Client Scopes",
            "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
            "allowed-client-templates",
            sub_type="anonymous",
        ),
        component(
            "hallucinai",
            "Allowed Client Scopes",
            "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy",
            "allowed-client-templates",
            sub_type="authenticated",
        ),
    ]
    return realm


def component(
    realm: str, name: str, provider_type: str, provider_id: str, *, sub_type: str = ""
) -> dict[str, Any]:
    rep = {
        "id": uid(realm, provider_type, name, sub_type),
        "name": name,
        "providerId": provider_id,
        "providerType": provider_type,
        "parentId": uid(realm),
        "config": {"privateKey": [PLANTED["component config"]]},
    }
    return rep | ({"subType": sub_type} if sub_type else {})


def master() -> FakeRealm:
    realm = FakeRealm("master", realm_rep("master"))
    admin = role_rep("master", "", "admin")
    admin["composite"] = True
    realm.roles = [admin, role_rep("master", "", "create-realm")]
    config_cli = client_rep("master", "config-cli", serviceAccountsEnabled=True)
    realm_management = client_rep("master", "hallucinai-realm")
    realm.clients = [config_cli, realm_management]
    manage = role_rep("master", "hallucinai-realm", "manage-realm", client=True)
    realm.client_roles = {realm_management["id"]: [manage]}
    realm.composites = {admin["id"]: [role_rep("master", "", "create-realm"), manage]}
    account = user_rep("master", "service-account-config-cli", "config-cli")
    realm.service_accounts = {config_cli["id"]: account}
    realm.role_mappings = {
        account["id"]: {
            "realmMappings": [
                role_rep("master", "", "admin"),
                role_rep("master", "", "default-roles-master"),
            ]
        }
    }
    realm.flows = [
        {
            "id": uid("master", "browser-mfa"),
            "alias": "browser-mfa",
            "description": "Password plus a required second factor",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
            "authenticationExecutions": [],
        }
    ]
    without_passkey = {
        "id": uid("master", "config", "browser-mfa-without-passkey"),
        "alias": "browser-mfa-without-passkey",
        "config": {"flow_to_check": "browser-mfa-passkey", "check_result": "not-executed"},
    }
    realm.configs = {without_passkey["id"]: without_passkey}
    conditional = step_rep(
        "master", "browser-mfa", 2, 0, authenticator="conditional-sub-flow-executed"
    )
    conditional |= {
        "authenticationConfig": without_passkey["id"],
        "alias": without_passkey["alias"],
    }
    realm.executions = {
        "browser-mfa": [
            step_rep("master", "browser-mfa", 0, 0, authenticator="auth-cookie"),
            step_rep("master", "browser-mfa", 0, 1, sub_flow="browser-mfa-forms"),
            step_rep("master", "browser-mfa", 1, 0, authenticator="auth-username-password-form"),
            step_rep("master", "browser-mfa", 1, 1, sub_flow="browser-mfa-totp"),
            conditional,
            step_rep("master", "browser-mfa", 2, 1, authenticator="auth-otp-form"),
        ]
    }
    return realm


def keycloak_realms() -> dict[str, FakeRealm]:
    return {"master": master(), "hallucinai": hallucinai()}


# --- The fake admin API ---------------------------------------------------------------------


@dataclass
class Faults:
    token_status: int = 200
    status: dict[str, int] = field(default_factory=dict)  # path -> status, every time
    delay: dict[str, float] = field(default_factory=dict)  # path -> seconds
    unauthorized_after: int | None = None  # 401 for the GET after this many GETs
    unauthorized_times: int = 1
    truncate_children: bool = False
    redirect: dict[str, str] = field(default_factory=dict)  # path -> Location
    not_json: set[str] = field(default_factory=set)  # paths answered 200 with a non-JSON body


class FakeKeycloak(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), AdminApi)
        self.realms = keycloak_realms()
        self.faults = Faults()
        self.requests: list[tuple[str, str]] = []
        self.gets = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


def paged(items: list[Any], query: dict[str, list[str]]) -> list[Any]:
    first = int(query.get("first", ["0"])[0])
    count = int(query.get("max", ["100"])[0])
    return items[first : first + count]


class AdminApi(BaseHTTPRequestHandler):
    server: FakeKeycloak

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(("POST", self.path))
        if self.server.faults.token_status != 200:
            self.reply({"error": REFUSAL_BODY}, self.server.faults.token_status)
        else:
            self.reply({"access_token": TOKEN, "expires_in": 60})

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path, query = unquote(split.path), parse_qs(split.query)
        self.server.requests.append(("GET", path))
        faults = self.server.faults
        self.server.gets += 1
        expired = (
            faults.unauthorized_after is not None and self.server.gets > faults.unauthorized_after
        )
        if expired and faults.unauthorized_times:
            faults.unauthorized_times -= 1
            return self.reply({"error": "token expired"}, 401)
        if self.headers["Authorization"] != f"Bearer {TOKEN}":
            return self.reply({"error": "no token"}, 401)
        time.sleep(faults.delay.get(path, 0))
        if path in faults.not_json:
            return self.reply_raw(200, b"<html>not JSON</html>")
        if path in faults.redirect:
            self.send_response(302)
            self.send_header("Location", faults.redirect[path])
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if path in faults.status:
            return self.reply({"error": "server error"}, faults.status[path])
        _, _, _, realm_name, *rest = path.split("/")
        answer = self.answer(self.server.realms[realm_name], "/" + "/".join(rest), query)
        if answer is None:
            return self.reply({"error": "not found"}, 404)
        self.reply(answer)

    def refuse_a_write(self) -> None:
        """Records any other method, so the read-only test sees it, and answers 405."""
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append((self.command, unquote(urlsplit(self.path).path)))
        self.reply({"error": "method not allowed"}, 405)

    do_PUT = do_DELETE = do_PATCH = refuse_a_write

    def answer(self, realm: FakeRealm, path: str, query: dict[str, list[str]]) -> Any:
        groups_by_id = {uid(realm.name, "group", path): path for path in realm.groups}
        parts = path.strip("/").split("/")
        match parts:
            case [""]:
                return realm.realm
            case ["groups", "count"]:
                return {"count": len(realm.children_of(""))}
            case ["groups"]:
                return paged([realm.group_rep(p) for p in realm.children_of("")], query)
            case ["groups", group_id, "children"]:
                children = [realm.group_rep(p) for p in realm.children_of(groups_by_id[group_id])]
                if self.server.faults.truncate_children:
                    children = children[:-1]
                return paged(children, query)
            case ["groups", group_id, "members"]:
                members = realm.groups[groups_by_id[group_id]].members
                return paged([{"id": member, "username": member} for member in members], query)
            case ["roles"]:
                return paged(realm.roles, query)
            case ["roles-by-id", role_id, "composites"]:
                return realm.composites.get(role_id, [])
            case ["clients"]:
                return paged(realm.clients, query)
            case ["clients", client_uuid, "roles"]:
                return paged(realm.client_roles.get(client_uuid, []), query)
            case ["clients", client_uuid, "service-account-user"]:
                return realm.service_accounts[client_uuid]
            case ["users", user_id, "role-mappings"]:
                return realm.role_mappings.get(user_id, {})
            case ["users", user_id, "groups"]:
                return paged(
                    [{"id": p, "path": p} for p in realm.user_groups.get(user_id, [])], query
                )
            case ["client-scopes"]:
                return realm.client_scopes
            case ["default-default-client-scopes"]:
                return [{"id": uid(name), "name": name} for name in realm.default_scopes]
            case ["default-optional-client-scopes"]:
                return [{"id": uid(name), "name": name} for name in realm.optional_scopes]
            case ["authentication", "flows"]:
                return realm.flows
            case ["authentication", "flows", alias, "executions"]:
                return realm.executions[alias]
            case ["authentication", "config", config_id]:
                return realm.configs[config_id]
            case ["authentication", "required-actions"]:
                return realm.required_actions
            case ["identity-provider", "instances"]:
                return paged(realm.providers, query)
            case ["identity-provider", "instances", alias, "mappers"]:
                return realm.provider_mappers.get(alias, [])
            case ["components"]:
                return realm.components
        return None

    def reply(self, body: Any, status: int = 200) -> None:
        self.reply_raw(status, json.dumps(body).encode())

    def reply_raw(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def keycloak() -> Iterator[FakeKeycloak]:
    server = FakeKeycloak()
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def observe(keycloak: FakeKeycloak) -> observer.Inventory:
    session = observer.Session(keycloak.url, SECRET)
    return observer.observe(session, observer.Digester(bytes.fromhex(KEY)))


def lines_after(
    keycloak: FakeKeycloak, change: Callable[[dict[str, FakeRealm]], None]
) -> list[str]:
    before = observe(keycloak)
    change(keycloak.realms)
    return change_lines(before, observe(keycloak))


@dataclass(frozen=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str


@pytest.fixture
def run_observer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Iterator[Callable[[str], Run]]:
    """The observer's command line as the compose service runs it, the key on stdin."""
    secret_file = tmp_path / "config-cli-secret"
    secret_file.write_text(SECRET + "\n")
    previous = signal.getsignal(signal.SIGTERM)

    def run(base_url: str) -> Run:
        stdin = io.StringIO(KEY + "\n")
        code = observer.main(["--base-url", base_url], stdin, secret_file)
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    yield run
    signal.signal(signal.SIGTERM, previous)


# --- U8-U12, U23, U25: what the lines say ------------------------------------------------------


def add_client(realms: dict[str, FakeRealm]) -> None:
    realms["hallucinai"].clients.append(client_rep("hallucinai", "probe-rp"))


def longer_tokens(realms: dict[str, FakeRealm]) -> None:
    realms["hallucinai"].realm["accessTokenLifespan"] = 360


def rename_hopin(realms: dict[str, FakeRealm]) -> None:
    groups = realms["hallucinai"].groups
    groups["/apps/hop-in"] = groups.pop("/apps/hopin")


def swap_forms_steps(realms: dict[str, FakeRealm]) -> None:
    steps = realms["master"].executions["browser-mfa"]
    steps[2]["index"], steps[3]["index"] = 1, 0


def master_only(realms: dict[str, FakeRealm]) -> None:
    realms["master"].realm["accessTokenLifespan"] = 120
    realms["master"].roles.append(role_rep("master", "", "probe-console-role"))


def new_uuids_and_access(realms: dict[str, FakeRealm]) -> None:
    realm = realms["hallucinai"]
    for client in realm.clients:
        old_id, client["id"] = client["id"], str(uuid.uuid4())
        client["access"] = {"view": False}
        if old_id in realm.client_roles:
            realm.client_roles[client["id"]] = realm.client_roles.pop(old_id)
    realms["hallucinai"].realm["id"] = str(uuid.uuid4())
    realms["hallucinai"].realm["defaultRole"]["id"] = str(uuid.uuid4())
    realms["hallucinai"].realm["attributes"]["de.adorsys.keycloak.config.state-default"] = "x"


def change_unlisted_and_secrets(realms: dict[str, FakeRealm]) -> None:
    agent = realms["hallucinai"].clients[0]
    agent["attributes"]["probe.unlisted"] = "another-hidden-value"
    agent["secret"] = "another-client-secret"
    agent["attributes"]["client.secret.rotated"] = "another-rotated-secret"
    realms["hallucinai"].providers[0]["config"]["clientSecret"] = "another-idp-secret"
    realms["hallucinai"].realm["smtpServer"]["password"] = "another-smtp-password"
    realms["hallucinai"].components[0]["config"] = {"privateKey": ["another-key"]}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(add_client, ["hallucinai: addition client probe-rp"], id="U8-client-added"),
        pytest.param(
            longer_tokens,
            ["hallucinai: change realm hallucinai accessTokenLifespan: 300 → 360"],
            id="U9-setting-changed-old-to-new",
        ),
        pytest.param(
            rename_hopin,
            [
                "hallucinai: addition group /apps/hop-in (2 members)",
                "hallucinai: removal group /apps/hopin (2 members)",
            ],
            id="U10-group-renamed-is-removal-plus-addition-with-members",
        ),
        pytest.param(
            swap_forms_steps,
            [
                "master: change flow step browser-mfa-forms/auth-username-password-form#1 "
                "place: 0 → 1",
                "master: change flow step browser-mfa-forms/browser-mfa-totp#1 place: 1 → 0",
            ],
            id="U11-reordered-step-is-a-change",
        ),
        pytest.param(
            master_only,
            [
                "master: change realm master accessTokenLifespan: 60 → 120",
                "master: addition realm role probe-console-role",
            ],
            id="U12-each-realm-named-on-its-own",
        ),
        pytest.param(new_uuids_and_access, [], id="U23-identity-not-uuid-volatile-excluded"),
        pytest.param(
            change_unlisted_and_secrets,
            ["hallucinai: change client agent-service attributes.probe.unlisted: changed"],
            id="U25-unlisted-key-changed-without-value-secret-silent",
        ),
    ],
)
def test_the_lines_name_each_addition_change_and_removal_by_identity(
    keycloak: FakeKeycloak, change: Callable[[dict[str, FakeRealm]], None], expected: list[str]
) -> None:
    assert lines_after(keycloak, change) == expected


def test_two_equal_steps_in_one_flow_are_told_apart_by_their_ordinal(
    keycloak: FakeKeycloak,
) -> None:
    """U11: a second auth-otp-form in the same sub-flow is its own step, #2."""

    def second_otp_step(realms: dict[str, FakeRealm]) -> None:
        extra = step_rep("master", "browser-mfa", 2, 2, authenticator="auth-otp-form")
        realms["master"].executions["browser-mfa"].append(extra)

    assert lines_after(keycloak, second_otp_step) == [
        "master: addition flow step browser-mfa-totp/auth-otp-form#2"
    ]


def test_a_removed_step_in_a_declared_sub_flow_is_one_removal(keycloak: FakeKeycloak) -> None:
    """U1 measurement, P3 in miniature: an extra step config-cli removes shows as a removal."""

    def remove_otp_step(realms: dict[str, FakeRealm]) -> None:
        realms["master"].executions["browser-mfa"].pop()

    assert lines_after(keycloak, remove_otp_step) == [
        "master: removal flow step browser-mfa-totp/auth-otp-form#1"
    ]


def test_service_account_roles_and_mappers_are_removed_one_line_each(
    keycloak: FakeKeycloak,
) -> None:
    """Rule 3 and the U1 three: a lost realm role on the service account and a lost scope mapper."""

    def lose_role_and_mapper(realms: dict[str, FakeRealm]) -> None:
        account = next(iter(realms["master"].role_mappings.values()))
        account["realmMappings"] = account["realmMappings"][1:]
        realms["hallucinai"].client_scopes[0]["protocolMappers"] = []

    assert lines_after(keycloak, lose_role_and_mapper) == [
        "hallucinai: removal protocol mapper scope groups/groups",
        "master: removal service-account realm role service-account-config-cli/admin",
    ]


def test_a_name_holding_esc_and_a_newline_prints_as_one_escaped_line(
    keycloak: FakeKeycloak,
) -> None:
    """Rule 7: every name passes through compose.printable."""

    def console_group(realms: dict[str, FakeRealm]) -> None:
        realms["hallucinai"].groups["/probe\x1b[31mred\nline"] = Group()

    assert lines_after(keycloak, console_group) == [
        "hallucinai: addition group /probe\\x1b[31mred\\nline (0 members)"
    ]


def test_the_inventory_holds_no_secret_value_and_no_unlisted_value(
    keycloak: FakeKeycloak, run_observer: Callable[[str], Run]
) -> None:
    """Rule 4 and U25: named secrets never leave, unlisted values leave only as digests."""
    run = run_observer(keycloak.url)

    assert run.exit_code == 0
    leaked = [name for name, value in PLANTED.items() if value in run.stdout + run.stderr]
    assert leaked == []
    assert TOKEN not in run.stdout + run.stderr
    assert SECRET not in run.stdout + run.stderr
    client = json.loads(run.stdout)["hallucinai"]["client"]["agent-service"]
    assert "attributes.probe.unlisted" in client["digests"]
    assert "secret" not in client["digests"]
    assert "secret" not in client["fields"]


def test_a_component_identity_includes_its_sub_type(keycloak: FakeKeycloak) -> None:
    """Rule 5: Keycloak's two `Allowed Client Scopes` policies differ only by their sub-type."""
    components = observe(keycloak)["hallucinai"]["component"]

    assert sorted(components) == [
        "org.keycloak.keys.KeyProvider/rsa-generated/rsa-generated",
        "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy/"
        "allowed-client-templates/anonymous/Allowed Client Scopes",
        "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy/"
        "allowed-client-templates/authenticated/Allowed Client Scopes",
    ]


def test_a_duplicate_component_identity_fails_loudly(keycloak: FakeKeycloak) -> None:
    """Rule 5: a duplicate fails loudly."""
    realm = keycloak.realms["hallucinai"]
    realm.components.append(copy.deepcopy(realm.components[0]))

    with pytest.raises(observer.ObserveError, match="two component objects in hallucinai"):
        observe(keycloak)


# --- U13, U14: paging and counts -------------------------------------------------------------


def test_more_than_a_page_of_everything_is_read_in_full(keycloak: FakeKeycloak) -> None:
    """U13: >100 subgroups, members, clients and roles; every page is read."""
    realm = keycloak.realms["hallucinai"]
    for number in range(150):
        realm.groups[f"/apps/app-{number:03}"] = Group()
    realm.groups["/apps/songmaker"].members = [uid("member", str(n)) for n in range(120)]
    realm.clients += [client_rep("hallucinai", f"client-{n:03}") for n in range(130)]
    realm.roles += [role_rep("hallucinai", "", f"role-{n:03}") for n in range(110)]

    inventory = observe(keycloak)["hallucinai"]

    assert len([path for path in inventory["group"] if path.startswith("/apps/app-")]) == 150
    assert inventory["group"]["/apps/songmaker"]["fields"]["members"] == 120
    assert len(inventory["client"]) == 132
    assert len(inventory["realm role"]) == 112


def test_children_fewer_than_the_sub_group_count_fail_loudly(keycloak: FakeKeycloak) -> None:
    """U14: the count check measures that nothing was truncated."""
    keycloak.faults.truncate_children = True

    with pytest.raises(observer.ObserveError, match=r"children returned \d+ of \d+"):
        observe(keycloak)


# --- U19, U20, U22, U27: the door ------------------------------------------------------------


def test_a_wrong_secret_is_one_sign_in_refused_line_without_body_or_token(
    keycloak: FakeKeycloak, run_observer: Callable[[str], Run]
) -> None:
    """U19."""
    keycloak.faults.token_status = 401

    run = run_observer(keycloak.url)

    assert (run.exit_code, run.stdout) == (1, "")
    assert run.stderr.splitlines() == [
        "realm-observer: sign-in refused (status 401 at "
        "/realms/master/protocol/openid-connect/token)"
    ]
    assert REFUSAL_BODY not in run.stderr
    assert SECRET not in run.stderr


def test_a_5xx_mid_read_is_one_line_and_no_inventory(
    keycloak: FakeKeycloak, run_observer: Callable[[str], Run]
) -> None:
    """U20."""
    keycloak.faults.status = {"/admin/realms/hallucinai/client-scopes": 503}

    run = run_observer(keycloak.url)

    assert (run.exit_code, run.stdout) == (1, "")
    assert run.stderr.splitlines() == [
        "realm-observer: status 503 at /admin/realms/hallucinai/client-scopes"
    ]


def test_a_timeout_mid_read_fails_the_observe_naming_the_path(keycloak: FakeKeycloak) -> None:
    """U20: the 5xx case above proves a failed observe prints one line and no inventory."""
    keycloak.faults.delay = {"/admin/realms/master/components": 1.0}
    session = observer.Session(keycloak.url, SECRET, timeout=0.3)
    digest = observer.Digester(bytes.fromhex(KEY))

    with pytest.raises(
        observer.ObserveError,
        match=r"^Keycloak does not answer /admin/realms/master/components \(timed out\)$",
    ):
        observer.observe(session, digest)


def test_keycloak_that_does_not_answer_is_one_line(run_observer: Callable[[str], Run]) -> None:
    """P18 in miniature: no Keycloak behind the URL."""
    run = run_observer("http://127.0.0.1:9")

    assert (run.exit_code, run.stdout) == (1, "")
    assert run.stderr.splitlines() == [
        "realm-observer: Keycloak does not answer "
        "/realms/master/protocol/openid-connect/token (unreachable)"
    ]


def test_the_observer_sends_get_only_plus_one_token_post(keycloak: FakeKeycloak) -> None:
    """Rule 1 and U22: read-only is a protocol contract."""
    observe(keycloak)

    not_admin_reads = [
        (method, path)
        for method, path in keycloak.requests
        if not (method == "GET" and path.startswith("/admin/realms/"))
    ]
    assert not_admin_reads == [("POST", "/realms/master/protocol/openid-connect/token")]


@pytest.mark.parametrize(
    "location",
    ["{url}/realms/elsewhere", "http://localhost:{port}/admin/realms/hallucinai"],
    ids=["outside-the-admin-path", "another-origin"],
)
def test_a_redirect_is_refused_as_its_status_and_never_followed(
    keycloak: FakeKeycloak, location: str
) -> None:
    """Rule 1: GET only under /admin/realms/ of the base URL; the token never follows a 30x."""
    redirecting = "/admin/realms/hallucinai/client-scopes"
    keycloak.faults.redirect = {
        redirecting: location.format(url=keycloak.url, port=keycloak.server_port)
    }

    with pytest.raises(observer.ObserveError, match=f"^status 302 at {redirecting}$"):
        observe(keycloak)
    assert keycloak.requests[-1] == ("GET", redirecting)


def test_two_observes_of_an_unchanged_keycloak_are_equal(keycloak: FakeKeycloak) -> None:
    """U22: nothing volatile reaches the inventory."""
    first = observe(keycloak)

    assert observe(keycloak) == first


def test_an_expired_token_mid_read_signs_in_once_more_and_completes(
    keycloak: FakeKeycloak,
) -> None:
    """U27."""
    expected = observe(keycloak)
    keycloak.requests.clear()
    keycloak.gets = 0
    keycloak.faults.unauthorized_after = 5

    assert observe(keycloak) == expected
    assert [method for method, _ in keycloak.requests].count("POST") == 2


def test_a_401_right_after_signing_in_again_fails_loudly(keycloak: FakeKeycloak) -> None:
    """U27: a second 401 is a loud failure."""
    keycloak.faults.unauthorized_after = 5
    keycloak.faults.unauthorized_times = 2

    with pytest.raises(observer.ObserveError, match=r"^status 401 at /admin/realms/master"):
        observe(keycloak)


# --- U21: required and optional fields ------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "message"),
    [
        ("enabled", r"^a client representation lacks enabled$"),
        ("clientId", r"^a reply lacks the key 'clientId'$"),
    ],
)
def test_a_missing_required_field_fails_loudly(
    keycloak: FakeKeycloak, field_name: str, message: str
) -> None:
    """U21, also for the identity, which is read before the representation is projected."""
    del keycloak.realms["hallucinai"].clients[0][field_name]

    with pytest.raises(observer.ObserveError, match=message):
        observe(keycloak)


def test_a_reply_that_is_not_json_fails_naming_its_path(keycloak: FakeKeycloak) -> None:
    """Rule 4: one line with the path, never the body."""
    keycloak.faults.not_json = {"/admin/realms/master/components"}

    with pytest.raises(
        observer.ObserveError, match=r"^no JSON at /admin/realms/master/components$"
    ):
        observe(keycloak)


def test_a_missing_optional_field_is_null(keycloak: FakeKeycloak) -> None:
    """U21: Keycloak leaves out empty fields."""
    del keycloak.realms["hallucinai"].clients[0]["name"]

    client = observe(keycloak)["hallucinai"]["client"]["agent-service"]

    assert client["fields"]["name"] is None


# --- U28, U29: coverage and the import ------------------------------------------------------


@pytest.mark.parametrize("config_file", ["master.yaml", "realm.yaml"])
def test_every_top_level_key_of_the_realm_files_is_read_or_a_named_gap(config_file: str) -> None:
    """U28: a key the coverage table does not name fails here."""
    declared = yaml.safe_load((REPOSITORY / "keycloak" / config_file).read_text())

    assert sorted(set(declared) - observer.COVERAGE.keys()) == []


def test_every_config_cli_import_type_maps_onto_the_coverage_table() -> None:
    """Rule 3: each config-cli 6.5.1 import type is a read or a named gap."""
    assert sorted(set(observer.IMPORT_TYPES.values()) - observer.COVERAGE.keys()) == []


def test_observe_py_imports_alone_with_python_isolated(tmp_path: Path) -> None:
    """Rule 2 and U29: standard library only, nothing of src on the path."""
    alone = tmp_path / "observe.py"
    shutil.copy(REPOSITORY / "src/edge/realm/observe.py", alone)

    result = subprocess.run(
        [sys.executable, "-I", str(alone), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert sys.version_info >= (3, 12)
