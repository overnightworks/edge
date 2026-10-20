from __future__ import annotations

import os
import select
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import pytest
from conftest import FAKE_SECRET_VALUE, TEST_MANIFEST, FakeProbe, plant_secrets, symlinked_folder_fix

from home_tools import cli, secrets_view
from home_tools.secrets_setup import BY_HAND_STEP, Boundaries, Terminal
from home_tools.secrets_view import ManifestError, SecretEntry, SecretKind, load_manifest
from home_tools.tidy_home import Layout

NOW = datetime(2026, 10, 15, 9, 0)
PACKAGED_MANIFEST = secrets_view.MANIFEST_PATH
EDGE_EXTERNAL_ENTRIES = (
    "cloudflare/fleet-read",
    "github/fleet-read",
    "opentofu/state-passphrase",
    "cloudflare/fleet-write",
    "github/fleet-admin",
)
INVENTORY = {
    *EDGE_EXTERNAL_ENTRIES,
    "github/token",
    "gitlab",
    "sonarcloud/token",
    "claudebot.env",
    "claudebot-runtime.env",
    "secrets/README",
    "cloudflared",
    "gmail-cleanup",
    "ssh/id_ed25519",
    "ssh/id_ed25519.ppk",
    "gh",
    "claude",
    "codex",
    "songmaker",
    "github/overnightworks-read-token",
    "restic/password",
    "cloudflared/tunnel",
    "claude-measure-token",
    "opentofu/state",
    "opentofu/state-backup",
    "opentofu/plans",
    "opentofu/tmp",
    "step-ca/root_ca_key",
    "step-ca/password",
    "step-ca/root_ca.crt",
    "step-ca/pending-root/root_ca_key",
    "step-ca/pending-root/root_ca.crt",
    "keycloak/db-password",
    "keycloak/config-cli-secret",
    "keycloak/bootstrap-admin-password",
    "keycloak/clients/*",
}
DECLARED_MODE_ENTRY = (
    '[[secret]]\nname = "step-ca/password"\nkind = "token"\npaths = ["~/.secrets/step-ca/password"]\nread_by = []\nrecreate = "r"\n'
    'loosest_mode = 0o644\nloosest_mode_reason = "uid 1000 reads it"\n'
)
WILDCARD_ENTRY = (
    '\n[[secret]]\nname = "step-ca/any"\nkind = "token"\npaths = ["~/.secrets/step-ca/*"]\nread_by = []\nrecreate = "r"\n'
    'loosest_mode = 0o644\nloosest_mode_reason = "a container reads the folder"\n'
)
RESTIC_RECREATE = (
    "Aus deinem Passwortmanager zurückholen. "
    "Nie neu erzeugen, solange unter ~/backups/restic Backups liegen: ein neues Passwort macht sie unlesbar."
)
RETIRED_ENTRY = """
[[secret]]
name = "github/token"
kind = "token"
paths = ["~/.secrets/github/token"]
read_by = []
recreate = "nicht neu anlegen"
retired_on = 2026-10-14
revoke_url = "https://github.com/settings/tokens"
"""


def home_secrets(home: Path, probe: FakeProbe, *options: str) -> int:
    return cli.main(["secrets", *options], home=home, probe=probe, now=NOW)


def cells_of(table: str) -> list[list[str]]:
    """The body rows of a Markdown table, cells stripped."""
    return [[cell.strip() for cell in line.strip("|").split(" | ")] for line in table.strip().splitlines()[2:]]


def rows_of(output: str) -> dict[str, list[str]]:
    """The status table, keyed by entry name."""
    return {cells[0]: cells for cells in cells_of(output.split("\n\n")[1])}


def findings_of(output: str, rule: str) -> dict[str, list[str]]:
    """The findings of one rule, keyed by subject: [detail, recommendation]."""
    table = output.split("## Funde\n\n", 1)[1].split("\n\n")[0] if "## Funde" in output else ""
    return {cells[1]: cells[2:] for cells in cells_of(table) if cells[0] == rule}


def finding_subjects(output: str) -> set[str]:
    """The subject of every finding, whatever its rule."""
    table = output.split("## Funde\n\n", 1)[1].split("\n\n")[0] if "## Funde" in output else ""
    return {cells[1] for cells in cells_of(table)}


def test_a_complete_home_lists_every_entry_and_writes_only_its_report(clean_home: Path, probe: FakeProbe, capsys) -> None:
    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    rows = rows_of(output)
    assert code == 0
    assert set(rows) == {"gitlab", "cloudflared", "gh"}
    assert {name: row[2] for name, row in rows.items()} == dict.fromkeys(rows, "vorhanden")
    assert {name: row[3] for name, row in rows.items()} == {"gitlab": "nicht geprüft", "cloudflared": "nicht geprüft"} | {
        "gh": "geprüft: gültig"
    }
    assert all(row[4] == "Ablauf unbekannt" for row in rows.values())
    assert "Keine Funde." in output
    assert Layout(clean_home).secrets_report_file.read_text() == output
    assert probe.calls == [("gh", "auth", "status")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_no_secret_value_reaches_the_output_or_the_report_and_no_secret_file_is_opened(clean_home: Path, probe: FakeProbe, capsys) -> None:
    unreadable = [path for path in Layout(clean_home).secrets_dir.rglob("*") if path.is_file()]
    for path in unreadable:
        path.chmod(0)  # opening one would now fail; stat still works

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out + Layout(clean_home).secrets_report_file.read_text()
    assert code == 0
    assert not any(part in output for part in FAKE_SECRET_VALUE.split())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0 for path in unreadable)


def symlink_the_report_file(layout: Layout, report_file: Path) -> None:
    layout.state_dir.mkdir(parents=True)
    report_file.symlink_to(layout.secrets_dir / "gitlab")


def hard_link_the_report_file(layout: Layout, report_file: Path) -> None:
    layout.state_dir.mkdir(parents=True)
    os.link(layout.secrets_dir / "gitlab", report_file)


def symlink_the_state_directory(layout: Layout, report_file: Path) -> None:
    layout.state_dir.parent.mkdir(parents=True)
    layout.state_dir.symlink_to(layout.secrets_dir)


