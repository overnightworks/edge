from __future__ import annotations

import os
import select
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FAKE_SECRET_VALUE, TEST_MANIFEST, FakeProbe, plant_secrets

from home_tools import cli, secrets_view
from home_tools.secrets_view import ManifestError, load_manifest
from home_tools.tidy_home import Layout

NOW = datetime(2026, 10, 15, 9, 0)
PACKAGED_MANIFEST = secrets_view.MANIFEST_PATH
INVENTORY = {
    "github/token",
    "gitlab",
    "sonarcloud/token",
    "claudebot.env",
    "cloudflared",
    "gmail-cleanup",
    "ssh/id_ed25519",
    "ssh/id_ed25519.ppk",
    "gh",
    "claude",
    "codex",
    "songmaker",
    "github/overnightworks-read-token",
}
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
    assert all(recommendation.startswith("unbekannt") for _detail, recommendation in unknown.values())
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
    assert {subject: detail for subject, (detail, _recommendation) in loose.items()} == {
        "~/.secrets/gitlab": "644, höchstens 600",
        "~/.secrets/cloudflared": "755, höchstens 700",
    }
    assert (stat.S_IMODE(token.stat().st_mode), stat.S_IMODE(directory.stat().st_mode)) == (0o644, 0o755)


def test_a_retired_entry_shows_its_revoke_link_and_unconfirmed_revocation(use_manifest, clean_home: Path, probe: FakeProbe, capsys) -> None:
    use_manifest(TEST_MANIFEST + RETIRED_ENTRY)

    code = home_secrets(clean_home, probe)

    output = capsys.readouterr().out
    retired = rows_of(output)["github/token"]
    assert code == 1
    assert output.splitlines()[0].endswith("| 1 Fund")
    assert retired[2:5] == ["zurückgezogen", "Widerruf unbestätigt", "Ablauf unbekannt"]
    assert retired[-1] == "Widerruf: https://github.com/settings/tokens"
    assert list(findings_of(output, "revocation-unconfirmed")) == ["github/token"]
    assert "github/token" not in findings_of(output, "missing")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_directory_under_secrets_that_cannot_be_listed_breaks_the_run_with_exit_3(clean_home: Path, probe: FakeProbe, capsys) -> None:
    locked = clean_home / ".secrets" / "cloudflared"
    locked.chmod(0)
    try:
        code = home_secrets(clean_home, probe)
    finally:
        locked.chmod(0o700)

    assert code == 3
    assert capsys.readouterr().err.startswith("home: unerwarteter Fehler (PermissionError)")


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


def test_the_packaged_manifest_lists_every_entry_and_its_mirror(tmp_path: Path, probe: FakeProbe, capsys) -> None:
    home = tmp_path / "home"
    plant_secrets(home)

    home_secrets(home, probe)
    home_secrets(home, probe, "--by-service")

    view, by_service = capsys.readouterr().out.split("# home secrets --by-service")
    assert set(rows_of(view)) == INVENTORY
    assert rows_of(view)["github/token"][-1] == "Widerruf: https://github.com/settings/tokens"
    assert "| ssh/id_ed25519 | ssh/id_ed25519.ppk |" in by_service


def test_an_invalid_manifest_exits_2_naming_the_file(use_manifest, clean_home: Path, probe: FakeProbe, capsys) -> None:
    manifest = use_manifest(TEST_MANIFEST + 'value = "x"\n')

    code = home_secrets(clean_home, probe)

    assert code == 2
    assert capsys.readouterr().err.startswith(f"home: {manifest}: ")


ENTRY = '[[secret]]\nname = "e"\nkind = "token"\npaths = ["~/.secrets/e"]\nread_by = []\nrecreate = "r"\n'


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
        (ENTRY + ENTRY, "e"),
        (ENTRY.replace('"token"', '"derived-mirror"') + 'mirror_of = "gone"\n', "gone"),
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
    assert family_started and os.read(reader, 1) == b"s", "the run never started its probe"


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
