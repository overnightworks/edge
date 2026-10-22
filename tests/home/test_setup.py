"""`home setup` against a sandboxed HOME with stub `uv`, `quality` and Ansible; the rules are those of #22 and, for the system step, #40."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import Sandbox

import home_tools
from hal import cli as hal_cli
from hal.grammar import ExitCode
from home_tools import system_step
from home_tools.cli import main
from home_tools.setup import ANSIBLE_SOURCE, HOOK_NAMES, QUALITY_SOURCE
from home_tools.system_step import CheckState, PlaybookRun, SystemStep

STEP_NAMES = ["quality", "Engines", "Hook-Dateien", "Aktivierung", "Ansible", "System"]
CHANGED_STEPS = [f"{name}: changed" for name in STEP_NAMES]
UNCHANGED_STEPS = [f"{name}: unchanged" for name in STEP_NAMES]
NOT_CHECKED = "System: not checked (needs sudo at a terminal)"
NOT_CHECKED_WITHOUT_ANSIBLE = "System: not checked (needs Ansible, which `hal setup --apply` installs first)"
QUALITY_URL = "https://github.com/overnightworks/quality"


def run_setup(sandbox: Sandbox, capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, list[str], str]:
    code = main(["setup", *flags], home=sandbox.home)
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def global_hooks_path(sandbox: Sandbox) -> str:
    completed = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, text=True, check=False)
    return completed.stdout.strip()


def uv_installs(sandbox: Sandbox) -> list[str]:
    return sandbox.uv_calls.read_text().splitlines() if sandbox.uv_calls.exists() else []


def test_setup_without_apply_only_reports_and_changes_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 and E1: `home setup` prints what it would change; before Ansible is installed the system step is not checked."""
    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines) == (0, [*(f"{step}: would change" for step in STEP_NAMES[:-1]), NOT_CHECKED_WITHOUT_ANSIBLE])
    assert uv_installs(sandbox) == []
    assert sandbox.system.playbook.calls == []
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_apply_installs_pinned_quality_engines_and_both_hooks_then_activates(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4, steps 1-4 and rule 2: the pinned sources are installed, hooks are mode 755, hooksPath is the rendered directory."""
    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    layout = sandbox.layout
    assert (code, lines) == (0, CHANGED_STEPS)
    assert uv_installs(sandbox) == [f"tool install --force {QUALITY_SOURCE}", f"tool install --force {ANSIBLE_SOURCE}"]
    assert [oct((layout.git_hooks_dir / name).stat().st_mode & 0o777) for name in HOOK_NAMES] == ["0o755", "0o755"]
    assert global_hooks_path(sandbox) == str(layout.git_hooks_dir)


def test_second_apply_reports_every_step_unchanged_and_reinstalls_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 example and #40 cell 9: a second --apply is `unchanged` for every step and reinstalls neither tool."""
    run_setup(sandbox, capsys, "--apply")
    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (0, UNCHANGED_STEPS)
    assert len(uv_installs(sandbox)) == 2


def test_failing_quality_install_leaves_hooks_path_untouched_and_exits_nonzero(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 4 counterexample: when `uv tool install` fails, core.hooksPath is unchanged and the step is named."""
    monkeypatch.setenv("STUB_UV_FAIL", "1")

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, [])
    assert "quality" in error
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_failing_engines_install_stops_before_hooks_and_activation(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 4: apply stops at the first failure, naming the step; activation never runs after it."""
    monkeypatch.setenv("STUB_ENGINES_FAIL", "1")

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, ["quality: changed"])
    assert "Engines" in error
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_failing_activation_is_a_failed_last_step(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 step 4: a global config git cannot write fails the activation step."""
    sandbox.global_git_config.unlink()
    sandbox.global_git_config.mkdir()

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, CHANGED_STEPS[:3])
    assert "Aktivierung" in error
    assert sandbox.system.playbook.calls == []


def test_apply_repoints_a_hooks_path_that_differs(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 step 4: core.hooksPath is set when it differs from the rendered directory."""
    subprocess.run(["git", "config", "--global", "core.hooksPath", "/elsewhere"], check=True)

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines[3]) == (0, "Aktivierung: changed")
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_apply_reinstalls_a_deleted_quality_executable_despite_matching_uv_metadata(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 4 step 1 recovery: matching receipt, deleted executable, so quality is reinstalled and reported `changed`."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.layout.quality_executable.unlink()

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (0, ["quality: changed", *UNCHANGED_STEPS[1:]])
    assert len(uv_installs(sandbox)) == 3


@pytest.mark.parametrize(
    ("step", "receipt"),
    [
        ("quality", "not toml ["),
        ("quality", "[tool]\nrequirements = []\n"),
        ("quality", f'[tool]\nrequirements = [{{ name = "overnightworks-quality", git = "{QUALITY_URL}?rev=0000" }}]\n'),
        ("Ansible", '[tool]\nrequirements = [{ name = "ansible-core", specifier = "==2.0.0" }]\n'),
    ],
    ids=["unreadable", "no requirement", "other revision", "other Ansible version"],
)
def test_apply_reinstalls_a_tool_when_uv_recorded_another_requirement(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], step: str, receipt: str
) -> None:
    """Rule 4 step 1 and #40 rule 1: a requirement uv recorded that is not the pinned source triggers the install."""
    run_setup(sandbox, capsys, "--apply")
    receipt_path = sandbox.layout.quality_uv_receipt if step == "quality" else sandbox.layout.ansible_uv_receipt
    receipt_path.write_text(receipt)

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines[STEP_NAMES.index(step)]) == (0, f"{step}: changed")


