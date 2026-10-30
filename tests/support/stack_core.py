"""What the `test_stack_core` tests share with other test modules."""

import hashlib
from pathlib import Path

import pytest

TRAEFIK_FILE = "traefik.yml"


def bind(source: Path, target: str) -> dict:
    """A bind as `docker compose config --format json` resolves the short syntax `./x:/y:ro`."""
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
        "bind": {"create_host_path": True},
    }


def bind_core_files(config: dict, checkout: Path) -> dict:
    """compose.yaml's mounts of socket-proxy and traefik, resolved for `checkout` (cell P7)."""
    services = config["services"]
    services["socket-proxy"]["volumes"] = [
        bind(Path("/var/run/docker.sock"), "/var/run/docker.sock"),
        {
            "type": "volume",
            "source": "socket-proxy-run",
            "target": "/run/socket-proxy",
            "volume": {},
        },
    ]
    services["traefik"]["depends_on"] = {
        "socket-proxy": {"condition": "service_started", "required": True}
    }
    services["traefik"]["volumes"] = [
        bind(checkout / TRAEFIK_FILE, "/etc/traefik/traefik.yml"),
        bind(checkout / "dynamic", "/dynamic"),
        {
            "type": "volume",
            "source": "socket-proxy-run",
            "target": "/run/socket-proxy",
            "read_only": True,
            "volume": {},
        },
    ]
    return config


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    tree = tmp_path / "git" / "edge"
    (tree / "dynamic").mkdir(parents=True)
    (tree / TRAEFIK_FILE).write_text("entryPoints: {}\n")
    return tree


@pytest.fixture
def config(checkout: Path) -> dict:
    return bind_core_files(
        {
            "services": {
                "socket-proxy": {},
                "traefik": {},
                "hello": {"profiles": ["probe"], "volumes": [bind(checkout / "x.yml", "/x")]},
            }
        },
        checkout,
    )