def symlink_an_ancestor_of_the_state_directory(layout: Layout, report_file: Path) -> None:
    local = layout.state_dir.parent.parent
    local.mkdir()
    (local / "state").symlink_to(layout.secrets_dir)


def secret_contents(layout: Layout) -> dict[Path, str]:
    return {path: path.read_text() for path in layout.secrets_dir.rglob("*") if path.is_file()}


def state_file_of(layout: Layout, command: str) -> Path:
    return {"secrets": layout.secrets_report_file, "check": layout.check_report_file}[command]


@pytest.mark.parametrize("plant_link", [symlink_the_report_file, hard_link_the_report_file])
@pytest.mark.parametrize("command", ["secrets", "check"])
def test_a_linked_report_file_is_replaced_and_never_written_through_into_the_secrets(
    clean_home: Path, probe: FakeProbe, runner, command: str, plant_link: Callable[[Layout, Path], None]
) -> None:
    layout = Layout(clean_home)
    report_file = state_file_of(layout, command)
    secrets_before = secret_contents(layout)
    plant_link(layout, report_file)

    cli.main([command], home=clean_home, run=runner, probe=probe, now=NOW)

    assert not report_file.is_symlink()
    assert report_file.stat().st_nlink == 1
    assert secret_contents(layout) == secrets_before


@pytest.mark.parametrize("plant_link", [symlink_the_state_directory, symlink_an_ancestor_of_the_state_directory])
@pytest.mark.parametrize("command", ["secrets", "check"])
def test_a_symlink_on_the_way_to_the_state_directory_is_refused_and_never_carries_the_report_into_the_secrets(
    clean_home: Path, probe: FakeProbe, runner, capsys, command: str, plant_link: Callable[[Layout, Path], None]
) -> None:
    layout = Layout(clean_home)
    secrets_before = secret_contents(layout)
    plant_link(layout, state_file_of(layout, command))

    code = cli.main([command], home=clean_home, run=runner, probe=probe, now=NOW)

    assert code == 3
    assert capsys.readouterr().err.startswith("home: unerwarteter Fehler (")
    assert secret_contents(layout) == secrets_before


def remove_gitlab(home: Path) -> None:
    (home / ".secrets" / "gitlab").unlink()


def remove_the_wildcard_match(home: Path) -> None:
    (home / ".secrets" / "cloudflared" / "fake.json").unlink()


def remove_every_secret(home: Path) -> None:
    shutil.rmtree(home / ".secrets")


def put_a_file_where_a_directory_belongs(home: Path) -> None:
    shutil.rmtree(home / ".secrets" / "cloudflared")
    (home / ".secrets" / "cloudflared").write_text("not a directory")
    (home / ".secrets" / "cloudflared").chmod(0o600)


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (remove_gitlab, {"gitlab": "fehlt", "cloudflared": "vorhanden"}),
        (remove_the_wildcard_match, {"gitlab": "vorhanden", "cloudflared": "fehlt"}),
        (remove_every_secret, {"gitlab": "fehlt", "cloudflared": "fehlt"}),
        (put_a_file_where_a_directory_belongs, {"gitlab": "vorhanden", "cloudflared": "unbekannt"}),
    ],
    ids=["one file missing", "no file matches the wildcard", "no ~/.secrets at all", "the filesystem cannot say"],
)
def test_each_entry_shows_present_missing_or_unknown_and_every_gap_is_a_finding(
    clean_home: Path, probe: FakeProbe, capsys, damage: Callable[[Path], None], expected: dict[str, str]
) -> None:
    damage(clean_home)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    rows = rows_of(output)
    assert code == 1
    assert {name: rows[name][2] for name in expected} == expected
    gaps = {name for name, presence in expected.items() if presence != "vorhanden"}
    assert set(findings_of(output, "missing")) | set(findings_of(output, "presence-unknown")) == gaps


@pytest.mark.parametrize(
    ("exit_code", "shown", "finding"),
    [
        (0, "geprüft: gültig", None),
        (1, "geprüft: ungültig", "verification-failed"),
        (None, "Prüfung nicht möglich", "verification-unreachable"),
    ],
    ids=["the probe passes", "the probe fails", "the probe hangs or is missing"],
)
def test_only_github_is_verified_and_its_outcome_is_shown(
    clean_home: Path, probe: FakeProbe, capsys, exit_code: int | None, shown: str, finding: str | None
) -> None:
    probe.code = exit_code

    home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    assert rows_of(output)["gh"][3] == shown
    verification_findings = {rule: findings_of(output, rule) for rule in ("verification-failed", "verification-unreachable")}
    assert {rule for rule, found in verification_findings.items() if "gh" in found} == ({finding} if finding else set())


def test_a_file_no_entry_names_is_reported_as_unknown_and_left_alone(clean_home: Path, probe: FakeProbe, capsys) -> None:
    stray = clean_home / ".secrets" / "old-backup.env"
    stray.write_text(FAKE_SECRET_VALUE)
    stray.chmod(0o600)
    (clean_home / ".secrets" / "latest").symlink_to(stray)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    assert code == 1
    unknown = findings_of(output, "unknown-file")
    assert list(unknown) == ["~/.secrets/latest", "~/.secrets/old-backup.env"]
    assert unknown["~/.secrets/latest"] == ["-", "~/.secrets/latest ins Manifest eintragen oder löschen"]
    assert not findings_of(output, "loose-mode")
    assert stray.read_text() == FAKE_SECRET_VALUE


def test_a_newline_or_pipe_in_a_file_name_keeps_the_table_intact(clean_home: Path, probe: FakeProbe, capsys) -> None:
    stray = clean_home / ".secrets" / "weird\nna|me.txt"
    stray.write_text(FAKE_SECRET_VALUE)
    stray.chmod(0o600)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    assert code == 1
    assert list(findings_of(output, "unknown-file")) == ["~/.secrets/weird\\nna\\|me.txt"]
    assert Layout(clean_home).secrets_report_file.read_text() == output