@pytest.mark.parametrize("damage", ["lost executable bit", "other content"])
def test_apply_repairs_a_damaged_hook_file_and_a_following_apply_is_unchanged(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """Rule 4 step 3 recovery: a hook without mode 755 or with other content is rewritten and reported `changed`."""
    run_setup(sandbox, capsys, "--apply")
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    if damage == "other content":
        hook.write_text("#!/bin/sh\nexit 0\n")
    else:
        hook.chmod(0o644)

    repaired = run_setup(sandbox, capsys, "--apply")
    again = run_setup(sandbox, capsys, "--apply")

    assert repaired[1] == [*UNCHANGED_STEPS[:2], "Hook-Dateien: changed", *UNCHANGED_STEPS[3:]]
    assert os.access(hook, os.X_OK)
    assert again[1] == UNCHANGED_STEPS


def test_unwritable_hook_directory_fails_the_hook_step_by_name_and_skips_activation(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 4: a filesystem failure in the Hook-Dateien step names the step and exits 1, not the generic error."""
    sandbox.layout.git_hooks_dir.parent.parent.mkdir(parents=True)
    sandbox.layout.git_hooks_dir.parent.write_text("a file where the hooks parent directory belongs")

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, CHANGED_STEPS[:2])
    assert "Hook-Dateien" in error
    assert global_hooks_path(sandbox) == ""


def test_dry_run_with_a_non_executable_quality_reports_instead_of_crashing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4: the reporting-only `home setup` copes with a quality that is present but not executable."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.layout.quality_executable.chmod(0o644)

    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines[:2]) == (0, ["quality: would change", "Engines: would change"])


def test_apply_tightens_a_hook_whose_mode_is_wider_than_755(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3 and rule 4 step 3: the hook is mode 755; a world-writable hook with the right content is repaired."""
    run_setup(sandbox, capsys, "--apply")
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    hook.chmod(0o777)

    repaired = run_setup(sandbox, capsys, "--apply")

    assert repaired[1][2] == "Hook-Dateien: changed"
    assert hook.stat().st_mode & 0o777 == 0o755


# --- the system step (#40) ---------------------------------------------------


def playbook_runs(sandbox: Sandbox) -> list[str]:
    """Each Ansible run the system step started: `check` for the dry run's check mode, `apply` for a real run."""
    return ["check" if "--check" in argv else "apply" for argv in sandbox.system.playbook.calls]


def test_the_system_step_comes_after_the_activation_and_asks_for_sudo_once(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2 and rule 5: --apply runs Ansible once, with one become prompt and the diff, and only after the activation."""
    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    (argv,) = sandbox.system.playbook.calls
    assert (code, lines[-3:]) == (0, ["Aktivierung: changed", "Ansible: changed", "System: changed"])
    assert argv[:2] == [str(sandbox.layout.ansible_playbook_executable), str(system_step.PLAYBOOK)]
    assert (argv.count("--ask-become-pass"), "--diff" in argv, "--check" in argv) == (1, True, False)


def test_the_playbook_gets_home_and_the_tunnel_files_from_layout_and_the_running_user(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 5: @HOME@ and @OPERATOR_USER@ are extra vars from home's Layout and the running user, never facts gathered under become."""
    run_setup(sandbox, capsys, "--apply")

    (argv,) = sandbox.system.playbook.calls
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert {key: extra_vars[key] for key in ("home", "operator_user", "edge_tunnel_dir")} == {
        "home": str(sandbox.home),
        "operator_user": "operator",
        "edge_tunnel_dir": str(sandbox.home / "git" / "edge" / "tunnel"),
    }


def test_the_playbook_ships_inside_the_package() -> None:
    """Rule 3: the playbook is read from beside the module, so a non-editable install carries it."""
    assert system_step.PLAYBOOK.is_file()
    assert system_step.PLAYBOOK.is_relative_to(Path(home_tools.__file__).parent)


@pytest.mark.parametrize(
    ("applied_before", "expected", "recorded"),
    [(False, "System: would change", False), (True, "System: unchanged", True)],
    ids=["something to change", "nothing to change"],
)
def test_a_dry_run_at_a_terminal_checks_with_ansible_and_only_unchanged_counts_as_verified(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], applied_before: bool, expected: str, recorded: bool
) -> None:
    """E1 and rule 7: the dry run asks once for sudo through Ansible's check mode; only `unchanged` records a verified run."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.layout.system_step_record.unlink()
    sandbox.system.playbook.applied = applied_before

    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines[-1]) == (0, expected)
    assert playbook_runs(sandbox) == ["apply", "check"]
    assert sandbox.layout.system_step_record.exists() is recorded


def test_without_a_terminal_the_dry_run_says_not_checked_and_never_starts_ansible(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """E1 and cell 11: without a terminal nothing root-only is read, and the dry run never claims `unchanged`."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.system.terminal = False

    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines) == (0, [*UNCHANGED_STEPS[:-1], NOT_CHECKED])
    assert playbook_runs(sandbox) == ["apply"]


@pytest.mark.parametrize(
    ("terminal", "applied_before", "expected"),
    [(False, True, CheckState.NOT_CHECKED), (True, False, CheckState.WOULD_CHANGE), (True, True, CheckState.UNCHANGED)],
    ids=["no terminal", "something to change", "nothing to change"],
)
def test_the_system_steps_check_answers_with_one_of_the_three_states(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], terminal: bool, applied_before: bool, expected: CheckState
) -> None:
    """Rule 4: the check returns the enum itself; what a `not checked` step needs first is the presentation's text."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.system.playbook.applied = applied_before
    sandbox.system.terminal = terminal

    assert SystemStep(sandbox.layout, sandbox.system.boundaries).check() is expected


def test_without_a_terminal_apply_refuses_the_system_step_naming_the_command(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 6 and cell 11: the commit hook is set up, then the system step refuses with the command, never waiting on a prompt."""
    sandbox.system.terminal = False

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, CHANGED_STEPS[:-1])
    assert "`hal setup --apply` in your own terminal window" in error
    assert "„System“" in error
    assert playbook_runs(sandbox) == []
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_a_failed_ansible_run_fails_the_system_step_alone_and_records_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2, rule 7 and cells 12, 13, 17: a wrong password or a missing tunnel id fails only the last step, with exit 1."""
    sandbox.system.playbook.succeeds = False

    code, lines, error = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (1, CHANGED_STEPS[:-1])
    assert "„System“" in error
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)
    assert not sandbox.layout.system_step_record.exists()


def test_a_failed_check_fails_the_dry_run(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """E1: a check Ansible could not finish (a wrong password) is a failure, never `unchanged`."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.system.playbook.succeeds = False

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines[-1]) == (1, "Ansible: unchanged")
    assert "„System“" in error


