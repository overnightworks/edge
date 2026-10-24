"""The secrets the packaged manifest has `hal setup` generate for an app: the entry, and a setup run against the packaged manifest."""

from __future__ import annotations

import re
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from conftest import FakeProbe, Sandbox, plant_secrets
from test_secrets_setup import FakeDisk, FakeTerminal
from test_secrets_view import findings_of
from test_setup import secrets_terminal

from home_tools import secrets_view
from home_tools.cli import main
from home_tools.secrets_setup import OPEN_PROMPT, PASTE_PROMPT, STILL_OPEN, UNSENDABLE_PASTE, Boundaries, Verdict
from home_tools.secrets_view import ManifestError, SecretEntry, SecretKind, load_manifest

PACKAGED_MANIFEST = secrets_view.MANIFEST_PATH
HOPIN_SESSION_SECRET = ".secrets/hopin/session-secret"
URL_SAFE_48_BYTES = re.compile(r"[A-Za-z0-9_-]{64}")
TURN_KEY_ID = ".secrets/cloudflare/turn-hopin/key-id"
TURN_API_TOKEN = ".secrets/cloudflare/turn-hopin/api-token"
CLOUDFLARE_DASHBOARD = "https://dash.cloudflare.com/"
NOW = datetime(2026, 10, 15, 9, 0)


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


@pytest.mark.parametrize(
    ("mode", "loose_subjects"),
    [(0o644, set()), (0o666, {f"~/{HOPIN_SESSION_SECRET}"})],
    ids=["0644 is the declared loosest mode", "0666 is loose"],
)
def test_hopins_session_secret_is_loose_only_beyond_0644(
    packaged_home: Sandbox, probe: FakeProbe, capsys: pytest.CaptureFixture[str], mode: int, loose_subjects: set[str]
) -> None:
    secret = packaged_home.home / HOPIN_SESSION_SECRET
    secret.write_text("planted")
    secret.chmod(mode)

    main(["secrets"], home=packaged_home.home, probe=probe, now=NOW)

    assert set(findings_of(capsys.readouterr().out, "loose-mode")) == loose_subjects


def test_by_service_lists_the_session_secret_under_hopin(
    packaged_home: Sandbox, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["secrets", "--by-service"], home=packaged_home.home, probe=probe, now=NOW)

    assert "| hopin | keycloak/clients/*, hopin/session-secret, cloudflare/turn-hopin/key-id, cloudflare/turn-hopin/api-token |" in capsys.readouterr().out


def test_home_next_lists_the_step_that_generates_the_missing_session_secret(
    packaged_home: Sandbox, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["next"], home=packaged_home.home, probe=probe, now=NOW)

    assert f"home secrets --setup (eigenes Terminalfenster) — erwartet: erzeugt und gespeichert: ~/{HOPIN_SESSION_SECRET} (644)" in (
        capsys.readouterr().out
    )


def test_the_packaged_manifest_holds_no_value_of_hopins_session_secret() -> None:
    assert not re.search(r"[A-Za-z0-9_-]{43,}", PACKAGED_MANIFEST.read_text())


# --- HopIn's TURN key: asked hidden, stored unverified (O6) ---------------------------------------------------------


@pytest.fixture
def turn_home(packaged_home: Sandbox) -> Sandbox:
    """The packaged manifest with every entry in place but HopIn's TURN key ID and API token."""
    (packaged_home.home / HOPIN_SESSION_SECRET).write_text("planted")
    for relative in (TURN_KEY_ID, TURN_API_TOKEN):
        (packaged_home.home / relative).unlink()
    return packaged_home


def never_verified(_source: object, _value: str) -> Verdict:
    pytest.fail("a TURN value is stored unverified and never sent to a provider")


def run_turn_setup(sandbox: Sandbox, capsys: pytest.CaptureFixture[str], terminal: FakeTerminal, disk: FakeDisk | None = None) -> tuple[int, str]:
    boundaries = Boundaries(terminal.boundary(), never_verified, (disk or FakeDisk()).sync)
    code = main(["setup", "--apply"], home=sandbox.home, secrets_setup_boundaries=boundaries, now=NOW)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.mark.parametrize("name", ["cloudflare/turn-hopin/key-id", "cloudflare/turn-hopin/api-token"])
def test_the_packaged_manifest_holds_hopins_turn_values_as_unverified_tokens_the_container_reads(name: str) -> None:
    entry = packaged_entry(name)

    assert (entry.kind, entry.read_by, entry.optional, entry.verifier, entry.source) == (SecretKind.UNVERIFIED_TOKEN, ("hopin",), False, None, None)
    assert entry.paths == (PurePosixPath(f".secrets/{name}"),)
    assert (entry.loosest_mode, bool(entry.loosest_mode_reason), bool(entry.purpose), entry.written_by_setup) == (0o644, True, True, True)


UNVERIFIED_ENTRY = '[[secret]]\nname = "turn"\nkind = "unverified-token"\nread_by = []\nrecreate = "r"\n'


