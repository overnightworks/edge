"""`hal setup` against a sandboxed HOME with stub `uv`, `quality` and Ansible; the rules are those of #22 and, for the system step, #40."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from workstation_secrets_support import never_run
from workstation_support import FakeClock, Sandbox, run_captured_hal_setup, run_hal_setup

import hal.workstation
from hal import cli as hal_cli
from hal.grammar import ExitCode
from hal.workstation import system_step
from hal.workstation.secrets_setup import Boundaries, Terminal
from hal.workstation.setup import ANSIBLE_SOURCE, HOOK_NAMES, QUALITY_SOURCE, RERUN, ExecutionRole
from hal.workstation.system_step import AGENT_USER, CheckState, SystemStep

COMMIT_HOOK, PATH, TIMERS, SYSTEM, SECRETS = "commit hook", "path", "timers", "system", "secrets"
ROWS = [COMMIT_HOOK, PATH, TIMERS, SYSTEM, SECRETS]


# The sandbox declares no timer job: the timers have nothing to change, and no secret is open.
SUDO_ANNOUNCEMENT = "  › system          needs sudo     tunnel, polkit, journal, mail, agent account"
FRESH_APPLY = [
    "  ✓ commit hook     changed",
    "  · path            unchanged",
    "  · timers          unchanged",
    SUDO_ANNOUNCEMENT,
    "  ✓ system          changed",
    "  · secrets         complete",
    "✓ setup done · 2 changed, 3 unchanged · 47s",
]
REPEATED_APPLY = [
    "  · commit hook     unchanged",
    "  · path            unchanged",
    "  · timers          unchanged",
    SUDO_ANNOUNCEMENT,
    "  · system          unchanged",
    "  · secrets         complete",
    "✓ setup done · 5 unchanged · 47s",
]
UP_TO_DATE = [*(line for line in REPEATED_APPLY[:-1] if line != SUDO_ANNOUNCEMENT), "✓ setup up to date · 5 unchanged · 47s"]
NOT_CHECKED = "  ! system          not checked    needs sudo at a terminal"
NOT_CHECKED_WITHOUT_ANSIBLE = (
    "  ! system          not checked    needs Ansible, which `hal setup` installs first"
)
NOT_RUN = {
    PATH: "  · path            not run",
    TIMERS: "  · timers          not run",
    SYSTEM: "  · system          not run",
    SECRETS: "  · secrets         not run",
}
CHANGED_ROW = {
    COMMIT_HOOK: "  ✓ commit hook     changed",
    SYSTEM: "  ✓ system          changed",
}


GENERATED_SECRET = '''
[[secret]]
name = "opentofu/passphrase"
kind = "service-password"
paths = ["~/.secrets/opentofu/passphrase"]
read_by = ["hal apply"]
recreate = "hal setup"
'''


QUALITY_URL = "https://github.com/overnightworks/quality"


def run_setup(sandbox: Sandbox, capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, list[str], str]:
    return run_captured_hal_setup(capsys, *flags, home=sandbox.home, secrets_setup_boundaries=sandbox.secrets_boundaries)


def test_the_verdict_shows_the_time_the_run_took_by_the_clock_the_ports_carry(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """#500: setup measures from its start to its verdict with the monotonic clock of its ports."""

    code = run_hal_setup("--dry-run", home=sandbox.home, secrets_setup_boundaries=sandbox.secrets_boundaries, clock=FakeClock(65))

    assert (code, capsys.readouterr().out.splitlines()[-1]) == (
        0,
        "~ setup plan · 2 open, 3 unchanged · 1m05s · run hal setup",
    )


def global_hooks_path(sandbox: Sandbox) -> str:
    completed = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, text=True, check=False)
    return completed.stdout.strip()


def uv_installs(sandbox: Sandbox) -> list[str]:
    return sandbox.uv_calls.read_text().splitlines() if sandbox.uv_calls.exists() else []


