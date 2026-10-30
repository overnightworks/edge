"""`hal secrets` through `hal.cli.main`: the rows it prints for its findings and for everything
healthy, and the aligned table of `-v`."""

import json
import os
import re
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from support.terminal_support import put_stdout_on_a_terminal
from support.workstation_secrets_report import report_of
from workstation_support import (
    FAKE_SECRET_VALUE,
    TEST_MANIFEST,
    FakeProbe,
    plant_secrets,
    run_hal_secrets,
)

from hal.workstation import secrets_view
from hal.workstation.layout import Layout

NOW = datetime(2026, 10, 15, 9, 0)
HINT = "hal secrets -v lists every entry"
HEALTHY = "  ✓ {count} entries | present | gh checked: valid; the rest not checked"
FIRST_RETIRED = """
[[secret]]
name = "github/token"
kind = "token"
paths = ["~/.secrets/github/token"]
read_by = []
recreate = "do not recreate"
retired_on = 2026-10-14
revoke_url = "https://github.com/settings/tokens"
"""


def entry(name: str, *, kind: str = "token", recreate: str = "r", optional: bool = False) -> str:
    fields = {
        "name": name,
        "kind": kind,
        "paths": [f"~/.secrets/{name}"],
        "read_by": [],
        "recreate": recreate,
        "optional": optional,
    }
    return "\n[[secret]]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in fields.items()
    )


def shown(output: str) -> list[str]:
    """The output with the padding between columns collapsed to ` | `, so a test states words, not
    widths (`hal.output` owns the widths)."""
    return [re.sub(r"(?<=\S) {2,}(?=\S)", " | ", line) for line in output.splitlines()]


@pytest.fixture
def add_entries(clean_home: Path, use_manifest: Callable[[str], Path]) -> Callable[..., Path]:
    def add(*entries: str) -> Path:
        use_manifest(TEST_MANIFEST + "".join(entries))
        plant_secrets(clean_home)
        return clean_home

    return add


@pytest.fixture
def run(clean_home: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]):
    def hal_secrets(*options: str) -> tuple[int, list[str]]:
        code = run_hal_secrets(clean_home, probe, *options, now=NOW)
        return code, shown(capsys.readouterr().out)

    return hal_secrets


def stray(home: Path, relative: str, mode: int = 0o600) -> None:
    path = home / ".secrets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_SECRET_VALUE)
    path.chmod(mode)


def test_a_complete_home_prints_one_healthy_row_and_a_verdict_that_counts_every_entry(run) -> None:
    code, lines = run()

    assert code == 0
    assert lines == [HEALTHY.format(count=3), f"✓ no findings · 3 entries · {HINT}"]


def test_a_loose_file_is_a_row_with_its_command_bare_under_it(clean_home: Path, run) -> None:
    (clean_home / ".secrets" / "gitlab").chmod(0o644)

    code, lines = run()

    assert code == 1
    assert lines == [
        "  ! gitlab | too open | has mode 644; at most 600 allowed",
        "    chmod 600 ~/.secrets/gitlab",
        HEALTHY.format(count=3),
        f"! 1 finding · {HINT}",
    ]


def test_a_missing_entry_the_wizard_writes_points_at_hal_setup(add_entries, run) -> None:
    home = add_entries(entry("svc/password", kind="service-password"))
    (home / ".secrets" / "svc" / "password").unlink()

    code, lines = run()

    assert code == 1
    assert lines[:2] == ["  ! svc/password | missing | service password", "    hal setup"]


def test_a_missing_human_password_shows_its_renewal_and_never_hal_setup(add_entries, run) -> None:
    home = add_entries(entry("bank/pin", kind="human-password", recreate="Ask the bank"))
    (home / ".secrets" / "bank" / "pin").unlink()

    code, lines = run()

    assert code == 1
    assert lines[:2] == ["  ! bank/pin | missing | human password", "    Ask the bank"]
    assert "    hal setup" not in lines


def test_a_missing_optional_entry_is_a_dim_row_and_no_finding(add_entries, run) -> None:
    home = add_entries(entry("opt/key", optional=True))
    (home / ".secrets" / "opt" / "key").unlink()

    code, lines = run()

    assert code == 0
    assert lines == [
        HEALTHY.format(count=3),
        "  · 1 optional | not set up | opt/key",
        f"✓ no findings · 4 entries · {HINT}",
    ]


def test_a_retired_entry_with_a_confirmed_revocation_is_a_dim_row(add_entries, run) -> None:
    add_entries(FIRST_RETIRED + "revocation_confirmed = 2026-10-15\n")

    code, lines = run()

    assert code == 0
    assert "  · 1 retired | revoked | github/token" in lines


def test_a_retired_entry_without_a_confirmed_revocation_shows_where_to_revoke(
    add_entries, run
) -> None:
    add_entries(FIRST_RETIRED)

    code, lines = run()

    assert code == 1
    assert lines[:2] == [
        "  ! github/token | retired | revocation not confirmed",
        "    revoke: https://github.com/settings/tokens",
    ]


