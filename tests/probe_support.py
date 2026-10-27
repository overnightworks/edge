"""Importing the modules of `probe/agent-service`: they import each other by bare name from their
own directory, so a test puts that directory on the path for its module and removes every module
it brought in afterwards."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def probe_imports() -> Iterator[None]:
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    sys.path[:0] = [str(ROOT / "probe" / "agent-service"), str(ROOT / "agent-service")]
    try:
        yield
    finally:
        sys.path[:] = path_before
        for name in set(sys.modules) - modules_before:
            sys.modules.pop(name)


TOKEN = "sk-ant-oat01-dummy-token"
LOGIN = "/run/agent-service/login/claude"
CONFIGURATION = "/etc/agent-service/service.toml"


@contextmanager
def login_probe_modules() -> Iterator[SimpleNamespace]:
    """The modules of the login command (#457), imported for the length of the `with`."""
    with probe_imports():
        import cells_12b
        import census_client
        import egress
        import image
        import login_checks

        yield SimpleNamespace(
            cells=cells_12b, checks=login_checks, census=census_client, egress=egress, image=image
        )


def make_login(directory: Path, content: bytes = TOKEN.encode() + b"\n") -> Path:
    directory.mkdir()
    (directory / "token").write_bytes(content)
    return directory


def resolved(*, login_source="/login", configuration_source="/scratch/service.toml") -> dict:
    return {
        "services": {
            "agent-service": {
                "read_only": True,
                "logging": {"driver": "local"},
                "volumes": [
                    {"type": "bind", "source": login_source, "target": LOGIN, "read_only": True},
                    {
                        "type": "bind",
                        "source": configuration_source,
                        "target": CONFIGURATION,
                        "read_only": True,
                    },
                ],
            },
            "agent-egress": {"logging": {"driver": "local"}},
            "agent-issuer": {"logging": {"driver": "local"}},
        }
    }