def test_setup_without_apply_only_reports_and_changes_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 and E1: `hal setup` prints what it would change; before Ansible is installed the system step is not checked."""
    code, lines, error = run_setup(sandbox, capsys, "--dry-run")

    assert (code, lines, error) == (
        ExitCode.OK,
        [
            "  ~ commit hook     will change",
            "  · path            unchanged",
            "  · timers          unchanged",
            NOT_CHECKED_WITHOUT_ANSIBLE,
            "  · secrets         complete",
            "~ setup plan · 2 open, 3 unchanged · 47s · run hal setup",
        ],
        "",
    )
    assert uv_installs(sandbox) == []
    assert sandbox.system.playbook.calls == []
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_apply_installs_pinned_quality_engines_and_both_hooks_then_activates(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4, steps 1-4 and rule 2: the pinned sources are installed, hooks are mode 755, hooksPath is the rendered directory."""
    code, lines, _ = run_setup(sandbox, capsys)

    layout = sandbox.layout
    assert (code, lines) == (ExitCode.OK, FRESH_APPLY)
    assert uv_installs(sandbox) == [
        f"tool install --force {QUALITY_SOURCE}",
        f"tool install --force {ANSIBLE_SOURCE}",
    ]
    assert [oct((layout.git_hooks_dir / name).stat().st_mode & 0o777) for name in HOOK_NAMES] == [
        "0o755",
        "0o755",
    ]
    assert global_hooks_path(sandbox) == str(layout.git_hooks_dir)


def test_a_dry_run_with_a_terminal_announces_no_sudo(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """#498: the announcement belongs to an apply; a dry run asks for the password in the check, and prints no `needs sudo` row."""
    run_setup(sandbox, capsys)
    _, lines, _ = run_setup(sandbox, capsys, "--dry-run")

    assert SUDO_ANNOUNCEMENT not in lines


def test_second_apply_reports_every_step_unchanged_and_reinstalls_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 example and #40 cell 9: a second --apply is `unchanged` for every step and reinstalls neither tool."""
    run_setup(sandbox, capsys)
    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines) == (ExitCode.OK, REPEATED_APPLY)
    assert len(uv_installs(sandbox)) == 2


def test_a_walk_that_stored_everything_ends_setup_with_its_verdict_and_next_hal_apply(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], use_manifest
) -> None:
    """#503: the wizard prints no ending; the verdict of `hal setup` is followed by `next: hal apply`."""
    use_manifest(GENERATED_SECRET)
    at_the_keyboard = Boundaries(Terminal(lambda: True, lambda _: "", lambda _: "", lambda _: True), lambda *_: None, lambda _: None, never_run)

    code = run_hal_setup(home=sandbox.home, secrets_setup_boundaries=at_the_keyboard)
    lines = capsys.readouterr().out.splitlines()

    assert code == ExitCode.OK
    assert "  ✓ secrets         stored" in lines
    assert lines[-2:] == ["✓ setup done · 3 changed, 2 unchanged · 47s", "next: hal apply"]
    assert [line for line in lines if line.startswith("next:") or "offen" in line] == ["next: hal apply"]


