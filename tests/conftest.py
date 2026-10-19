"""The scripted Docker host, the CLI driver and the network backstop that test modules share."""

import json
import os
import socket
from pathlib import Path

import pytest
from edge_backup_support import (
    CANARY_PASSWORD,
    SONGMAKER_TOML,
    Machine,
    Outcome,
)

from edge_backup import cli, host

LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}


@pytest.fixture
def loopback_only(monkeypatch):
    """The backstop: whatever a test builds, a connection beyond loopback fails loudly."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and address[0] not in LOOPBACK_ADDRESSES
        ):
            raise AssertionError(f"a test connection leaves loopback: {address[0]}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "backups/restic").mkdir(parents=True)
    (tmp_path / "backups/restic/config").write_text("restic repository config")
    password = tmp_path / ".secrets/restic/password"
    password.parent.mkdir(parents=True, mode=0o700)
    password.write_text(CANARY_PASSWORD)
    password.chmod(0o600)
    return tmp_path


@pytest.fixture
def machine(monkeypatch, home) -> Machine:
    machine = Machine(status_file=home / ".local/state/edge/backup-status.json")
    monkeypatch.setattr(host, "run", machine.run)
    monkeypatch.setattr(host, "now", machine.clock.now)
    monkeypatch.setattr(host, "monotonic", machine.clock.monotonic)
    monkeypatch.setattr(host, "sleep", machine.clock.sleep)
    return machine


def _status_if_readable(status_file: Path) -> dict | None:
    """A status a run refused to touch may be unreadable or no JSON: then there is none to read."""
    if not status_file.exists() or not os.access(status_file, os.R_OK):
        return None
    try:
        return json.loads(status_file.read_text())
    except ValueError:
        return None


@pytest.fixture
def edge_backup(machine, home, capsys):
    (home / "songmaker-only.toml").write_text(SONGMAKER_TOML)
    songmaker_only = ("--apps", str(home / "songmaker-only.toml"))

    def run(*arguments: str) -> Outcome:
        """Songmaker alone unless the test declares its apps: most cases are about one app."""
        declared = () if "--apps" in arguments else songmaker_only
        exit_code = cli.main([*declared, *arguments])
        captured = capsys.readouterr()
        status_file = home / ".local/state/edge/backup-status.json"
        status = _status_if_readable(status_file)
        return Outcome(exit_code, captured.out, captured.err, status)

    return run
