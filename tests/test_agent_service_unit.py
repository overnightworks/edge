"""The agent service unit as files, no containers (#201 cell U13): the `agent-service` service of
compose.yaml, `agent-service/service.toml`, and what they must agree on with the image recipe and
the realm.

The running paths (U1-U12) are proven by probe/agent-service/unit.py and egress.py --target
service; these tests hold the static facts the probe does not see in CI: the hardening, the binds
and tmpfs mounts, `pull_policy: never`, the image tag, and that every path in `service.toml` is a
mount target of the service.
"""

import re
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import pytest
import yaml

from edge.realm import observe
from edge.realm.declared import declared_realm

ROOT = Path(__file__).resolve().parents[1]

SERVICE = "agent-service"
OPERATOR_UID_GID = "1000:1000"
LOGIN_SOURCE = "${HOME}/.secrets/agent-service/claude"
OWNER_ONLY_TMPFS = ("uid=1000", "gid=1000", "mode=0700", "nosuid", "nodev", "noexec")
SIZE_BOUND = re.compile(r"size=\d+[kmg]")


@pytest.fixture(scope="module")
def service() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())["services"][SERVICE]


@pytest.fixture(scope="module")
def configuration() -> dict:
    return tomllib.loads((ROOT / "agent-service/service.toml").read_text())


@pytest.fixture(scope="module")
def realm() -> dict:
    return yaml.safe_load((ROOT / "keycloak/realm.yaml").read_text())


@pytest.fixture(scope="module")
def tmpfs_options(service) -> dict[str, list[str]]:
    mounts = (entry.partition(":") for entry in service["tmpfs"])
    return {path: options.split(",") for path, _, options in mounts}


@pytest.fixture(scope="module")
def binds(service) -> dict[str, dict]:
    return {volume["target"]: volume for volume in service["volumes"]}


def test_the_service_is_hardened_and_never_pulled(service):
    """U13: a fixed user, an init, a read-only root, no capability, no privilege gain, no core."""
    assert service["user"] == OPERATOR_UID_GID
    assert service["init"] is True
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["ulimits"] == {"core": 0}
    assert service["restart"] == "unless-stopped"
    assert service["pull_policy"] == "never"
    assert service["profiles"] == ["agent-service"]


def test_the_service_is_one_replica_with_no_published_port_and_its_home_in_tmp(service):
    """U2: no replicas, no scale; the network is the anchor's alone and HOME is the tmpfs."""
    assert not {"deploy", "scale", "ports", "network_mode", "expose"} & service.keys()
    assert service["environment"] == {"HOME": "/tmp"}


@pytest.mark.parametrize("directory", ["/tmp", "/run/agent-service/cli"])
def test_a_writable_directory_is_a_size_bounded_private_tmpfs_for_the_user(
    tmpfs_options, directory
):
    """U6, U7: owned by 1000 with exactly mode 0700, nosuid, nodev, noexec and bounded."""
    options = tmpfs_options[directory]

    assert set(OWNER_ONLY_TMPFS) <= set(options)
    assert any(SIZE_BOUND.fullmatch(option) for option in options)


def test_the_service_has_no_writable_directory_but_its_two_tmpfs(service, tmpfs_options):
    assert set(tmpfs_options) == {"/tmp", "/run/agent-service/cli"}
    assert all(
        volume["type"] == "bind" and volume["read_only"] is True for volume in service["volumes"]
    )


def test_the_login_is_a_read_only_directory_bind_that_is_never_created(binds):
    """U8, U10: a directory so that `mv -T` reaches the running container; a missing source fails
    the start instead of becoming root's directory."""
    login = binds["/run/agent-service/login/claude"]

    assert login["source"] == LOGIN_SOURCE
    assert login["read_only"] is True
    assert login["bind"] == {"create_host_path": False}


def test_the_configuration_is_a_read_only_file_bind_from_the_checkout(binds):
    configuration = binds["/etc/agent-service/service.toml"]

    assert configuration["source"] == "./agent-service/service.toml"
    assert configuration["read_only"] is True
    assert configuration["bind"] == {"create_host_path": False}


def test_the_image_tag_is_the_locks_release_and_the_cli_version(service):
    """The tag names the agent-runtime release of the lock and the Claude CLI of the recipe."""
    lock = (ROOT / "agent-service/image/requirements.txt").read_text()
    dockerfile = (ROOT / "agent-service/image/Dockerfile").read_text()
    release = re.match(r"# agent-runtime v(\S+), commit ", lock).group(1)
    cli = re.search(r"^ARG CLAUDE_VERSION=(\S+)$", dockerfile, re.MULTILINE).group(1)

    assert service["image"] == f"edge-agent-service:{release}-{cli}"


def test_the_service_toml_paths_are_the_services_mount_targets(
    service, configuration, tmpfs_options, binds
):
    """U13: the working root is a tmpfs, the token file lies in the login bind, the binary is the
    image's, and the image's default command reads the configuration bind."""
    dockerfile = (ROOT / "agent-service/image/Dockerfile").read_text()
    token_file = PurePosixPath(configuration["profiles"]["claude"]["token_file"])

    assert configuration["cli"]["working_root"] in tmpfs_options
    assert str(token_file.parent) in binds
    assert token_file.name == "token"
    binary = configuration["cli"]["claude"]["binary"]
    assert f"--chmod=0555 /tmp/claude {binary}\n" in dockerfile
    assert 'CMD ["--config", "/etc/agent-service/service.toml"]' in dockerfile
    assert "/etc/agent-service/service.toml" in binds


