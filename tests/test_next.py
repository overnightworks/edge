from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FAKE_SECRET_VALUE, HOME_README, TEST_MANIFEST, FakeProbe, RunnerWithRealGitAndQuality, Sandbox, symlinked_folder_fix

from home_tools import cli

NOW = datetime(2026, 10, 17, 9, 0)

# A secret the wizard generates and a retired token whose revocation nobody confirmed; neither is planted.
OPEN_ENTRIES = """
[[secret]]
name = "opentofu/passphrase"
kind = "service-password"
paths = ["~/.secrets/opentofu/passphrase"]
read_by = ["edge external plan"]
recreate = "home secrets --setup"

[[secret]]
name = "github/old"
kind = "token"
paths = ["~/.secrets/github/old"]
read_by = []
recreate = "nicht neu anlegen"
retired_on = 2026-10-14
revoke_url = "https://github.com/settings/tokens"
"""

# What the wizard says at the end of a walk that stores opentofu/passphrase: its stored line and its one `next:` line.
WIZARD_STEP = (
    "home secrets --setup (beliebiges Terminal) — erwartet: erzeugt und gespeichert: ~/.secrets/opentofu/passphrase (600);"
    " zuletzt next: `uv run edge external plan` in ~/git/edge (expected: No changes)"
)
NOT_AS_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
UNREADABLE_SECRETS = "1. konnte die Geheimnisse nicht lesen; den Fehler zeigt: home secrets"

SETUP_PENDING = (
    "1. home setup --apply (beliebiges Terminal) — erwartet: quality, Engines, Hook-Dateien, Aktivierung: geändert",
    "   warum: ohne den Hook prüft kein Commit auf Geheimnisse",
)


@pytest.fixture
def home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home inside the sandbox's HOME, so that `home setup` could install the hook there."""
    return furnish_clean_home(sandbox.home)


@pytest.fixture
def hook_installed(home: Path) -> None:
    assert cli.main(["setup", "--apply"], home=home) == 0


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


def home_next(home: Path, probe: FakeProbe, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    code = cli.main(["next"], home=home, run=RunnerWithRealGitAndQuality(), probe=probe, now=NOW)
    return code, capsys.readouterr().out.splitlines()


def files_under(home: Path) -> dict[str, int]:
    return {str(path.relative_to(home)): path.stat().st_mtime_ns for path in home.rglob("*") if not path.is_symlink()}


@pytest.mark.usefixtures("hook_installed")
def test_a_home_with_nothing_open_says_nichts_zu_tun_and_exits_0(home: Path, probe: FakeProbe, capsys) -> None:
    """#37 line 4: nothing to do is one line and exit 0."""
    assert home_next(home, probe, capsys) == (0, ["Nichts zu tun."])


@pytest.mark.usefixtures("hook_installed")
def test_every_open_step_names_its_command_where_to_run_it_and_what_to_expect_most_blocking_first(
    home: Path, use_manifest, probe: FakeProbe, leave_pasted_image, capsys
) -> None:
    """#37 lines 1, 2 and 4: missing secrets give the wizard and the by-hand page, then the failed login, the revocation and tidying."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    (home / ".secrets" / "gitlab").unlink()
    probe.code = 1
    leave_pasted_image(home)

    code, lines = home_next(home, probe, capsys)

    assert code == 0
    assert lines == [
        f"1. {WIZARD_STEP}",
        "2. https://gitlab.example/-/user_settings/personal_access_tokens (von Hand, für gitlab)"
        " — erwartet: home secrets zeigt gitlab vorhanden",
        "3. gh auth login --web (beliebiges Terminal) — erwartet: home secrets zeigt gh geprüft: gültig",
        "4. https://github.com/settings/tokens (Browser, github/old widerrufen) — erwartet: das Token steht dort nicht mehr",
        "5. home tidy --apply (beliebiges Terminal) — erwartet: Apply | 2 Funde",
    ]


@pytest.mark.usefixtures("hook_installed")
def test_the_tidy_step_expects_what_home_tidy_apply_really_prints(home: Path, probe: FakeProbe, leave_pasted_image, capsys) -> None:
    """#37 line 1: the expectation quotes the real output, so running the step as printed shows it."""
    leave_pasted_image(home)
    leave_pasted_image(home / "Bilder")
    (home / ".stray").mkdir()
    _code, lines = home_next(home, probe, capsys)
    tidy_step = next(line for line in lines if "home tidy --apply" in line)
    expected = tidy_step.split(" — erwartet: ", 1)[1]

    cli.main(["tidy", "--apply"], home=home, run=RunnerWithRealGitAndQuality(), now=NOW)

    assert expected in capsys.readouterr().out


@pytest.mark.usefixtures("hook_installed")
def test_a_home_entry_the_readme_does_not_know_is_a_step_by_hand(home: Path, probe: FakeProbe, capsys) -> None:
    """#37 line 2: a `home check` finding of `home tidy` names its fix, so it is a step, not silence."""
    (home / ".stray").mkdir()

    assert home_next(home, probe, capsys) == (
        0,
        [
            "1. ~/.stray: in ~/README.md eintragen oder löschen (von Hand)"
            " — erwartet: home check meldet unknown-home-entry für ~/.stray nicht mehr"
        ],
    )


SSH_KEY_ENTRY = """
[[secret]]
name = "ssh/id_ed25519"
kind = "tool-login"
paths = ["~/.ssh/id_ed25519"]
read_by = ["ssh"]
recreate = "`ssh-keygen -t ed25519`"
"""


def close_the_ssh_folder_to_its_owner(home: Path, use_manifest, _probe: FakeProbe) -> str:
    use_manifest(TEST_MANIFEST + SSH_KEY_ENTRY)
    (home / "README.md").write_text(HOME_README + "`.ssh` too.\n")
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "id_ed25519").write_text(FAKE_SECRET_VALUE)
    (home / ".ssh").chmod(0o600)
    return (
        "~/.ssh/id_ed25519: Rechte des Elternordners prüfen (von Hand)"
        " — erwartet: home secrets meldet presence-unknown für ssh/id_ed25519 nicht mehr"
    )


