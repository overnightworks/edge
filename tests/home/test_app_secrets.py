"""The secrets the packaged manifest has `hal setup` generate for an app: the entry, and a setup run against the packaged manifest."""

from __future__ import annotations

import re
import stat
from pathlib import PurePosixPath

import pytest
from conftest import Sandbox, plant_secrets
from test_setup import secrets_terminal

from home_tools import secrets_view
from home_tools.cli import main
from home_tools.secrets_view import SecretEntry, SecretKind, load_manifest

PACKAGED_MANIFEST = secrets_view.MANIFEST_PATH
HOPIN_SESSION_SECRET = ".secrets/hopin/session-secret"
URL_SAFE_48_BYTES = re.compile(r"[A-Za-z0-9_-]{64}")


def packaged_entry(name: str) -> SecretEntry:
    return next(entry for entry in load_manifest(PACKAGED_MANIFEST) if entry.name == name)


@pytest.fixture
def packaged_home(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    """The sandbox home under the packaged manifest with every entry in place but hopin's session secret."""
    monkeypatch.setattr(secrets_view, "MANIFEST_PATH", PACKAGED_MANIFEST)
    plant_secrets(sandbox.home)
    (sandbox.home / HOPIN_SESSION_SECRET).unlink()
    return sandbox


def run_packaged_setup(sandbox: Sandbox, capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    main(["setup", *flags], home=sandbox.home, secrets_setup_boundaries=secrets_terminal(True))
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_the_packaged_manifest_holds_hopins_session_secret_as_a_generated_password_the_container_reads() -> (
    None
):
    entry = packaged_entry("hopin/session-secret")

    assert (entry.kind, entry.paths, entry.read_by, entry.optional) == (
        SecretKind.SERVICE_PASSWORD,
        (PurePosixPath(".secrets/hopin/session-secret"),),
        ("hopin",),
        False,
    )
    assert (entry.loosest_mode, bool(entry.loosest_mode_reason)) == (0o644, True)


def test_hopin_reads_the_keycloak_client_secrets_besides_the_readers_it_joins() -> None:
    assert {"hopin", "keycloak-config-cli", "edge"} <= set(
        packaged_entry("keycloak/clients/*").read_by
    )


def test_the_dry_run_lists_hopins_session_secret_as_missing_and_writes_nothing(
    packaged_home: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    output = run_packaged_setup(packaged_home, capsys)

    assert "fehlt: hopin/session-secret" in output
    assert not (packaged_home.home / HOPIN_SESSION_SECRET).exists()


def test_apply_generates_hopins_session_secret_unseen_at_0644_in_a_0700_folder_and_a_rerun_keeps_it(
    packaged_home: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    output = run_packaged_setup(packaged_home, capsys, "--apply")

    secret = packaged_home.home / HOPIN_SESSION_SECRET
    value = secret.read_text()
    assert URL_SAFE_48_BYTES.fullmatch(value)
    assert (stat.S_IMODE(secret.stat().st_mode), stat.S_IMODE(secret.parent.stat().st_mode)) == (
        0o644,
        0o700,
    )
    assert value not in output

    assert value not in run_packaged_setup(packaged_home, capsys, "--apply")
    assert secret.read_text() == value