def test_loose_modes_are_reported_and_never_changed(clean_home: Path, probe: FakeProbe, capsys) -> None:
    token, directory = clean_home / ".secrets" / "gitlab", clean_home / ".secrets" / "cloudflared"
    token.chmod(0o644)
    directory.chmod(0o755)
    (clean_home / ".secrets" / "cloudflared" / "cert.pem").chmod(0o400)

    code = home_secrets(clean_home, probe)

    loose = findings_of(capsys.readouterr().out, "loose-mode")
    assert code == 1
    assert loose == {
        "~/.secrets/gitlab": ["hat Rechte 644, höchstens 600", "`chmod 600 ~/.secrets/gitlab`"],
        "~/.secrets/cloudflared": ["hat Rechte 755, höchstens 700", "`chmod 700 ~/.secrets/cloudflared`"],
    }
    assert (stat.S_IMODE(token.stat().st_mode), stat.S_IMODE(directory.stat().st_mode)) == (0o644, 0o755)


@pytest.mark.parametrize(
    ("manifest", "file_mode", "loose_findings"),
    [
        (DECLARED_MODE_ENTRY, 0o644, {}),
        (
            DECLARED_MODE_ENTRY,
            0o666,
            {"~/.secrets/step-ca/password": ["hat Rechte 666, höchstens 644 (uid 1000 reads it)", "`chmod 644 ~/.secrets/step-ca/password`"]},
        ),
        (
            DECLARED_MODE_ENTRY.split("loosest_mode")[0],
            0o644,
            {"~/.secrets/step-ca/password": ["hat Rechte 644, höchstens 600", "`chmod 600 ~/.secrets/step-ca/password`"]},
        ),
        (DECLARED_MODE_ENTRY + WILDCARD_ENTRY, 0o644, {}),
        (
            DECLARED_MODE_ENTRY.split("loosest_mode")[0] + WILDCARD_ENTRY,
            0o644,
            {"~/.secrets/step-ca/password": ["hat Rechte 644, höchstens 600", "`chmod 600 ~/.secrets/step-ca/password`"]},
        ),
        (
            DECLARED_MODE_ENTRY.replace("0o644", "0o640") + WILDCARD_ENTRY.replace("0o644", "0o604"),
            0o644,
            {"~/.secrets/step-ca/password": ["hat Rechte 644, höchstens 600", "`chmod 600 ~/.secrets/step-ca/password`"]},
        ),
    ],
    ids=[
        "M1 declared mode is no finding",
        "M2 looser than declared is fixed to the declared mode",
        "M3 no declaration means 0600",
        "two declarations of the same mode agree",
        "the strictest declaration wins over a looser wildcard",
        "two declarations allow only the bits both allow",
    ],
)
def test_a_file_is_judged_against_the_mode_its_entry_declares(
    tmp_path: Path, use_manifest, probe: FakeProbe, capsys, manifest: str, file_mode: int, loose_findings: dict[str, list[str]]
) -> None:
    """#195 M1-M3: the declared mode replaces 0600 for that entry's files only, and the fix names it."""
    home = tmp_path / "home"
    use_manifest(manifest)
    plant_secrets(home)
    (home / ".secrets" / "step-ca" / "password").chmod(file_mode)

    home_secrets(home, probe)

    assert findings_of(capsys.readouterr().out, "loose-mode") == loose_findings


@pytest.mark.parametrize(
    ("file_name", "loose_findings"),
    [
        ("root_ca_key", {"~/.secrets/step-ca/.pending-root/root_ca_key": ["hat Rechte 644, höchstens 600", "`chmod 600 ~/.secrets/step-ca/.pending-root/root_ca_key`"]}),
        ("root_ca.crt", {}),
    ],
    ids=["a pending root key at 0644 is loose", "a pending root certificate at 0644 is fine"],
)
def test_only_the_pending_root_certificate_may_be_0644(
    tmp_path: Path, probe: FakeProbe, capsys, file_name: str, loose_findings: dict[str, list[str]]
) -> None:
    home = tmp_path / "home"
    pending = home / ".secrets" / "step-ca" / ".pending-root"
    pending.mkdir(parents=True, mode=0o700)
    (pending / file_name).write_text("x")
    (pending / file_name).chmod(0o644)
    for folder in (pending, pending.parent, home / ".secrets"):
        folder.chmod(0o700)

    home_secrets(home, probe)

    assert findings_of(capsys.readouterr().out, "loose-mode") == loose_findings


def test_the_files_go_live_and_init_write_are_known_and_need_no_change(tmp_path: Path, probe: FakeProbe, capsys) -> None:
    """#195 M5: laid out as keycloak/go-live and step-ca/init write them, `home check` reports nothing for them."""
    home = tmp_path / "home"
    files = {
        "step-ca": {"password": 0o644, "root_ca_key": 0o600, "root_ca.crt": 0o644},
        "step-ca/.pending-root": {"root_ca_key": 0o600, "root_ca.crt": 0o644},
        "keycloak": {"db-password": 0o644, "config-cli-secret": 0o644, "bootstrap-admin-password": 0o644},
        "keycloak/clients": {"edge": 0o644, "presentator": 0o644},
    }
    for folder, modes in files.items():
        (home / ".secrets" / folder).mkdir(parents=True, mode=0o700)
        for name, mode in modes.items():
            (home / ".secrets" / folder / name).write_text("x")
            (home / ".secrets" / folder / name).chmod(mode)
    for folder in ("keycloak/clients", "keycloak", "step-ca/.pending-root", "step-ca", ""):
        (home / ".secrets" / folder).chmod(0o700)

    home_secrets(home, probe)

    output = capsys.readouterr().out
    for rule in ("loose-mode", "unknown-file", "closed-folder", "not-a-folder", "symlinked-folder"):
        assert findings_of(output, rule) == {}
    assert not [name for name in findings_of(output, "missing") if name.startswith(("step-ca/", "keycloak/"))]


def test_a_home_where_neither_unit_is_set_up_has_no_finding_about_their_files(tmp_path: Path, probe: FakeProbe, capsys) -> None:
    """#195 H1: each unit's own --check owns presence; the daily check never calls its files missing."""
    home = tmp_path / "home"
    (home / ".secrets").mkdir(parents=True, mode=0o700)

    home_secrets(home, probe)

    subjects = finding_subjects(capsys.readouterr().out)
    assert not {subject for subject in subjects if subject.startswith(("step-ca/", "keycloak/", "~/.secrets/step-ca", "~/.secrets/keycloak"))}