def link_the_gitlab_token_to_itself(home: Path, _use_manifest, _probe: FakeProbe) -> str:
    token = home / ".secrets" / "gitlab"
    token.unlink()
    token.symlink_to(token)
    return (
        "~/.secrets/gitlab: Rechte des Elternordners prüfen (von Hand)"
        " — erwartet: home secrets meldet presence-unknown für gitlab nicht mehr"
    )


def leave_the_gh_check_unanswered(_home: Path, _use_manifest, probe: FakeProbe) -> str:
    probe.code = None
    return (
        "~/.config/gh: Prüfbefehl hing oder fehlt; später erneut (von Hand)"
        " — erwartet: home secrets meldet verification-unreachable für gh nicht mehr"
    )


@pytest.fixture
def reopened_ssh_folder(home: Path) -> Iterator[None]:
    """Gives ~/.ssh back to its owner afterwards, so the temporary home can be removed."""
    yield
    if (home / ".ssh").is_dir():
        (home / ".ssh").chmod(0o700)


@pytest.mark.usefixtures("hook_installed", "reopened_ssh_folder")
@pytest.mark.parametrize(
    "leave_a_finding_without_a_command",
    [
        pytest.param(close_the_ssh_folder_to_its_owner, id="ssh-folder-at-0600", marks=NOT_AS_ROOT),
        pytest.param(link_the_gitlab_token_to_itself, id="self-loop-symlink"),
        pytest.param(leave_the_gh_check_unanswered, id="verification-unreachable"),
    ],
)
def test_a_secrets_finding_without_a_command_is_a_step_by_hand_never_nichts_zu_tun(
    home: Path, use_manifest, probe: FakeProbe, capsys, leave_a_finding_without_a_command: Callable[..., str]
) -> None:
    """#37: "Nichts zu tun." exactly when no source reports a finding; one without a command names its place and recommendation."""
    step = leave_a_finding_without_a_command(home, use_manifest, probe)
    assert cli.main(["secrets"], home=home, probe=probe, now=NOW) == 1
    capsys.readouterr()

    assert home_next(home, probe, capsys) == (0, [f"1. {step}"])


def test_on_an_empty_home_the_missing_readme_is_a_step(sandbox: Sandbox, probe: FakeProbe, capsys) -> None:
    """#37 line 2: the tidy finding "README.md fehlt" names its fix "anlegen", so it is a step."""
    _code, lines = home_next(sandbox.home, probe, capsys)

    steps = [line.split(". ", 1)[1] for line in lines if line[:1].isdigit()]
    assert "README.md fehlt: anlegen (von Hand) — erwartet: home check meldet unknown-home-entry für README.md fehlt nicht mehr" in steps


def test_a_pending_setup_gives_home_setup_apply_once_although_the_hook_check_names_it_too(home: Path, probe: FakeProbe, capsys) -> None:
    """#37 line 2: the setup plan's open steps are one step, and the hook check's same fix is not repeated."""
    assert home_next(home, probe, capsys) == (0, list(SETUP_PENDING))