def test_the_running_user_is_the_operator_the_polkit_rule_names(tmp_path: Path) -> None:
    """Rule 5: @OPERATOR_USER@ is whoever runs `home`, the owner of the files it creates."""
    assert system_step.TERMINAL_BOUNDARIES.operator_user() == tmp_path.owner()


RECAP_LINE = "localhost                  : ok=9    changed={changed}    unreachable={unreachable}    failed={failed}    skipped=2"


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (0, RECAP_LINE.format(changed=3, unreachable=0, failed=0), PlaybookRun(True, 3)),
        (0, RECAP_LINE.format(changed=0, unreachable=0, failed=0), PlaybookRun(True, 0)),
        (2, RECAP_LINE.format(changed=1, unreachable=0, failed=1), PlaybookRun(False, 1)),
        (4, RECAP_LINE.format(changed=0, unreachable=1, failed=0), PlaybookRun(False, 0)),
        (1, "ERROR! the playbook could not be read", PlaybookRun(False, 0)),
    ],
    ids=["changed", "converged", "failed task", "unreachable", "no recap"],
)
def test_a_run_succeeds_only_with_exit_0_and_a_recap_without_failures(returncode: int, output: str, expected: PlaybookRun) -> None:
    """Rule 4 and rule 7: a run succeeds only when Ansible exits 0 and its recap counts no failed or unreachable host."""
    assert system_step.read_recap(returncode, f"PLAY RECAP ***\n{output}\n") == expected