@pytest.mark.parametrize(
    ("recorded", "shown", "findings"),
    [("", "Widerruf unbestätigt", ["github/token"]), ("revocation_confirmed = 2026-10-15\n", "Widerruf bestätigt 15.10.2026", [])],
    ids=["revocation unconfirmed", "revocation confirmed"],
)
def test_a_retired_entry_shows_its_revoke_link_and_whether_its_revocation_is_confirmed(
    use_manifest, clean_home: Path, probe: FakeProbe, capsys, recorded: str, shown: str, findings: list[str]
) -> None:
    """#21 point 1: a confirmed revocation is shown with its date and raises no revocation-unconfirmed finding."""
    use_manifest(TEST_MANIFEST + RETIRED_ENTRY + recorded)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    retired = rows_of(output)["github/token"]
    assert code == (1 if findings else 0)
    assert retired[2:5] == ["zurückgezogen", shown, "Ablauf unbekannt"]
    assert retired[-1] == "Widerruf: https://github.com/settings/tokens"
    assert list(findings_of(output, "revocation-unconfirmed")) == findings
    assert "github/token" not in findings_of(output, "missing")


KNOWN_FILES = """
[[secret]]
name = "secrets/README"
kind = "documentation"
paths = ["~/.secrets/README"]
read_by = []
recreate = "von Hand"

[[secret]]
name = "runtime.env"
kind = "configuration"
paths = ["~/.secrets/runtime.env"]
read_by = ["bot.service"]
recreate = "von Hand"

[[secret]]
name = "ppk"
kind = "derived-mirror"
paths = ["~/.secrets/ppk"]
read_by = []
recreate = "puttygen"
mirror_of = "gitlab"
optional = true
elsewhere = "/etc/example (root)"
"""


def test_known_non_secret_files_show_their_kind_and_a_missing_optional_entry_is_no_finding(
    use_manifest, clean_home: Path, probe: FakeProbe, capsys
) -> None:
    """#21 points 2-4: documentation and configuration are known files; an optional entry may be absent; copies elsewhere are named."""
    use_manifest(TEST_MANIFEST + KNOWN_FILES)
    for name in ("README", "runtime.env"):
        (clean_home / ".secrets" / name).write_text("no secret")
        (clean_home / ".secrets" / name).chmod(0o600)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    rows = rows_of(output)
    assert code == 0
    assert {name: rows[name][1:3] for name in ("secrets/README", "runtime.env", "ppk")} == {
        "secrets/README": ["Dokumentation", "vorhanden"],
        "runtime.env": ["Konfiguration", "vorhanden"],
        "ppk": ["abgeleitete Kopie", "fehlt (optional)"],
    }
    assert rows["ppk"][5] == "~/.secrets/ppk; /etc/example (root)"
    assert "Keine Funde." in output


def close_to(mode: int, folder: str) -> Callable[[Path], None]:
    def close(secrets: Path) -> None:
        (secrets / folder).chmod(mode)

    return close


@pytest.fixture
def reopened_secrets(clean_home: Path) -> Iterator[Path]:
    """Gives ~/.secrets and the folders directly under it back to their owner afterwards, so the temporary home can be removed."""
    secrets = clean_home / ".secrets"
    yield secrets
    if secrets.is_dir() and not secrets.is_symlink():
        secrets.chmod(0o700)
        for place in secrets.iterdir():
            if place.is_dir() and not place.is_symlink():
                place.chmod(0o700)


def put_a_file_in_place_of(secrets: Path) -> None:
    shutil.rmtree(secrets)
    secrets.write_text("not a folder")


def point_a_dangling_link_from(secrets: Path) -> None:
    shutil.rmtree(secrets)
    secrets.symlink_to(secrets.with_name("gone"))


NOT_AS_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")


@pytest.mark.parametrize(
    ("block", "rule", "place", "finding", "held"),
    [
        pytest.param(
            close_to(0, "cloudflared"),
            "closed-folder",
            "~/.secrets/cloudflared",
            ["hat Rechte 0, der Besitzer braucht 700", "`chmod 700 ~/.secrets/cloudflared`"],
            "unbekannt",
            id="closed-subfolder",
            marks=NOT_AS_ROOT,
        ),
        pytest.param(
            close_to(0o600, "."),
            "closed-folder",
            "~/.secrets",
            ["hat Rechte 600, der Besitzer braucht 700", "`chmod 700 ~/.secrets`"],
            "unbekannt",
            id="closed-secrets-folder",
            marks=NOT_AS_ROOT,
        ),
        pytest.param(
            put_a_file_in_place_of,
            "not-a-folder",
            "~/.secrets",
            ["ist kein Ordner", "`mv -n ~/.secrets ~/.secrets.alt`"],
            "unbekannt",
            id="file-in-place-of-secrets",
        ),
        pytest.param(
            point_a_dangling_link_from,
            "symlinked-folder",
            "~/.secrets",
            ["ist ein Symlink", f"`{symlinked_folder_fix('~/.secrets')}`"],
            "fehlt",
            id="dangling-link-in-place-of-secrets",
        ),
    ],
)
def test_a_folder_on_the_way_that_cannot_hold_a_secret_is_a_finding_with_its_fix_and_nothing_below_it_shows_present(
    clean_home: Path,
    reopened_secrets: Path,
    probe: FakeProbe,
    capsys,
    block: Callable[[Path], None],
    rule: str,
    place: str,
    finding: list[str],
    held: str,
) -> None:
    """#37 line 7: a closed folder, or a file or link where ~/.secrets belongs, is named with its fix, never a dead end."""
    block(reopened_secrets)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    assert code == 1
    assert findings_of(output, rule) == {place: finding}
    assert rows_of(output)["cloudflared"][2] == held