@pytest.mark.parametrize(
    ("probe_code", "row", "step"),
    [
        (1, "  ! gh | invalid | gh auth status failed", "    gh auth login --web"),
        (None, "  ! gh | not checked | gh auth status hung or is missing", None),
    ],
    ids=["the check fails", "the check hangs"],
)
def test_a_failed_check_names_the_login_to_repeat_and_a_hanging_one_names_no_step(
    probe: FakeProbe, run, probe_code: int | None, row: str, step: str | None
) -> None:
    probe.code = probe_code

    code, lines = run()

    assert code == 1
    assert lines[0] == row
    assert lines[1] == (step or "  ✓ 2 entries | present | not checked")


def test_an_entry_whose_folder_cannot_be_read_is_not_checked_and_carries_no_step(
    clean_home: Path, run
) -> None:
    folder = clean_home / ".secrets" / "cloudflared"
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink()
    folder.rmdir()
    folder.write_text("not a folder")
    folder.chmod(0o600)

    code, lines = run()

    assert code == 1
    assert lines == [
        "  ! cloudflared | not checked | its folder cannot be read",
        "  ! cloudflared | not a folder | is not a folder",
        "    mv -n ~/.secrets/cloudflared ~/.secrets/cloudflared.alt",
        HEALTHY.format(count=2),
        f"! 2 findings · {HINT}",
    ]


def test_five_findings_are_five_rows_each_with_its_step(clean_home: Path, run) -> None:
    for number in range(1, 6):
        stray(clean_home, f"stray-{number}")

    code, lines = run()

    assert code == 1
    assert lines[:10] == [
        line
        for number in range(1, 6)
        for line in (
            f"  ! stray-{number} | unknown | not in the manifest",
            f"    add ~/.secrets/stray-{number} to the manifest or delete it",
        )
    ]
    assert lines[-1] == f"! 5 findings · {HINT}"


def crowd_a_login_folder(home: Path) -> None:
    """Six unknown paths and two folders left open, below a folder the manifest names a file in."""
    for folder in ("vault/.cache", "vault/.cache/fontconfig"):
        (home / ".secrets" / folder).mkdir(parents=True, exist_ok=True)
        (home / ".secrets" / folder).chmod(0o755)
    for number in range(1, 5):
        stray(home, f"vault/.cache/file-{number}")


def test_six_findings_in_one_folder_share_a_row_per_rule_and_carry_no_step(
    add_entries, run
) -> None:
    crowd = entry("vault/token")
    crowd_a_login_folder(add_entries(crowd))

    code, lines = run()

    assert code == 1
    assert lines == [
        "  ! vault | unknown | 6 paths · not in the manifest",
        "  ! vault | too open | .cache, .cache/fontconfig · has mode 755; at most 700 allowed",
        HEALTHY.format(count=4),
        f"! 8 findings · {HINT}",
    ]


def test_a_tree_finding_alone_in_its_group_is_its_own_row_with_its_step_among_grouped_ones(
    add_entries, run
) -> None:
    home = add_entries(entry("vault/token"))
    crowd_a_login_folder(home)
    shared = home / ".secrets" / "vault" / "shared"
    shared.mkdir()
    shared.chmod(0o775)

    code, lines = run()

    assert code == 1
    assert lines[:4] == [
        "  ! vault | unknown | 7 paths · not in the manifest",
        "  ! vault | too open | .cache, .cache/fontconfig · has mode 755; at most 700 allowed",
        "  ! vault/shared | too open | has mode 775; at most 700 allowed",
        "    chmod 700 ~/.secrets/vault/shared",
    ]


def test_an_entry_finding_stays_its_own_row_with_its_step_among_grouped_findings(
    add_entries, run
) -> None:
    home = add_entries(entry("vault/token"), entry("svc/password", kind="service-password"))
    crowd_a_login_folder(home)
    (home / ".secrets" / "svc" / "password").unlink()

    code, lines = run()

    assert code == 1
    assert lines[:4] == [
        "  ! svc/password | missing | service password",
        "    hal setup",
        "  ! vault | unknown | 6 paths · not in the manifest",
        "  ! vault | too open | .cache, .cache/fontconfig · has mode 755; at most 700 allowed",
    ]
    assert lines[-1] == f"! 9 findings · {HINT}"


