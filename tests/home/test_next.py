from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from conftest import (
    FAKE_SECRET_VALUE,
    HOME_README,
    TEST_MANIFEST,
    FakePlaybook,
    FakeProbe,
    RunnerWithRealGitAndQuality,
    Sandbox,
    symlinked_folder_fix,
)

from home_tools import cli, system_step
from home_tools.layout import Layout

NOW = datetime(2026, 10, 17, 9, 0)

# A secret the wizard generates; it is never planted.
WIZARD_ENTRY = """
[[secret]]
name = "opentofu/passphrase"
kind = "service-password"
paths = ["~/.secrets/opentofu/passphrase"]
read_by = ["edge external plan"]
recreate = "home secrets --setup"
"""

# The wizard's secret and a retired token whose revocation nobody confirmed; neither is planted.
OPEN_ENTRIES = (
    WIZARD_ENTRY
    + """
[[secret]]
name = "github/old"
kind = "token"
paths = ["~/.secrets/github/old"]
read_by = []
recreate = "nicht neu anlegen"
retired_on = 2026-10-14
revoke_url = "https://github.com/settings/tokens"
"""
)

# What the wizard says at the end of a walk that stores opentofu/passphrase: its stored row and the step after it.
WIZARD_STEP = (
    "home secrets --setup (eigenes Terminalfenster) — erwartet: opentofu/passphrase generated"
    " ~/.secrets/opentofu/passphrase (600) · never shown; then hal apply"
)
NOT_AS_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
UNREADABLE_SECRETS = "1. konnte die Geheimnisse nicht lesen; den Fehler zeigt: home secrets"
AGENT_SORTS_IT_IN = "Das macht ein Agent: sag „räum home auf“."

SETUP_PENDING = (
    "1. hal setup --apply (eigenes Terminalfenster) — erwartet: quality, Engines, Hook-Dateien, Aktivierung, Ansible, System: changed;"
    " it asks once for sudo",
    "   warum: without the hook no commit is checked for secrets;"
    " without it the tunnel ignores edge's global stop after a reboot, and only root may stop it",
)
SYSTEM_PENDING = (
    "1. hal setup --apply (eigenes Terminalfenster) — erwartet: System: changed; it asks once for sudo",
    "   warum: without it the tunnel ignores edge's global stop after a reboot, and only root may stop it",
)


@pytest.fixture
def home(sandbox: Sandbox, furnish_clean_home: Callable[[Path], Path]) -> Path:
    """The conftest's clean home inside the sandbox's HOME, so that `hal setup` could install the hook there."""
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


@dataclass(frozen=True)
class Arrangement:
    """What a cell leaves open in an otherwise clean home with the hook installed; each count is that many findings."""

    wizard_secret_missing: bool = False
    unknown_secret_files: int = 0
    loose_secret: bool = False
    manifest_unreadable: bool = False


def arrange(home: Path, use_manifest: Callable[[str], Path], cell: Arrangement) -> None:
    use_manifest("[[secret]\n" if cell.manifest_unreadable else TEST_MANIFEST + (WIZARD_ENTRY if cell.wizard_secret_missing else ""))
    for number in range(cell.unknown_secret_files):
        (home / ".secrets" / f"stray-{number}.json").write_text("{}")
        (home / ".secrets" / f"stray-{number}.json").chmod(0o600)
    if cell.loose_secret:
        (home / ".secrets" / "gitlab").chmod(0o664)


LOOSE_GITLAB = (
    "chmod 600 ~/.secrets/gitlab (beliebiges Terminal) — erwartet: home secrets meldet loose-mode für ~/.secrets/gitlab nicht mehr"
)


@pytest.mark.usefixtures("hook_installed")
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        pytest.param(
            Arrangement(wizard_secret_missing=True, unknown_secret_files=4),
            [
                f"1. {WIZARD_STEP}",
                f"Außerdem zum Einsortieren: 4 unbekannte Dateien in ~/.secrets. {AGENT_SORTS_IT_IN}",
            ],
            id="N1-one-wizard-step-in-its-own-terminal-window-and-one-count-line",
        ),
        pytest.param(
            Arrangement(unknown_secret_files=2),
            ["Nichts für dich zu tun.", f"Außerdem zum Einsortieren: 2 unbekannte Dateien in ~/.secrets. {AGENT_SORTS_IT_IN}"],
            id="N2-only-bookkeeping-is-nothing-for-you",
        ),
        pytest.param(Arrangement(), ["Nichts zu tun."], id="N3-nothing-at-all"),
        pytest.param(
            Arrangement(loose_secret=True, unknown_secret_files=1),
            [f"1. {LOOSE_GITLAB}", f"Außerdem zum Einsortieren: 1 unbekannte Datei in ~/.secrets. {AGENT_SORTS_IT_IN}"],
            id="N4-a-loose-mode-is-still-a-step-with-its-chmod",
        ),
    ],
)
def test_home_next_lists_only_your_steps_and_counts_the_bookkeeping_in_one_line(
    home: Path, use_manifest, probe: FakeProbe, capsys, cell: Arrangement, expected: list[str]
) -> None:
    """#41 lines 1-4: a step is what only the operator can do; every finding an agent sorts in is one count per kind, last."""
    arrange(home, use_manifest, cell)

    assert home_next(home, probe, capsys) == (0, expected)