def test_folder_states_and_missing_secrets_come_before_a_pending_setup(home: Path, use_manifest, probe: FakeProbe, capsys) -> None:
    """#37 core order: blocking folder states first, then the missing secrets, then the pending setup."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    (home / ".secrets").chmod(0o755)

    _code, lines = home_next(home, probe, capsys)

    assert [line.split(" (", 1)[0] for line in lines if line[:1].isdigit()] == [
        "1. chmod 700 ~/.secrets",
        "2. home secrets --setup",
        "3. https://github.com/settings/tokens",
        "4. home setup --apply",
    ]


def test_an_unreadable_manifest_is_one_line_and_the_other_steps_still_show(home: Path, use_manifest, probe: FakeProbe, capsys) -> None:
    """#37 lines 1 and 7: a source that cannot be read is named once, first, as it blocks its steps; the others still give theirs."""
    use_manifest("[[secret]\n")

    assert home_next(home, probe, capsys) == (
        0,
        [UNREADABLE_SECRETS, *(line.replace("1. ", "2. ", 1) for line in SETUP_PENDING)],
    )


@pytest.mark.usefixtures("hook_installed")
def test_an_unreadable_source_with_no_steps_left_does_not_claim_nothing_to_do(home: Path, use_manifest, probe: FakeProbe, capsys) -> None:
    """#37 lines 4 and 7: when a source failed, the empty list says only that nothing else is to do."""
    use_manifest("[[secret]\n")

    assert home_next(home, probe, capsys) == (0, [UNREADABLE_SECRETS, "Sonst nichts zu tun."])


@pytest.fixture(
    params=[
        pytest.param((".config/home/config.toml", "bogus = 1\n"), id="unknown-key"),
        pytest.param((".config/tidy-home/config.toml", "[tidy]\n"), id="left-behind-old-config"),
    ]
)
def invalid_config(request: pytest.FixtureRequest, home: Path) -> None:
    relative, text = request.param
    config = home / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


@pytest.mark.usefixtures("hook_installed", "invalid_config")
def test_an_invalid_config_is_the_tidy_step_and_home_next_still_exits_0(home: Path, probe: FakeProbe, capsys) -> None:
    """#37 lines 4 and 7: exit 0 even with steps.

    Only home tidy reads the config, so an invalid one makes its source the one unreadable step.
    """
    assert home_next(home, probe, capsys) == (
        0,
        ["1. konnte das Home (home tidy) nicht lesen; den Fehler zeigt: home tidy", "Sonst nichts zu tun."],
    )


def test_it_never_shows_a_secret_value_and_changes_nothing(home: Path, use_manifest, probe: FakeProbe, capsys) -> None:
    """#37 line 5: read-only, presence only; the planted values never appear and no file is written or touched."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    before = files_under(home.parent)

    _code, lines = home_next(home, probe, capsys)

    assert lines
    for value in (FAKE_SECRET_VALUE, *FAKE_SECRET_VALUE.split()):
        assert value not in "\n".join(lines)
    assert files_under(home.parent) == before


def test_on_an_empty_home_every_step_of_the_shipped_manifest_starts_with_something_to_do(
    sandbox: Sandbox, probe: FakeProbe, capsys
) -> None:
    """#37 line 1, counterexample: no step is an item number to go and read or says "von Hand" for its command."""
    (sandbox.home / ".secrets").mkdir(mode=0o700)

    _code, lines = home_next(sandbox.home, probe, capsys)

    steps = [line for line in lines if line[:1].isdigit()]
    assert steps
    assert [step for step in steps if "#" in step or ". von Hand" in step] == []