def test_v_prints_an_aligned_table_of_every_entry_then_every_finding_without_a_hint(
    clean_home: Path, capsys: pytest.CaptureFixture[str], probe: FakeProbe
) -> None:
    (clean_home / ".secrets" / "gitlab").chmod(0o644)

    code = run_hal_secrets(clean_home, probe, "-v", now=NOW)

    raw = capsys.readouterr().out.splitlines()
    cells = [re.split(r" {2,}", line) for line in raw[:4]]
    assert code == 1
    assert cells == [
        ["name", "kind", "file", "check", "place", "renew"],
        [
            "gitlab",
            "token",
            "present",
            "not checked",
            "~/.secrets/gitlab",
            "https://gitlab.example/-/user_settings/personal_access_tokens",
        ],
        [
            "cloudflared",
            "tool login",
            "present",
            "not checked",
            "~/.secrets/cloudflared/*.json, ~/.secrets/cloudflared/cert.pem",
            "cloudflared tunnel login",
        ],
        ["gh", "tool login", "present", "checked: valid", "~/.config/gh", "gh auth login --web"],
    ]
    assert len({line.index(row[1]) for line, row in zip(raw[:4], cells, strict=True)}) == 1
    assert shown("\n".join(raw[4:])) == [
        "  ! gitlab | too open | has mode 644; at most 600 allowed",
        "    chmod 600 ~/.secrets/gitlab",
        "! 1 finding",
    ]


def test_v_shows_a_retired_and_an_optional_entry_as_table_rows_and_groups_nothing(
    add_entries, run
) -> None:
    home = add_entries(
        FIRST_RETIRED + "revocation_confirmed = 2026-10-15\n",
        entry("opt/key", optional=True),
    )
    (home / ".secrets" / "opt" / "key").unlink()
    for number in range(1, 7):
        stray(home, f"stray-{number}")

    _, lines = run("--verbose")

    assert lines[4:6] == [
        "github/token | token | retired | revocation confirmed 15.10.2026"
        " | ~/.secrets/github/token | revoke: https://github.com/settings/tokens",
        "opt/key | token | missing (optional) | not checked | ~/.secrets/opt/key | r",
    ]
    assert sum(line.startswith("  ! stray-") for line in lines) == 6
    assert lines[-1] == "! 6 findings"


def test_a_name_with_a_newline_stays_one_escaped_line(clean_home: Path, run) -> None:
    stray(clean_home, "weird\nna|me.txt")

    code, lines = run()

    assert code == 1
    assert lines[:2] == [
        "  ! weird\\nna|me.txt | unknown | not in the manifest",
        "    add ~/.secrets/weird\\nna|me.txt to the manifest or delete it",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.parametrize("options", [(), ("-v",)])
def test_no_part_of_a_secret_value_reaches_stdout_stderr_or_the_report(
    clean_home: Path, probe: FakeProbe, capfd: pytest.CaptureFixture[str], options: tuple[str, ...]
) -> None:
    files = [path for path in Layout(clean_home).secrets_dir.rglob("*") if path.is_file()]
    for path in files:
        path.chmod(0)

    code = run_hal_secrets(clean_home, probe, *options, now=NOW)

    captured = capfd.readouterr()
    everything = captured.out + captured.err + report_of(clean_home)
    assert code == 0
    assert not any(part in everything for part in FAKE_SECRET_VALUE.split())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0 for path in files)


@pytest.mark.parametrize("options", [(), ("-v",)])
def test_the_report_file_is_the_markdown_hal_check_reads(
    clean_home: Path, probe: FakeProbe, options: tuple[str, ...]
) -> None:
    run_hal_secrets(clean_home, probe, *options, now=NOW)

    _, report = secrets_view.inspect(Layout(clean_home), probe, NOW)
    assert report_of(clean_home) == report
    assert report.startswith("# hal secrets 15.10.2026 09:00 | ")


@pytest.mark.parametrize("options", [(), ("-v",)])
def test_an_invalid_manifest_prints_an_error_pair_and_no_row(
    use_manifest, clean_home: Path, probe: FakeProbe, capsys, options: tuple[str, ...]
) -> None:
    manifest = use_manifest(TEST_MANIFEST + 'value = "x"\n')

    code = run_hal_secrets(clean_home, probe, *options, now=NOW)

    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert captured.err.startswith(f"error: {manifest}: ")


@pytest.mark.parametrize(
    "environment",
    [{}, {"NO_COLOR": "1"}, {"TERM": "dumb"}],
    ids=["a pipe", "NO_COLOR", "TERM=dumb"],
)
def test_the_same_words_come_without_colour_off_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
    clean_home: Path,
    probe: FakeProbe,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
) -> None:
    (clean_home / ".secrets" / "gitlab").chmod(0o644)
    out = put_stdout_on_a_terminal(monkeypatch) if environment else None
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    run_hal_secrets(clean_home, probe, now=NOW)

    printed = out.getvalue() if out else capsys.readouterr().out
    assert "\x1b" not in printed
    assert shown(printed)[0] == "  ! gitlab | too open | has mode 644; at most 600 allowed"


class InterruptedProbe(FakeProbe):
    def __call__(self, argv) -> int | None:
        raise KeyboardInterrupt


def test_ctrl_c_during_the_probe_exits_130_without_a_traceback(
    clean_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_hal_secrets(clean_home, InterruptedProbe(), now=NOW)

    assert code == 130
    assert "Traceback" not in capsys.readouterr().err


def test_the_help_names_the_verbose_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_hal_secrets(Path("/nonexistent"), FakeProbe(), "--help")

    assert "--verbose" in capsys.readouterr().out