def test_by_service_inverts_read_by_under_a_declared_dependencies_heading(clean_home: Path, probe: FakeProbe, capsys) -> None:
    code = home_secrets(clean_home, probe, "--by-service")

    output = capsys.readouterr().out
    assert code == 0
    assert output.splitlines()[0] == "# home secrets --by-service | deklarierte Abhängigkeiten"
    assert "| .bashrc | gitlab, cloudflared |" in output
    assert "Abgeleitete Kopien" not in output
    assert probe.calls == []
    assert not Layout(clean_home).state_dir.exists()


def test_the_packaged_manifest_names_every_inventory_entry_without_a_value() -> None:
    manifest = load_manifest(PACKAGED_MANIFEST)

    assert {entry.name for entry in manifest} == INVENTORY
    assert {entry.name for entry in manifest if entry.retired} == {"github/token", "github/overnightworks-read-token"}
    assert {entry.name for entry in manifest if entry.verifier} == {"gh"}


def test_the_packaged_manifest_catches_up_with_the_first_live_run() -> None:
    """#21 points 1-4: confirmed revocations, the known non-secret files, the cloudflared layout; ppk and non-secret files are optional."""
    manifest = {entry.name: entry for entry in load_manifest(PACKAGED_MANIFEST)}

    retired = {name: entry.revocation_confirmed for name, entry in manifest.items() if entry.retired}
    assert retired == dict.fromkeys(("github/token", "github/overnightworks-read-token"), date(2026, 10, 15))
    assert {name: entry.kind for name, entry in manifest.items() if entry.kind in (SecretKind.CONFIGURATION, SecretKind.DOCUMENTATION)} == {
        "claudebot-runtime.env": SecretKind.CONFIGURATION,
        "opentofu/state": SecretKind.CONFIGURATION,
        "opentofu/state-backup": SecretKind.CONFIGURATION,
        "opentofu/plans": SecretKind.CONFIGURATION,
        "opentofu/tmp": SecretKind.CONFIGURATION,
        "secrets/README": SecretKind.DOCUMENTATION,
        "step-ca/root_ca.crt": SecretKind.CONFIGURATION,
        "step-ca/pending-root/root_ca.crt": SecretKind.CONFIGURATION,
    }
    assert manifest["cloudflared"].paths == (PurePosixPath(".secrets/cloudflared/cert.pem"),)
    assert "/etc/cloudflared (root)" in manifest["cloudflared"].elsewhere
    assert {name for name, entry in manifest.items() if entry.optional} == {
        "ssh/id_ed25519.ppk",
        "claudebot-runtime.env",
        "secrets/README",
        "claude-measure-token",
        "opentofu/state",
        "opentofu/state-backup",
        "opentofu/plans",
        "opentofu/tmp",
        "step-ca/root_ca_key",
        "step-ca/password",
        "step-ca/root_ca.crt",
        "step-ca/pending-root/root_ca_key",
        "step-ca/pending-root/root_ca.crt",
        "keycloak/db-password",
        "keycloak/config-cli-secret",
        "keycloak/bootstrap-admin-password",
        "keycloak/clients/*",
    }


def test_the_packaged_manifest_puts_the_five_edge_external_entries_first_each_with_its_place_and_rights() -> None:
    """#36 rule 1 / rule 2: the five edge external entries come first, each with its place, and every token names its rights."""
    manifest = load_manifest(PACKAGED_MANIFEST)

    first = manifest[: len(EDGE_EXTERNAL_ENTRIES)]
    assert tuple(entry.name for entry in first) == EDGE_EXTERNAL_ENTRIES
    assert [f"~/{entry.place}" for entry in first] == [f"~/.secrets/{name}" for name in EDGE_EXTERNAL_ENTRIES]
    tokens = [entry for entry in first if entry.kind is SecretKind.TOKEN]
    assert [(entry.name, entry.source.provider.value) for entry in tokens] == [
        ("cloudflare/fleet-read", "cloudflare"),
        ("github/fleet-read", "github"),
        ("cloudflare/fleet-write", "cloudflare"),
        ("github/fleet-admin", "github"),
    ]
    assert all(entry.source.scope and entry.purpose for entry in tokens)
    assert {entry.name: entry.read_by for entry in tokens if entry.source.provider.value == "github"} == {
        "github/fleet-read": (),
        "github/fleet-admin": (),
    }
    assert {entry.name: entry.kind for entry in first if entry.source is None} == {"opentofu/state-passphrase": SecretKind.SERVICE_PASSWORD}


@pytest.mark.parametrize(
    ("state_mode", "loose_findings"),
    [
        (0o600, {}),
        (0o664, {"~/.secrets/opentofu/fleet.tfstate": ["hat Rechte 664, höchstens 600", "`chmod 600 ~/.secrets/opentofu/fleet.tfstate`"]}),
    ],
    ids=["owner-only state", "loose state is still reported"],
)
def test_the_files_edge_external_leaves_under_opentofu_are_known_and_only_a_loose_mode_is_a_finding(
    tmp_path: Path, probe: FakeProbe, capsys, state_mode: int, loose_findings: dict[str, list[str]]
) -> None:
    """#159 T2/T3: state, its backup, plans and tmp are declared; the loose-mode check still fires."""
    home = tmp_path / "home"
    plant_secrets(home)
    opentofu = home / ".secrets" / "opentofu"
    for directory in ("plans", "tmp"):
        (opentofu / directory).unlink()
        (opentofu / directory).mkdir(mode=0o700)
    for name, mode in (("fleet.tfstate", state_mode), ("fleet.tfstate.backup", 0o600)):
        (opentofu / name).write_text("encrypted state")
        (opentofu / name).chmod(mode)
    (opentofu / "plans" / "fleet.tfplan").write_text("encrypted plan")
    (opentofu / "plans" / "fleet.tfplan").chmod(0o600)

    code = home_secrets(home, probe)

    output = capsys.readouterr().out
    assert findings_of(output, "unknown-file") == {}
    assert findings_of(output, "loose-mode") == loose_findings
    assert code == (1 if loose_findings else 0)