@pytest.mark.parametrize(
    "keys",
    [
        'paths = ["~/.secrets/turn/a"]\nprovider = "cloudflare"\nresource = "zone"\nscope = ["a"]\nexpires_in_days = 30\npurpose = "p"\n',
        'paths = ["~/.secrets/turn/a", "~/.secrets/turn/b"]\n',
    ],
    ids=["a provider source", "two paths"],
)
def test_an_unverified_token_cannot_name_a_provider_to_verify_with_nor_two_files(tmp_path: Path, keys: str) -> None:
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(UNVERIFIED_ENTRY + keys)

    with pytest.raises(ManifestError):
        load_manifest(manifest)


def test_apply_asks_the_turn_values_hidden_in_order_and_stores_them_at_0644_in_a_0700_folder_unseen(
    turn_home: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal = FakeTerminal(pastes=["FAKE-TURN-KEY-ID-0001", "FAKE-TURN-API-TOKEN-0002"])

    code, output = run_turn_setup(turn_home, capsys, terminal)

    key_id, api_token = (turn_home.home / relative for relative in (TURN_KEY_ID, TURN_API_TOKEN))
    assert code == 0
    assert (key_id.read_text(), api_token.read_text()) == ("FAKE-TURN-KEY-ID-0001", "FAKE-TURN-API-TOKEN-0002")
    assert [stat.S_IMODE(path.stat().st_mode) for path in (key_id, api_token, key_id.parent)] == [0o644, 0o644, 0o700]
    assert terminal.prompts.count(PASTE_PROMPT) == 2
    assert "FAKE-TURN" not in output
    assert output.count("gespeichert, nicht geprüft: ") == 2


def test_the_wizard_names_where_the_turn_key_is_made_and_offers_only_the_dashboard(
    turn_home: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal = FakeTerminal(pastes=["FAKE-TURN-KEY-ID-0001", "FAKE-TURN-API-TOKEN-0002"], browser_opens=False)

    _, output = run_turn_setup(turn_home, capsys, terminal)

    assert packaged_entry("cloudflare/turn-hopin/key-id").recreate in output
    assert OPEN_PROMPT in terminal.prompts
    assert set(terminal.opened) == {CLOUDFLARE_DASHBOARD}
    assert f"Kein Browser ließ sich öffnen; die Seite von Hand öffnen: {CLOUDFLARE_DASHBOARD}" in output


def test_a_declined_open_prompt_opens_nothing_and_asks_for_no_paste(turn_home: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    terminal = FakeTerminal(enter=KeyboardInterrupt)

    code, _ = run_turn_setup(turn_home, capsys, terminal)

    assert code == 130
    assert terminal.opened == []
    assert PASTE_PROMPT not in terminal.prompts
    assert not any((turn_home.home / relative).exists() for relative in (TURN_KEY_ID, TURN_API_TOKEN))


def test_an_empty_turn_paste_skips_the_value_and_leaves_it_open(turn_home: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    code, output = run_turn_setup(turn_home, capsys, FakeTerminal())

    assert code == 1
    assert STILL_OPEN + "cloudflare/turn-hopin/key-id, cloudflare/turn-hopin/api-token" in output
    assert not any((turn_home.home / relative).exists() for relative in (TURN_KEY_ID, TURN_API_TOKEN))


def test_a_non_ascii_turn_paste_is_refused_unshown_and_unwritten(turn_home: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    canary = "FAKE-TURN-\u00e4-0003"

    code, output = run_turn_setup(turn_home, capsys, FakeTerminal(pastes=[canary]))

    assert code == 1
    assert UNSENDABLE_PASTE in output
    assert canary not in output
    assert not (turn_home.home / TURN_KEY_ID).exists()


def test_an_interrupt_between_the_two_turn_values_keeps_the_first_and_the_rerun_asks_only_the_second(
    turn_home: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_turn_setup(turn_home, capsys, FakeTerminal(pastes=["FAKE-TURN-KEY-ID-0001", KeyboardInterrupt]))
    assert code == 130
    assert (turn_home.home / TURN_KEY_ID).read_text() == "FAKE-TURN-KEY-ID-0001"
    assert not (turn_home.home / TURN_API_TOKEN).exists()

    rerun = FakeTerminal(pastes=["FAKE-TURN-API-TOKEN-0002"])
    code, _ = run_turn_setup(turn_home, capsys, rerun)

    assert code == 0
    assert rerun.prompts.count(PASTE_PROMPT) == 1
    assert (turn_home.home / TURN_API_TOKEN).read_text() == "FAKE-TURN-API-TOKEN-0002"


def test_the_view_shows_the_stored_turn_values_as_not_verified(packaged_home: Sandbox, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> None:
    main(["secrets"], home=packaged_home.home, probe=probe, now=NOW)

    rows = {line for line in capsys.readouterr().out.splitlines() if line.startswith(("| cloudflare/turn-hopin/", "cloudflare/turn-hopin/"))}
    assert len(rows) == 2
    assert all("Token, ungeprüft" in row and "nicht geprüft" in row for row in rows)


def test_home_next_says_the_missing_turn_values_are_stored_not_verified(
    turn_home: Sandbox, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["next"], home=turn_home.home, probe=probe, now=NOW)

    assert f"gespeichert, nicht geprüft: ~/{TURN_KEY_ID} (644)" in capsys.readouterr().out