@pytest.mark.usefixtures("hook_installed")
def test_every_open_step_names_its_command_where_to_run_it_and_what_to_expect_most_blocking_first(
    home: Path, use_manifest, probe: FakeProbe, capsys
) -> None:
    """#37 lines 1, 2 and 4: missing secrets give the wizard and the by-hand page, then the failed login and the revocation."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    (home / ".secrets" / "gitlab").unlink()
    probe.code = 1

    code, lines = home_next(home, probe, capsys)

    assert code == 0
    assert lines == [
        f"1. {WIZARD_STEP}",
        "2. https://gitlab.example/-/user_settings/personal_access_tokens (von Hand, für gitlab)"
        " — erwartet: home secrets zeigt gitlab vorhanden",
        "3. gh auth login --web (beliebiges Terminal) — erwartet: home secrets zeigt gh geprüft: gültig",
        "4. https://github.com/settings/tokens (Browser, github/old widerrufen) — erwartet: das Token steht dort nicht mehr",
    ]


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
        "4. hal setup --apply",
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
        pytest.param((".config/home/config.toml", "[tidy]\n"), id="tidy-table"),
    ]
)
def invalid_config(request: pytest.FixtureRequest, home: Path) -> None:
    relative, text = request.param
    config = home / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


@pytest.mark.usefixtures("hook_installed", "invalid_config")
def test_home_next_reads_no_config_so_an_invalid_one_changes_nothing(home: Path, probe: FakeProbe, capsys) -> None:
    """#37 lines 4 and 7: exit 0 whatever the config file holds."""
    assert home_next(home, probe, capsys) == (0, ["Nichts zu tun."])


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
def test_loose_rights_come_with_their_fix_ahead_of_the_wizard_and_the_unknown_file_is_counted_last(
    home: Path, use_manifest, probe: FakeProbe, capsys
) -> None:
    """#37 lines 1 and 2: a `home secrets` finding the operator fixes is a step before the wizard; the unknown file is counted (#41)."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    (home / ".secrets").chmod(0o755)
    (home / ".secrets" / "gitlab").chmod(0o664)
    (home / ".secrets" / "stray.json").write_text("{}")
    (home / ".secrets" / "stray.json").chmod(0o600)

    _code, lines = home_next(home, probe, capsys)

    assert lines[:3] == [
        "1. chmod 700 ~/.secrets (beliebiges Terminal) — erwartet: home secrets meldet loose-mode für ~/.secrets nicht mehr",
        "2. chmod 600 ~/.secrets/gitlab (beliebiges Terminal) — erwartet: home secrets meldet loose-mode für ~/.secrets/gitlab nicht mehr",
        f"3. {WIZARD_STEP}",
    ]
    assert lines[-1] == f"Außerdem zum Einsortieren: 1 unbekannte Datei in ~/.secrets. {AGENT_SORTS_IT_IN}"


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


# --- the system step (#40, E2): `home next` reads its record and never asks for sudo ---


def edge_tunnel_file(home: Path) -> Path:
    path = Layout(home).edge_tunnel_dir / "cloudflared.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def setup_step(lines: list[str]) -> list[str]:
    """The `hal setup --apply` step and its why, without its number."""
    index = next(index for index, line in enumerate(lines) if ". hal setup --apply" in line)
    return [lines[index].split(". ", 1)[1], lines[index + 1]]


def test_before_any_verified_run_the_system_step_shows_and_neither_ansible_nor_sudo_runs(
    home: Path, sandbox: Sandbox, probe: FakeProbe, capsys
) -> None:
    """Cell 14: the step and its sudo note show from the record alone; no playbook and no sudo is started."""
    runner = RunnerWithRealGitAndQuality()
    cli.main(["next"], home=home, run=runner, probe=probe, now=NOW)

    assert capsys.readouterr().out.splitlines()[:2] == list(SETUP_PENDING)
    assert sandbox.system.playbook.calls == []
    assert [call for call in runner.calls if Path(call[0]).name in {"sudo", "ansible-playbook"}] == []


def test_as_the_agent_home_next_lists_none_of_the_operators_steps(
    home: Path, sandbox: Sandbox, use_manifest, probe: FakeProbe, capsys
) -> None:
    """Matrix A1 cell 4: Ansible, the system step, the user units and the secrets are not the agent's; it is never asked for sudo."""
    use_manifest(TEST_MANIFEST + OPEN_ENTRIES)
    sandbox.system.user = system_step.AGENT_USER

    _, lines = home_next(home, probe, capsys)

    assert lines == [
        "1. hal setup --apply (beliebiges Terminal) — erwartet: quality, Engines, Hook-Dateien, Aktivierung: changed",
        "   warum: without the hook no commit is checked for secrets",
    ]