def test_a_stray_copy_of_the_state_beside_it_is_still_an_unknown_file(tmp_path: Path, probe: FakeProbe, capsys) -> None:
    """#159 T4: the state's files are named exactly, so a plaintext `tofu state pull` copy does not hide."""
    home = tmp_path / "home"
    plant_secrets(home)
    stray = home / ".secrets" / "opentofu" / "fleet.tfstate.json"
    stray.write_text("plaintext state")
    stray.chmod(0o600)

    home_secrets(home, probe)

    assert set(findings_of(capsys.readouterr().out, "unknown-file")) == {"~/.secrets/opentofu/fleet.tfstate.json"}


@pytest.mark.parametrize(
    "expected",
    [
        SecretEntry(
            name="restic/password",
            kind=SecretKind.HUMAN_PASSWORD,
            paths=(PurePosixPath(".secrets/restic/password"),),
            read_by=("edge-backup.service", "edge-backup-restore-test.service"),
            recreate=RESTIC_RECREATE,
            purpose="edge-backup verschlüsselt damit die restic-Backups; ohne es sind sie unlesbar.",
        ),
        SecretEntry(
            name="cloudflared/tunnel",
            kind=SecretKind.TOOL_LOGIN,
            paths=(PurePosixPath(".secrets/cloudflared/a903516a-2660-4507-90a5-5a27d20c82c4.json"),),
            read_by=("cloudflared tunnel run (von Hand; credentials-file in /etc/cloudflared/config.yml)",),
            recreate="`cloudflared tunnel create` (neuer Tunnel) oder die Datei aus /etc/cloudflared kopieren",
            elsewhere="/etc/cloudflared (root), die Kopie, die der laufende Tunnel über LoadCredential liest",
        ),
        SecretEntry(
            name="claude-measure-token",
            kind=SecretKind.TOKEN,
            paths=(PurePosixPath(".secrets/claude-measure-token"),),
            read_by=("agent-providers#115, Messläufe 0b2/0e",),
            recreate="`claude setup-token` im Browser",
            purpose="Die Messläufe starten Claude damit unter einem eigenen, widerrufbaren Login.",
            optional=True,
        ),
    ],
    ids=lambda entry: entry.name,
)
def test_the_packaged_manifest_carries_the_ruled_contract_of_each_issue_42_entry(expected: SecretEntry) -> None:
    """#42 rules 1-3: each new entry carries exactly its ruled kind, path, readers, recreate line and notes."""
    manifest = {entry.name: entry for entry in load_manifest(PACKAGED_MANIFEST)}

    assert manifest[expected.name] == expected


def test_the_packaged_manifest_lists_every_entry_and_its_mirror(tmp_path: Path, probe: FakeProbe, capsys) -> None:
    """#42 M1/M4: every planted file is known, none is an unknown file, and edge-backup.service lists restic/password."""
    home = tmp_path / "home"
    plant_secrets(home)

    home_secrets(home, probe)
    home_secrets(home, probe, "--by-service")

    view, by_service = capsys.readouterr().out.split("# home secrets --by-service")
    assert set(rows_of(view)) == INVENTORY
    assert findings_of(view, "unknown-file") == {}
    assert rows_of(view)["github/token"][-1] == "Widerruf: https://github.com/settings/tokens"
    assert "| ssh/id_ed25519 | ssh/id_ed25519.ppk |" in by_service
    assert "| edge-backup.service | restic/password |" in by_service


def never_asked(*_: object) -> object:
    raise AssertionError("--setup asked the operator although nothing is set up by it")


def test_a_missing_restic_password_is_named_with_its_recreate_line_and_setup_never_generates_it(
    tmp_path: Path, probe: FakeProbe, capsys
) -> None:
    """#42 rule 1: losing the restic password makes every backup unreadable, so `--setup` leaves it by hand."""
    home = tmp_path / "home"
    plant_secrets(home)
    password = home / ".secrets" / "restic" / "password"
    password.unlink()
    operator_absent = Boundaries(Terminal(lambda: True, never_asked, never_asked, never_asked), never_asked, os.fsync)

    view_code = home_secrets(home, probe)
    view = capsys.readouterr().out
    setup_code = cli.main(["secrets", "--setup"], home=home, probe=probe, now=NOW, secrets_setup_boundaries=operator_absent)
    setup = capsys.readouterr().out.splitlines()

    assert view_code == 1
    assert findings_of(view, "missing") == {"restic/password": ["-", "zurückholen, siehe Tabelle"]}
    assert rows_of(view)["restic/password"][-1] == RESTIC_RECREATE
    assert setup_code == 1
    assert f"Dafür gibt es noch keinen Assistenten; von Hand: {RESTIC_RECREATE}" in setup
    assert BY_HAND_STEP + "restic/password" in setup
    assert not password.exists()


@pytest.mark.parametrize(
    ("kind", "generated"),
    [
        ("human-password", False),
        ("service-password", True),
        ("tool-login", False),
        ("token", False),
    ],
)
def test_setup_generates_only_a_service_password_and_never_a_human_one(tmp_path: Path, kind: str, *, generated: bool) -> None:
    """#42 rule 1: a human-password entry is never written by `--setup`, so a lost one is never silently replaced."""
    manifest = tmp_path / "secrets_manifest.toml"
    manifest.write_text(ENTRY.replace('kind = "token"', f'kind = "{kind}"'))

    (entry,) = load_manifest(manifest)

    assert entry.written_by_setup is generated


def test_an_invalid_manifest_exits_2_naming_the_file(use_manifest, clean_home: Path, probe: FakeProbe, capsys) -> None:
    manifest = use_manifest(TEST_MANIFEST + 'value = "x"\n')

    code = home_secrets(clean_home, probe)

    assert code == 2
    assert capsys.readouterr().err.startswith(f"home: {manifest}: ")


