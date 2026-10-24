"""A one-off container of the edge stack logs to driver `none`; a long-running service does not.

The one-offs are the services of the `apply` profile: `compose run` starts them for their output,
which belongs to the caller alone and must not reach the journal a second time.
"""

import json
from pathlib import Path

import pytest

from edge import compose
from edge_backup.restic import container_argv
from edge_backup.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
APPLY_PROFILE = "apply"
NO_LOGGING = {"driver": "none"}
OVERLAYS = sorted(ROOT.glob("probe/*/compose*.yaml"))
PLACEHOLDERS = {
    "DOCKER_GID": "999",
    "EDGE_DOMAIN": "edge.test",
    "PROBE_ADMIN_URL": "https://admin.test",
    "PROBE_CA_JSON": "/tmp/ca.json",
    "PROBE_CONFIG_DIR": "/tmp",
    "PROBE_EGRESS_STATIC": "/tmp/static.yml",
    "PROBE_KEYCLOAK_SUBNET": "172.31.240.0/24",
    "PROBE_SECRETS_DIR": "/tmp",
    "PROBE_TRAEFIK_CONFIG": "/tmp/traefik.yml",
}


def resolved(monkeypatch, *overlays: Path) -> dict:
    """`docker compose config` of compose.yaml and the overlays, every profile switched on."""
    for name, value in PLACEHOLDERS.items():
        monkeypatch.setenv(name, value)
    files = [argument for path in (ROOT / "compose.yaml", *overlays) for argument in ("-f", path)]
    result = compose.run_docker(
        ["docker", "compose", "--project-directory", str(ROOT), *map(str, files),
         "--profile", "*", "config", "--format", "json"],
        compose.READ_SECONDS,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def one_off_names(config: dict) -> set[str]:
    return {
        name
        for name, service in config["services"].items()
        if service.get("profiles") == [APPLY_PROFILE]
    }


def test_every_apply_profile_service_logs_to_none_and_no_other_service_does(monkeypatch):
    """#384 U4"""
    services = resolved(monkeypatch)["services"]

    one_offs = one_off_names({"services": services})
    assert one_offs == {"keycloak-config-cli", "realm-observer", "agent-smoke"}
    assert {name for name, service in services.items() if service.get("logging") == NO_LOGGING} == (
        one_offs
    )


@pytest.mark.parametrize("overlay", OVERLAYS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_an_overlay_keeps_the_one_offs_logging_to_none(monkeypatch, overlay):
    """#384 U5"""
    services = resolved(monkeypatch, overlay)["services"]

    assert all(
        services[name].get("logging") == NO_LOGGING
        for name in one_off_names({"services": services})
    )


def test_the_restic_container_starts_with_log_driver_none(tmp_path):
    """#384 U1"""
    argv = container_argv(Settings.for_home(tmp_path, uid=1000, gid=1000))

    assert argv[argv.index("--log-driver") + 1] == "none"