def fake_ansible(tmp_path: Path, script: str) -> Path:
    playbook = tmp_path / "ansible-playbook"
    playbook.write_text(f"#!/bin/sh\n{script}\necho '{RECAP_LINE.format(changed=2, unreachable=0, failed=0)}'\n")
    playbook.chmod(0o755)
    return playbook


def test_the_runner_shows_ansibles_output_and_reads_its_recap(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """Rule 4: the output reaches the terminal as it comes, and the run is judged by its recap."""
    playbook = fake_ansible(tmp_path, "echo 'TASK [Write edge files]'")

    run = system_step.run_playbook([str(playbook)])

    assert run == PlaybookRun(True, 2)
    assert "TASK [Write edge files]" in capfd.readouterr().out


def test_a_run_leaves_no_ansible_state_in_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """Head ruling 18.10.2026 and rule 4 (the runner): Ansible's home and temp live in a run-scoped directory.

    So no `~/.ansible` is left for `home check` to flag.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANSIBLE_NOCOLOR", "0")
    seen = tmp_path / "seen"
    playbook = fake_ansible(
        tmp_path,
        f'mkdir -p "${{ANSIBLE_HOME:-$HOME/.ansible}}" "${{ANSIBLE_LOCAL_TEMP:-$HOME/.ansible/tmp}}"\n'
        f'echo "$ANSIBLE_HOME $ANSIBLE_NOCOLOR $ANSIBLE_STDOUT_CALLBACK" > {seen}',
    )

    assert system_step.run_playbook([str(playbook)]) == PlaybookRun(True, 2)

    ansible_home, nocolor, callback = seen.read_text().split()
    assert list(home.iterdir()) == []
    assert not Path(ansible_home).exists()
    assert (nocolor, callback) == ("1", "default")


def run_hal_setup(capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, list[str], str]:
    """`hal setup` in process; the sandbox's HOME is the process's HOME, so hal finds the same world `home` was handed."""
    code = hal_cli.main(["setup", *flags])
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def test_hal_setup_shows_what_home_setup_shows(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    home_result = run_setup(sandbox, capsys)

    assert run_hal_setup(capsys) == (ExitCode.OK, home_result[1], "")


def test_hal_setup_apply_runs_what_home_setup_apply_runs(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    code, lines, _ = run_hal_setup(capsys, "--apply")

    assert (code, lines) == (ExitCode.OK, CHANGED_STEPS)
    assert uv_installs(sandbox) == [f"tool install --force {QUALITY_SOURCE}", f"tool install --force {ANSIBLE_SOURCE}"]
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_hal_setup_without_a_terminal_refuses_the_system_step_naming_itself_and_exits_3(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.system.terminal = False

    code, lines, error = run_hal_setup(capsys, "--apply")

    assert (code, lines) == (ExitCode.FAILURE, CHANGED_STEPS[:-1])
    assert "`hal setup --apply` in your own terminal window" in error
    assert "hal setup: Schritt „System“ ist fehlgeschlagen" in error