def test_the_service_listens_on_every_interface_and_pins_the_cli_it_runs(configuration):
    """The networks are internal and isolated, so 0.0.0.0 reaches only members; the version pin
    equals the CLI the recipe installs."""
    dockerfile = (ROOT / "agent-service/image/Dockerfile").read_text()
    pinned = re.search(r"^ARG CLAUDE_VERSION=(\S+)$", dockerfile, re.MULTILINE).group(1)

    assert configuration["server"] == {"host": "0.0.0.0"}
    assert configuration["cli"]["claude"]["version"] == pinned


def test_the_identity_names_the_realms_issuer_audience_and_subscription_groups(
    configuration, realm
):
    """The issuer is the realm's public one, the keys come from it over https, the audience is
    the realm's bearer-only client and the subscription group is the realm's."""
    identity = configuration["identity"]
    issuer, jwks = urlsplit(identity["issuer"]), urlsplit(identity["jwks_url"])
    clients = {client["clientId"] for client in realm["clients"]}
    declared = declared_realm((ROOT / "keycloak/realm.yaml").read_text())
    groups = declared.identities[observe.GROUP.name]

    assert issuer.path == f"/realms/{realm['realm']}"
    assert (jwks.scheme, jwks.hostname) == ("https", issuer.hostname)
    assert jwks.path.startswith(issuer.path + "/")
    assert identity["audience"] in clients
    assert (
        set(identity["subscription_groups"]) == {"/services/agent-service/subscription"} <= groups
    )
    assert not {"subscription_group", "api_groups"} & identity.keys()


def test_every_app_is_a_realm_workload_client_with_a_service_account_and_overrides_no_model(
    configuration, realm
):
    """The workload clients of #183; no app declares a `tools` table (songmaker's MCP server
    lists them) or overrides the model (N2)."""
    service_accounts = {
        client["clientId"] for client in realm["clients"] if client.get("serviceAccountsEnabled")
    }

    assert configuration["apps"]
    for app in configuration["apps"].values():
        assert app["workload_client"] in service_accounts
        assert not {"tools", "purposes", "models"} & app.keys()


def test_the_service_has_no_purpose_tables_and_no_paid_route_and_serves_the_subscriptions_aliases(
    configuration,
):
    """Edge holds no API provider key, so there is no `[route]` and no `api_groups`."""
    assert not {"purposes", "route"} & configuration.keys()
    assert configuration["cli"]["claude"]["models"] == ["opus", "sonnet", "haiku"]


def test_the_one_profile_is_claude_with_its_token_file(configuration):
    assert configuration["profiles"] == {
        "claude": {
            "provider": "claude",
            "token_file": "/run/agent-service/login/claude/token",
        }
    }


def test_the_copresenter_sees_only_the_talk_so_presentator_has_no_tools_and_no_mcp_origin(
    configuration,
):
    assert not {"tools", "mcp_origin"} & configuration["apps"]["presentator"].keys()


def test_songmakers_mcp_origin_is_the_host_traefik_mcp_answers_to_on_mcp(configuration):
    """E1, T1: the origin's host is traefik-mcp's alias on `mcp` and the route's router host."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    origin = urlsplit(configuration["apps"]["songmaker"]["mcp_origin"])
    router = yaml.safe_load((ROOT / "mcp/dynamic/songmaker.yml").read_text())["http"]["routers"][
        "songmaker"
    ]

    assert (origin.scheme, origin.hostname) == ("https", "songmaker.mcp.internal")
    assert origin.hostname in compose["services"]["traefik-mcp"]["networks"]["mcp"]["aliases"]
    assert f"Host(`{origin.hostname}`)" in router["rule"]
    assert "mcp" in compose["services"][SERVICE]["networks"]
    assert "mcp-songmaker" not in compose["services"][SERVICE]["networks"]


def test_the_signing_key_and_the_ca_root_are_compose_secrets_from_the_manifests_paths(
    service, configuration
):
    """E3, B1, T1: `key_file` and `ca_file` are the service's secret targets, and each secret's
    source is the path the secrets manifest names for it, so the folder is never mounted."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    manifest = tomllib.loads((ROOT / "src/home_tools/secrets_manifest.toml").read_text())
    paths = {entry["name"]: entry["paths"] for entry in manifest["secret"]}
    key_secret, ca_secret = "agent_service_signing_key", "step_ca_root_certificate"

    assert set(service["secrets"]) == {key_secret, ca_secret}
    assert configuration["signing"] == {"key_file": f"/run/secrets/{key_secret}"}
    assert configuration["mcp"] == {"ca_file": f"/run/secrets/{ca_secret}"}
    assert [compose["secrets"][key_secret]["file"].replace("${HOME}", "~")] == [
        path.removesuffix("*") for path in paths["agent-service/signing-key"]
    ]
    assert [compose["secrets"][ca_secret]["file"].replace("${HOME}", "~")] == paths[
        "step-ca/root_ca.crt"
    ]
    assert "agent-service" in {
        reader
        for entry in manifest["secret"]
        if entry["name"] == "step-ca/root_ca.crt"
        for reader in entry["read_by"]
    }
    assert not any("signing" in str(volume["source"]) for volume in service["volumes"])