def test_failing_quality_install_leaves_hooks_path_untouched_and_exits_nonzero(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 4 counterexample: when `uv tool install` fails, core.hooksPath is unchanged and the step is named."""
    monkeypatch.setenv("STUB_UV_FAIL", "1")

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines) == (
        ExitCode.FAILURE,
        [
            "  ✗ commit hook     failed         uv could not install quality",
            NOT_RUN[PATH], NOT_RUN[TIMERS], NOT_RUN[SYSTEM], NOT_RUN[SECRETS],
        ],
    )
    assert error.splitlines()[0] == "error: setup stopped at commit hook"
    assert error.splitlines()[1].startswith("next: uv tool install --force ")
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_failing_engines_install_stops_before_hooks_and_activation(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 4: apply stops at the first failure, naming the step; activation never runs after it."""
    monkeypatch.setenv("STUB_ENGINES_FAIL", "1")

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines) == (
        ExitCode.FAILURE,
        [
            "  ✗ commit hook     failed         the quality engines did not install",
            NOT_RUN[PATH], NOT_RUN[TIMERS], NOT_RUN[SYSTEM], NOT_RUN[SECRETS],
        ],
    )
    assert error.splitlines() == [
        "error: setup stopped at commit hook",
        f"next: {sandbox.layout.quality_executable} engines install {sandbox.layout.quality_engines_dir}",
    ]
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_failing_activation_is_a_failed_last_step(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 step 4: a global config git cannot write fails the activation step."""
    sandbox.global_git_config.unlink()
    sandbox.global_git_config.mkdir()

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines) == (
        ExitCode.FAILURE,
        [
            "  ✗ commit hook     failed         git could not set core.hooksPath",
            NOT_RUN[PATH], NOT_RUN[TIMERS], NOT_RUN[SYSTEM], NOT_RUN[SECRETS],
        ],
    )
    assert error.splitlines() == [
        "error: setup stopped at commit hook",
        f"next: git config --global core.hooksPath {sandbox.layout.git_hooks_dir}",
    ]
    assert sandbox.system.playbook.calls == []


def test_apply_repoints_a_hooks_path_that_differs(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 step 4: core.hooksPath is set when it differs from the rendered directory."""
    subprocess.run(["git", "config", "--global", "core.hooksPath", "/elsewhere"], check=True)

    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines[0]) == (ExitCode.OK, "  ✓ commit hook     changed")
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_apply_reinstalls_a_deleted_quality_executable_despite_matching_uv_metadata(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 4 step 1 recovery: matching receipt, deleted executable, so quality is reinstalled and reported `changed`."""
    run_setup(sandbox, capsys)
    sandbox.layout.quality_executable.unlink()

    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines) == (
        0,
        [
            "  ✓ commit hook     changed",
            *REPEATED_APPLY[1:-1],
            "✓ setup done · 1 changed, 4 unchanged · 47s",
        ],
    )
    assert len(uv_installs(sandbox)) == 3


@pytest.mark.parametrize(
    ("step", "row", "receipt"),
    [
        ("quality", COMMIT_HOOK, "not toml ["),
        ("quality", COMMIT_HOOK, "[tool]\nrequirements = []\n"),
        (
            "quality",
            COMMIT_HOOK,
            f'[tool]\nrequirements = [{{ name = "overnightworks-quality", git = "{QUALITY_URL}?rev=0000" }}]\n',
        ),
        (
            "Ansible",
            SYSTEM,
            '[tool]\nrequirements = [{ name = "ansible-core", specifier = "==2.0.0" }]\n',
        ),
    ],
    ids=["unreadable", "no requirement", "other revision", "other Ansible version"],
)
def test_apply_reinstalls_a_tool_when_uv_recorded_another_requirement(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], step: str, row: str, receipt: str
) -> None:
    """Rule 4 step 1 and #40 rule 1: a requirement uv recorded that is not the pinned source triggers the install."""
    run_setup(sandbox, capsys)
    receipt_path = sandbox.layout.quality_uv_receipt if step == "quality" else sandbox.layout.ansible_uv_receipt
    receipt_path.write_text(receipt)

    code, lines, _ = run_setup(sandbox, capsys)

    assert code == ExitCode.OK
    assert CHANGED_ROW[row] in lines


@pytest.mark.parametrize("damage", ["lost executable bit", "other content"])
def test_apply_repairs_a_damaged_hook_file_and_a_following_apply_is_unchanged(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """Rule 4 step 3 recovery: a hook without mode 755 or with other content is rewritten and reported `changed`."""
    run_setup(sandbox, capsys)
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    if damage == "other content":
        hook.write_text("#!/bin/sh\nexit 0\n")
    else:
        hook.chmod(0o644)

    repaired = run_setup(sandbox, capsys)
    again = run_setup(sandbox, capsys)

    assert repaired[1][0] == "  ✓ commit hook     changed"
    assert os.access(hook, os.X_OK)
    assert again[1] == REPEATED_APPLY


def test_unwritable_hook_directory_fails_the_hook_step_by_name_and_skips_activation(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 4: a filesystem failure in the Hook-Dateien step names the step and exits 1, not the generic error."""
    sandbox.layout.git_hooks_dir.parent.parent.mkdir(parents=True)
    sandbox.layout.git_hooks_dir.parent.write_text("a file where the hooks parent directory belongs")

    code, lines, error = run_setup(sandbox, capsys)

    assert code == ExitCode.FAILURE
    assert lines[0].startswith("  ✗ commit hook     failed         could not write the hook files:")
    assert error.splitlines() == ["error: setup stopped at commit hook", f"next: {RERUN}"]
    assert global_hooks_path(sandbox) == ""


def test_dry_run_with_a_non_executable_quality_reports_instead_of_crashing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4: the reporting-only `hal setup` copes with a quality that is present but not executable."""
    run_setup(sandbox, capsys)
    sandbox.layout.quality_executable.chmod(0o644)

    code, lines, _ = run_setup(sandbox, capsys, "--dry-run")

    assert (code, lines[0]) == (ExitCode.OK, "  ~ commit hook     will change")


def test_apply_tightens_a_hook_whose_mode_is_wider_than_755(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3 and rule 4 step 3: the hook is mode 755; a world-writable hook with the right content is repaired."""
    run_setup(sandbox, capsys)
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    hook.chmod(0o777)

    repaired = run_setup(sandbox, capsys)

    assert repaired[1][0] == "  ✓ commit hook     changed"
    assert hook.stat().st_mode & 0o777 == 0o755


# --- the system step (#40) ---------------------------------------------------


def playbook_runs(sandbox: Sandbox) -> list[str]:
    """Each Ansible run the system step started: `check` for the dry run's check mode, `apply` for a real run."""
    return ["check" if "--check" in argv else "apply" for argv in sandbox.system.playbook.calls]


def test_the_system_step_comes_after_the_activation_and_asks_for_sudo_once(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2 and rule 5: --apply runs Ansible once, with the diff, and only after the activation; sudo asks, never Ansible."""
    code, lines, _ = run_setup(sandbox, capsys)

    (argv,) = sandbox.system.playbook.calls
    assert (code, lines) == (ExitCode.OK, FRESH_APPLY)
    assert argv[:2] == [str(sandbox.layout.ansible_playbook_executable), str(system_step.PLAYBOOK)]
    assert ("--ask-become-pass" in argv, "--diff" in argv, "--check" in argv) == (False, True, False)


def test_the_playbook_gets_home_and_the_tunnel_files_from_layout_and_the_running_user(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 5: @HOME@ and @OPERATOR_USER@ are extra vars from home's Layout and the running user, never facts gathered as root."""
    run_setup(sandbox, capsys)

    (argv,) = sandbox.system.playbook.calls
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert {key: extra_vars[key] for key in ("home", "operator_user", "edge_tunnel_dir")} == {
        "home": str(sandbox.home),
        "operator_user": "operator",
        "edge_tunnel_dir": str(sandbox.home / "git" / "edge" / "tunnel"),
    }


def test_the_playbook_gets_the_agents_account_name_and_the_digest_covers_it(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    run_setup(sandbox, capsys)

    (argv,) = sandbox.system.playbook.calls
    assert json.loads(argv[argv.index("--extra-vars") + 1])["agent_user"] == AGENT_USER == "agent"
    assert "agent_user" in SystemStep(sandbox.layout, sandbox.system.boundaries).extra_vars()


@pytest.mark.parametrize(
    ("user", "role"),
    [("agent", ExecutionRole.AGENT), ("operator", ExecutionRole.OPERATOR), ("root", ExecutionRole.OPERATOR), ("bystander", ExecutionRole.OPERATOR), ("agent2", ExecutionRole.OPERATOR), ("Agent", ExecutionRole.OPERATOR)],
)
def test_only_the_account_named_agent_resolves_to_the_agent_role(user: str, role: ExecutionRole) -> None:
    assert ExecutionRole.of(user) is role


def passwd_entry(name: str, uid: int) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", uid, uid, "", f"/home/{name}", "/bin/sh"))


@pytest.mark.parametrize("names", [("agent", "operator"), ("operator", "agent")])
def test_setup_fails_loud_instead_of_guessing_the_role_when_agent_shares_the_running_uid(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, names: tuple[str, str]
) -> None:
    """Whichever name the lookup by uid would pick, `hal setup` says so and runs no step, with either passwd order."""
    entries = [passwd_entry(name, 1001) for name in names]
    monkeypatch.setattr(system_step.pwd, "getpwall", lambda: entries)
    monkeypatch.setattr(system_step.pwd, "getpwuid", lambda _uid: entries[0])
    monkeypatch.setattr(system_step.os, "getuid", lambda: 1001)
    monkeypatch.setattr(system_step, "TERMINAL_BOUNDARIES", replace(sandbox.system.boundaries, operator_user=system_step._running_user))

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines, error.splitlines()) == (
        ExitCode.FAILURE,
        [],
        [f"error: uid 1001 has the names {names[0]}, {names[1]}; agent must have a uid of its own"],
    )
    assert sandbox.system.playbook.calls == []


@pytest.mark.parametrize("names", [("operator", "bystander"), ("agent",)])
def test_the_running_user_is_the_name_of_its_uid_unless_agent_is_one_of_several(
    monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]
) -> None:
    entries = [passwd_entry(name, 1001) for name in names]
    monkeypatch.setattr(system_step.pwd, "getpwall", lambda: entries)
    monkeypatch.setattr(system_step.pwd, "getpwuid", lambda _uid: entries[0])
    monkeypatch.setattr(system_step.os, "getuid", lambda: 1001)

    assert system_step._running_user() == names[0]


def test_a_user_that_the_passwd_enumeration_omits_is_resolved_by_its_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_step.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(system_step.pwd, "getpwuid", lambda _uid: passwd_entry("lookuponly", 1001))
    monkeypatch.setattr(system_step.os, "getuid", lambda: 1001)

    assert system_step._running_user() == "lookuponly"


def test_a_running_uid_without_a_passwd_name_is_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_uid: int) -> pwd.struct_passwd:
        raise KeyError(_uid)

    monkeypatch.setattr(system_step.pwd, "getpwuid", missing)
    monkeypatch.setattr(system_step.os, "getuid", lambda: 1001)

    with pytest.raises(system_step.UnresolvableRunningUser, match="no passwd entry for uid 1001"):
        system_step._running_user()


@pytest.mark.parametrize("flags", [("--dry-run",), ()])
def test_as_the_agent_the_operators_steps_are_skipped_without_sudo_and_the_exit_is_0(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    """Cells 4 and 5: the per-user steps behave as for anyone, the operator's are skipped, and neither Ansible nor sudo is started."""
    sandbox.system.user = AGENT_USER
    code, lines, _ = run_setup(sandbox, capsys, *flags)

    hook = "  ~ commit hook     will change" if flags else "  ✓ commit hook     changed"
    closing = (
        "~ setup plan · 1 open, 4 skipped · 47s · run hal setup"
        if flags
        else "✓ setup done · 1 changed, 4 skipped · 47s"
    )
    assert (code, lines) == (
        0,
        [
            hook,
            "  · path            skipped        the operator's step",
            "  · timers          skipped        the operator's step",
            "  · system          skipped        the operator's step",
            "  · secrets         skipped        the operator's step",
            closing,
        ],
    )
    assert sandbox.system.playbook.calls == []


@pytest.mark.parametrize("user", ["root", "bystander"])
def test_every_user_but_the_agent_is_refused_without_a_terminal_before_any_step(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], user: str
) -> None:
    """Cell 7: no sudo inferred from the user's name or rights; root and a bystander meet the yes guard like the operator."""
    sandbox.system.user = user
    sandbox.system.terminal = False
    code, _, err = run_setup(sandbox, capsys)

    assert code == ExitCode.REFUSED
    assert err.splitlines()[0] == "error: setup asks yes at a terminal · nothing changed"
    assert sandbox.system.playbook.calls == []


def test_the_playbook_vacuums_two_days_short_of_the_ruled_retention(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """T6: the timer's `--vacuum-time` is the ruled 30 days minus the file-span and slack days."""
    run_setup(sandbox, capsys)

    (argv,) = sandbox.system.playbook.calls
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert (system_step.LOG_RETENTION_DAYS, extra_vars["journal_vacuum_days"]) == (30, 28)


def test_the_playbook_is_given_the_journal_as_docker_log_driver(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """E1 to E3: the play merges the one driver name `system_step` owns into `daemon.json`."""
    run_setup(sandbox, capsys)
    (argv,) = sandbox.system.playbook.calls
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert extra_vars["docker_log_driver"] == system_step.DOCKER_LOG_DRIVER == "journald"


def test_the_playbook_ships_inside_the_package() -> None:
    """Rule 3: the playbook is read from beside the module, so a non-editable install carries it."""
    assert system_step.PLAYBOOK.is_file()
    assert system_step.PLAYBOOK.is_relative_to(Path(hal.workstation.__file__).parent)


@pytest.mark.parametrize(
    ("applied_before", "expected"),
    [
        (
            False,
            [
                *UP_TO_DATE[:3],
                "  ~ system          will change",
                UP_TO_DATE[4],
                "~ setup plan · 1 open, 4 unchanged · 47s · run hal setup",
            ],
        ),
        (True, UP_TO_DATE),
    ],
    ids=["something to change", "nothing to change"],
)
def test_a_dry_run_at_a_terminal_checks_with_ansible_and_shows_whether_the_system_step_would_change(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], applied_before: bool, expected: list[str]
) -> None:
    """E1 and rule 7: the dry run asks once for sudo through Ansible's check mode."""
    run_setup(sandbox, capsys)
    sandbox.system.playbook.applied = applied_before

    code, lines, _ = run_setup(sandbox, capsys, "--dry-run")

    assert (code, lines) == (ExitCode.OK, expected)
    assert playbook_runs(sandbox) == ["apply", "check"]


def test_without_a_terminal_the_dry_run_says_not_checked_and_never_starts_ansible(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """E1 and cell 11: without a terminal nothing root-only is read, and the dry run never claims `unchanged`."""
    run_setup(sandbox, capsys)
    sandbox.system.terminal = False

    code, lines, _ = run_setup(sandbox, capsys, "--dry-run")

    assert (code, lines) == (
        0,
        [
            *UP_TO_DATE[:3],
            NOT_CHECKED,
            UP_TO_DATE[4],
            "~ setup plan · 1 open, 4 unchanged · 47s · run hal setup",
        ],
    )
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
    run_setup(sandbox, capsys)
    sandbox.system.playbook.applied = applied_before
    sandbox.system.terminal = terminal

    assert SystemStep(sandbox.layout, sandbox.system.boundaries).check() is expected


def test_a_failed_ansible_run_fails_the_system_step_alone_and_records_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2, rule 7 and cells 12, 13, 17: a wrong password or a missing tunnel id fails only the last step, with exit 3."""
    sandbox.system.playbook.succeeds = False

    code, lines, error = run_setup(sandbox, capsys)

    assert (code, lines) == (
        ExitCode.FAILURE,
        [
            *FRESH_APPLY[:3],
            SUDO_ANNOUNCEMENT,
            "  ✗ system          failed         Ansible did not finish · its output above says why",
            NOT_RUN[SECRETS],
        ],
    )
    assert error.splitlines() == [
        "error: setup stopped at system · /etc may be partial",
        f"next: {RERUN}",
    ]
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_a_failed_check_fails_the_dry_run(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """E1: a check Ansible could not finish (a wrong password) is a failure, never `unchanged`."""
    run_setup(sandbox, capsys)
    sandbox.system.playbook.succeeds = False

    code, lines, error = run_setup(sandbox, capsys, "--dry-run")

    assert (code, lines[3:]) == (
        ExitCode.FAILURE,
        [
            "  ✗ system          failed         Ansible's check did not finish · its output above says why",
            NOT_RUN[SECRETS],
        ],
    )
    assert error.splitlines() == [
        "error: setup stopped at system · /etc unchanged",
        f"next: {RERUN}",
    ]


def test_setup_help_warns_that_the_system_step_restarts_docker(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        hal_cli.main(["setup", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert "restarts Docker while its default driver is not journald: every container stops meanwhile" in help_text
    assert "--dry-run" in help_text
    assert "--apply" not in help_text


def test_the_running_user_is_the_operator_the_polkit_rule_names(tmp_path: Path) -> None:
    """Rule 5: @OPERATOR_USER@ is whoever runs `hal setup`, the owner of the files it creates."""
    assert system_step.TERMINAL_BOUNDARIES.operator_user() == tmp_path.owner()