ENTRY = '[[secret]]\nname = "e"\nkind = "token"\npaths = ["~/.secrets/e"]\nread_by = []\nrecreate = "r"\n'
SOURCE = 'provider = "github"\nresource = "o"\nscope = ["metadata:read"]\nexpires_in_days = 366\n'


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("[[secret]\n", "secrets_manifest.toml"),
        ("other = 1\n", "other"),
        ("secret = 1\n", "secret"),
        ("secret = [1]\n", "[[secret]] #1"),
        ('[[secret]]\nname = "e"\n', "kind"),
        (ENTRY + 'value = "hunter2"\n', "value"),
        (ENTRY.replace('"token"', '"password"'), "kind"),
        (ENTRY.replace('recreate = "r"', 'recreate = ["hunter2"]'), "recreate"),
        (ENTRY.replace('name = "e"', "name = 1"), "[[secret]] #1"),
        (ENTRY.replace('recreate = "r"', 'recreate = " "'), "recreate"),
        (ENTRY.replace("read_by = []", "read_by = [1]"), "read_by"),
        (ENTRY.replace('["~/.secrets/e"]', "[]"), "paths"),
        (ENTRY.replace("~/.secrets/e", "/etc/e"), "paths"),
        (ENTRY.replace("~/.secrets/e", "~/../e"), "paths"),
        (ENTRY.replace("~/.secrets/e", "~//etc/e"), "paths"),
        (ENTRY.replace("~/.secrets/e", "~/.secrets/*/e"), "paths"),
        (ENTRY + 'verify = "curl"\n', "verify"),
        (ENTRY.replace('"token"', '"derived-mirror"'), "mirror_of"),
        (ENTRY + 'mirror_of = "e"\n', "mirror_of"),
        (ENTRY + "retired_on = 2026-10-14\n", "revoke_url"),
        (ENTRY + 'retired_on = "yesterday"\nrevoke_url = "https://x"\n', "retired_on"),
        (ENTRY + 'retired_on = 2026-10-14T10:00:00\nrevoke_url = "https://x"\n', "retired_on"),
        (ENTRY + 'retired_on = 2026-10-14\nrevoke_url = "http://x"\n', "revoke_url"),
        # #21 points 1 and 4: a confirmed revocation is a date on a retired entry; optional is true or false.
        (ENTRY + "revocation_confirmed = 2026-10-15\n", "retired entry only"),
        (ENTRY + 'retired_on = 2026-10-14\nrevoke_url = "https://x"\nrevocation_confirmed = "today"\n', "revocation_confirmed"),
        (ENTRY + 'optional = "yes"\n', "optional"),
        (ENTRY + 'elsewhere = " "\n', "elsewhere"),
        # #195 M4: a declared mode comes with its reason and is never looser than 0644.
        (ENTRY + 'loosest_mode = 0o666\nloosest_mode_reason = "r"\n', "loosest_mode"),
        (ENTRY + 'loosest_mode = 0o755\nloosest_mode_reason = "r"\n', "loosest_mode"),
        (ENTRY + 'loosest_mode = "0644"\nloosest_mode_reason = "r"\n', "loosest_mode"),
        (ENTRY + 'loosest_mode = true\nloosest_mode_reason = "r"\n', "loosest_mode"),
        (ENTRY + 'loosest_mode = 0\nloosest_mode_reason = "r"\n', "loosest_mode"),
        (ENTRY + "loosest_mode = 0o644\n", "belong together"),
        (ENTRY + 'loosest_mode_reason = "r"\n', "belong together"),
        (ENTRY + ENTRY, "e"),
        (ENTRY.replace('"token"', '"derived-mirror"') + 'mirror_of = "gone"\n', "gone"),
        # #36 rule 1 / rule 2.2: a token the wizard guides names its provider, resource, rights and expiry, and one place.
        (ENTRY + 'provider = "github"\n', "provider"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace("github", "gitlab"), "provider"),
        (ENTRY.replace('"token"', '"service-password"') + 'purpose = "p"\n' + SOURCE, "token entry only"),
        (ENTRY + SOURCE, "purpose"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace('"metadata:read"', '"metadata"'), "scope"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace('"metadata:read"', '"metadata:own"'), "scope"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace('["metadata:read"]', "[]"), "scope"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace("366", "367"), "expires_in_days"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace("366", "0"), "expires_in_days"),
        (ENTRY + 'purpose = "p"\n' + SOURCE.replace("366", "true"), "expires_in_days"),
        (ENTRY.replace('["~/.secrets/e"]', '["~/.secrets/e", "~/.secrets/f"]') + 'purpose = "p"\n' + SOURCE, "exactly one place"),
        (ENTRY.replace('"token"', '"service-password"').replace("~/.secrets/e", "~/.secrets/*.key"), "exactly one place"),
    ],
)
def test_an_invalid_manifest_is_refused_naming_the_file_and_the_key(tmp_path: Path, text: str, named: str) -> None:
    path = tmp_path / "secrets_manifest.toml"
    path.write_text(text)

    with pytest.raises(ManifestError) as refused:
        load_manifest(path)

    assert str(refused.value).startswith(f"{path}: ")
    assert named in str(refused.value)
    assert "hunter2" not in str(refused.value)


@pytest.mark.parametrize(
    ("pattern", "matched"),
    [
        ("one/file", ["one/file"]),
        ("one/*.key", ["one/a.key", "one/b.key"]),
        ("one/*.nothing", []),
        ("absent/file", []),
        ("absent/*", []),
        ("one/file/x", None),
        ("one/file/*", None),
    ],
)
def test_stat_matches_returns_every_named_path_as_one_tuple_python_S8495(tmp_path: Path, pattern: str, matched: list[str] | None) -> None:
    (tmp_path / "one").mkdir()
    for name in ("file", "a.key", "b.key", "c.txt"):
        (tmp_path / "one" / name).write_text("x")
    layout = Layout(tmp_path)

    facts = secrets_view.stat_matches(layout, PurePosixPath(pattern))

    assert (None if facts is None else [str(fact.relative) for fact in facts]) == matched


def test_a_missing_manifest_is_refused_naming_the_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="secrets_manifest.toml"):
        load_manifest(tmp_path / "secrets_manifest.toml")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([sys.executable, "-c", "print('ghp_0000notarealtoken')"], 0),
        ([sys.executable, "-c", "import sys; sys.stderr.write('glpat-0000'); raise SystemExit(4)"], 4),
        (["home-no-such-tool"], None),
    ],
    ids=["a probe that passes", "a probe that fails", "a probe that is not installed"],
)
def test_a_probe_hands_back_only_its_exit_code(capfd, argv: list[str], expected: int | None) -> None:
    assert secrets_view.probe_exit_code(argv) == expected
    assert capfd.readouterr() == ("", "")