@pytest.mark.usefixtures("hook_installed")
def test_after_a_verified_apply_there_is_no_system_step(home: Path, probe: FakeProbe, capsys) -> None:
    """Cell 15."""
    assert home_next(home, probe, capsys) == (0, ["Nichts zu tun."])


@pytest.mark.usefixtures("hook_installed")
def test_with_only_a_local_step_open_after_a_verified_system_run_the_step_still_asks_for_sudo_in_your_own_window(
    home: Path, probe: FakeProbe, capsys
) -> None:
    """E2 'its line says that the step asks once for sudo': `--apply` always runs the system step, so a damaged hook file needs it too."""
    (Layout(home).git_hooks_dir / "pre-commit").write_text("#!/bin/sh\n")

    assert home_next(home, probe, capsys) == (
        0,
        [
            "1. hal setup --apply (eigenes Terminalfenster) — erwartet: Hook-Dateien: changed; it asks once for sudo",
            "   warum: without the hook no commit is checked for secrets",
        ],
    )


@pytest.mark.usefixtures("hook_installed")
def test_a_hook_finding_fixed_by_home_setup_apply_asks_for_sudo_in_your_own_window_when_setup_is_unreadable(
    home: Path, probe: FakeProbe, capsys
) -> None:
    """E2 'its line says that the step asks once for sudo': the hook check's `hal setup --apply` says so on its own too."""
    hook = Layout(home).git_hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\n")

    def refusing_operator() -> str:
        raise system_step.UnresolvableRunningUser("no passwd entry for the running user")

    unreadable_setup = system_step.Boundaries(lambda: True, FakePlaybook(), refusing_operator)
    code = cli.main(["next"], home=home, run=RunnerWithRealGitAndQuality(), probe=probe, now=NOW, system_boundaries=unreadable_setup)

    assert (code, capsys.readouterr().out.splitlines()) == (
        0,
        [
            "1. konnte den Stand von hal setup nicht lesen; den Fehler zeigt: hal setup",
            f"2. hal setup --apply (eigenes Terminalfenster) — erwartet: home check meldet hook-file für {hook} nicht mehr; "
            "it asks once for sudo",
        ],
    )


@pytest.mark.usefixtures("hook_installed")
def test_a_hook_finding_with_its_own_fix_is_any_terminal_and_does_not_say_it_asks_for_sudo(
    home: Path, sandbox: Sandbox, probe: FakeProbe, capsys
) -> None:
    """E2 'its line says that the step asks once for sudo': only `hal setup --apply` does, not a hook finding's other fix."""
    repository = home / "git" / "project"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)

    assert home_next(home, probe, capsys) == (
        0,
        [
            f"1. {sandbox.global_git_config} bearbeiten und die Zeile core.hooksPath (oder das include) entfernen (beliebiges Terminal)"
            f" — erwartet: home check meldet hooks-path für {repository} nicht mehr",
        ],
    )


@pytest.mark.parametrize("change", ["edited file", "new file", "changed playbook"])
def test_a_changed_tunnel_file_or_playbook_brings_the_system_step_back(
    home: Path,
    sandbox: Sandbox,
    probe: FakeProbe,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    """Cell 16 and T6: the record holds what was rendered, so a change in edge's `tunnel/` or in the
    playbook shows the step again, sudo-free."""
    playbook = tmp_path / "playbook.yml"
    playbook.write_text("- hosts: localhost\n")
    monkeypatch.setattr(system_step, "PLAYBOOK", playbook)
    edge_tunnel_file(home).write_text("[Unit]\n")
    assert cli.main(["setup", "--apply"], home=home) == 0
    capsys.readouterr()
    if change == "changed playbook":
        playbook.write_text("- hosts: localhost\n  gather_facts: false\n")
    elif change == "edited file":
        edge_tunnel_file(home).write_text("[Unit]\nDescription=cloudflared\n")
    else:
        (edge_tunnel_file(home).parent / "cloudflared.service.d").mkdir()
        (edge_tunnel_file(home).parent / "cloudflared.service.d" / "40-new.conf").write_text("[Service]\n")

    _code, lines = home_next(home, probe, capsys)

    assert setup_step(lines) == setup_step(list(SYSTEM_PENDING))
    assert [argv for argv in sandbox.system.playbook.calls if "--check" in argv] == []


@pytest.mark.parametrize("verified_before", [False, True], ids=["never verified", "verified earlier"])
def test_a_failed_ansible_run_leaves_no_verified_record_and_the_system_step_stays(
    home: Path, sandbox: Sandbox, probe: FakeProbe, capsys, verified_before: bool
) -> None:
    """Cell 17 and rule 7: only a run Ansible reports as successful is recorded, and a failed apply withdraws an earlier record."""
    if verified_before:
        assert cli.main(["setup", "--apply"], home=home) == 0
    sandbox.system.playbook.succeeds = False
    assert cli.main(["setup", "--apply"], home=home) == 1
    capsys.readouterr()

    assert home_next(home, probe, capsys) == (0, list(SYSTEM_PENDING))