@pytest.mark.usefixtures("hook_installed")
def test_loose_rights_and_an_unknown_file_come_with_their_fix_ahead_of_the_wizard(
    home: Path, use_manifest, probe: FakeProbe, capsys
) -> None:
    """#37 lines 1 and 2: a `home secrets` finding with an operator fix is a step, before the wizard it would block."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    (home / ".secrets").chmod(0o755)
    (home / ".secrets" / "gitlab").chmod(0o664)
    (home / ".secrets" / "stray.json").write_text("{}")
    (home / ".secrets" / "stray.json").chmod(0o600)

    _code, lines = home_next(home, probe, capsys)

    assert lines[:4] == [
        "1. chmod 700 ~/.secrets (beliebiges Terminal) — erwartet: home secrets meldet loose-mode für ~/.secrets nicht mehr",
        "2. chmod 600 ~/.secrets/gitlab (beliebiges Terminal) — erwartet: home secrets meldet loose-mode für ~/.secrets/gitlab nicht mehr",
        "3. ~/.secrets/stray.json ins Manifest eintragen oder löschen (von Hand)"
        " — erwartet: home secrets meldet unknown-file für ~/.secrets/stray.json nicht mehr",
        f"4. {WIZARD_STEP}",
    ]


def symlink_the_opentofu_folder(home: Path) -> tuple[str, str]:
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (home / ".secrets" / "opentofu").symlink_to(elsewhere)
    return symlinked_folder_fix("~/.secrets/opentofu"), "symlinked-folder für ~/.secrets/opentofu"


def put_a_file_where_the_cloudflared_folder_belongs(home: Path) -> tuple[str, str]:
    shutil.rmtree(home / ".secrets" / "cloudflared")
    (home / ".secrets" / "cloudflared").write_text("not a folder")
    return "mv -n ~/.secrets/cloudflared ~/.secrets/cloudflared.alt", "not-a-folder für ~/.secrets/cloudflared"


def close_the_cloudflared_folder_to(mode: int) -> Callable[[Path], tuple[str, str]]:
    def close(home: Path) -> tuple[str, str]:
        (home / ".secrets" / "cloudflared").chmod(mode)
        return "chmod 700 ~/.secrets/cloudflared", "closed-folder für ~/.secrets/cloudflared"

    return close


@pytest.fixture
def reopened_secrets_folders(home: Path) -> Iterator[None]:
    """Gives every folder directly under ~/.secrets back to its owner afterwards, so the temporary home can be removed."""
    yield
    for place in (home / ".secrets").iterdir():
        if place.is_dir() and not place.is_symlink():
            place.chmod(0o700)


@pytest.mark.usefixtures("hook_installed", "reopened_secrets_folders")
@pytest.mark.parametrize(
    "block_the_wizard",
    [
        pytest.param(symlink_the_opentofu_folder, id="symlinked-folder"),
        pytest.param(put_a_file_where_the_cloudflared_folder_belongs, id="not-a-folder"),
        pytest.param(close_the_cloudflared_folder_to(0), id="closed-neither-listed-nor-entered", marks=NOT_AS_ROOT),
        pytest.param(close_the_cloudflared_folder_to(0o600), id="closed-listed-but-not-entered", marks=NOT_AS_ROOT),
    ],
)
def test_a_folder_that_blocks_the_wizard_comes_first_with_its_fix_and_the_wizard_follows(
    home: Path, use_manifest, probe: FakeProbe, capsys, block_the_wizard: Callable[[Path], tuple[str, str]]
) -> None:
    """#37 lines 1 and 7: the wizard stores nothing below such a folder, so the step that clears it comes first, never the loop."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    fix, finding = block_the_wizard(home)

    _code, lines = home_next(home, probe, capsys)

    assert lines[0] == f"1. {fix} (beliebiges Terminal) — erwartet: home secrets meldet {finding} nicht mehr"
    assert lines[1].startswith("2. home secrets --setup ")


def run_step(home: Path, step: str) -> None:
    command = step.split(". ", 1)[1].split(" (beliebiges Terminal) — ", 1)[0]
    subprocess.run(["bash", "-c", command], env={**os.environ, "HOME": str(home)}, check=True, timeout=10)


@pytest.mark.usefixtures("hook_installed")
@pytest.mark.parametrize("target_exists", [pytest.param(True, id="link-to-a-folder"), pytest.param(False, id="dangling-link")])
def test_the_symlink_step_run_as_printed_leaves_a_real_folder_and_the_step_goes(
    home: Path, use_manifest, probe: FakeProbe, capsys, target_exists: bool
) -> None:
    """#37 core: every problem names its fix command, and that command works, also for a link whose target is gone."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    target = home.parent / "elsewhere"
    if target_exists:
        target.mkdir(mode=0o700)
        (target / "kept").write_text("kept")
        (target / "kept").chmod(0o600)
    (home / ".secrets" / "opentofu").symlink_to(target)

    _code, lines = home_next(home, probe, capsys)
    run_step(home, lines[0])

    place = home / ".secrets" / "opentofu"
    assert not place.is_symlink()
    assert place.is_dir()
    assert sorted(path.name for path in place.iterdir()) == (["kept"] if target_exists else [])
    assert not [line for line in home_next(home, probe, capsys)[1] if "symlinked-folder" in line]