@pytest.fixture
def probe_family(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """A FIFO every process of a probe's family holds open for writing: its reader sees EOF only once all of them are gone."""
    held_open_while_alive = tmp_path / "probe-family"
    os.mkfifo(held_open_while_alive)
    reader = os.open(held_open_while_alive, os.O_RDONLY | os.O_NONBLOCK)
    yield held_open_while_alive, reader
    os.close(reader)


def wait_until_the_family_started(reader: int) -> None:
    family_started, _, _ = select.select([reader], [], [], 10)
    assert family_started, "the run never started its probe"
    first_byte = os.read(reader, 1)
    assert first_byte == b"s", "the probe's first byte was not its start marker"


def assert_every_family_member_gone(reader: int) -> None:
    every_writer_gone, _, _ = select.select([reader], [], [], 10)
    assert every_writer_gone, "a process the probe started outlived it"
    assert os.read(reader, 1) == b""


@pytest.mark.parametrize(
    "family",
    ['exec 3>"$1"; sleep 60 & exec sleep 30', 'trap "" TERM; exec 3>"$1"; sleep 60 & exec sleep 30'],
    ids=["a family that dies on SIGTERM", "a family that ignores SIGTERM"],
)
def test_a_probe_that_hangs_is_killed_with_everything_it_started_and_reports_nothing(probe_family: tuple[Path, int], family: str) -> None:
    held_open_while_alive, reader = probe_family
    probe_with_background_child = ["sh", "-c", family, "sh", str(held_open_while_alive)]

    assert secrets_view.probe_exit_code(probe_with_background_child, timeout=1) is None

    assert_every_family_member_gone(reader)


def a_stop_as_the_probe_is_started(monkeypatch: pytest.MonkeyPatch, reader: int) -> None:
    """The stop lands once the probe's family runs but before probe_exit_code holds the probe, where it would orphan it."""
    start = subprocess.Popen

    def start_then_stop(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        probe = start(*args, **kwargs)
        wait_until_the_family_started(reader)
        os.kill(os.getpid(), signal.SIGTERM)
        return probe

    monkeypatch.setattr(subprocess, "Popen", start_then_stop)


def no_stop_from_outside(_monkeypatch: pytest.MonkeyPatch, _reader: int) -> None:
    pass


@pytest.mark.parametrize(
    ("stop_from_outside", "family", "stopped"),
    [
        (a_stop_as_the_probe_is_started, "sleep 60 & printf s >&3; exec sleep 30", SystemExit(128 + signal.SIGTERM)),
        (no_stop_from_outside, "sleep 60 & kill -INT $PPID; exec sleep 30", KeyboardInterrupt()),
        (
            no_stop_from_outside,
            'sleep 60 & trap "kill -TERM $PPID" TERM; while kill -0 $! 2>/dev/null; do wait $!; done',
            SystemExit(128 + signal.SIGTERM),
        ),
    ],
    ids=["a stop while the probe is started", "Ctrl-C while the probe runs", "a stop while the hung probe is killed"],
)
def test_a_stop_ends_the_run_with_its_exit_status_once_the_probe_and_everything_it_started_are_dead(
    monkeypatch: pytest.MonkeyPatch,
    probe_family: tuple[Path, int],
    stop_from_outside: Callable[[pytest.MonkeyPatch, int], None],
    family: str,
    stopped: BaseException,
) -> None:
    held_open_while_alive, reader = probe_family
    stop_from_outside(monkeypatch, reader)

    with pytest.raises(type(stopped)) as run_end:
        secrets_view.probe_exit_code(["sh", "-c", f'exec 3>"$1"; {family}', "sh", str(held_open_while_alive)], timeout=1)

    assert run_end.value.args == stopped.args
    assert_every_family_member_gone(reader)


def test_a_stop_the_caller_ignores_neither_ends_the_run_nor_is_unignored() -> None:
    caller_handler = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert secrets_view.probe_exit_code(["sh", "-c", "kill -HUP $PPID"]) == 0
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, caller_handler)


A_HOME_RUN_FROM_A_TERMINAL = (
    "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); from home_tools.cli import main; sys.exit(main())"
)


@pytest.mark.parametrize(
    "family",
    [
        "sleep 60 & printf s >&3; exec sleep 30",
        'trap "" TERM; sleep 60 & trap "kill -{stop} $PPID" TERM; printf s >&3; while kill -0 $! 2>/dev/null; do wait $!; done',
    ],
    ids=["a family that dies on SIGTERM", "a family that ignores SIGTERM and stops the run again while it is killed"],
)
@pytest.mark.parametrize(
    ("stop", "exit_status"),
    [(signal.SIGINT, -signal.SIGINT), (signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGHUP, 128 + signal.SIGHUP)],
    ids=["Ctrl-C", "a timeout wrapper", "a closed terminal"],
)
def test_a_stopped_run_takes_its_probe_and_everything_it_started_along(
    tmp_path: Path, probe_family: tuple[Path, int], stop: signal.Signals, exit_status: int, family: str
) -> None:
    held_open_while_alive, reader = probe_family
    tools = tmp_path / "bin"
    tools.mkdir()
    hanging_gh = tools / "gh"
    hanging_gh.write_text(f'#!/bin/sh\nexec 3>"{held_open_while_alive}"; {family.format(stop=stop.value)}\n')
    hanging_gh.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home), "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
    environment["PYTHONPATH"] = str(Path(secrets_view.__file__).parents[1])
    run = subprocess.Popen(
        [sys.executable, "-c", A_HOME_RUN_FROM_A_TERMINAL, "secrets"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_until_the_family_started(reader)

        os.killpg(run.pid, stop)  # a terminal or `timeout` signals the run's process group, never the probe's own session

        assert run.wait(timeout=10) == exit_status
        assert_every_family_member_gone(reader)
    finally:
        if run.poll() is None:
            os.killpg(run.pid, signal.SIGKILL)
            run.wait()
