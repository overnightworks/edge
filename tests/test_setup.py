"""`home setup` against a sandboxed HOME with stub `uv` and `quality`; the rules are those of overnightworks/home-tools#22."""

from __future__ import annotations

import os
import subprocess

import pytest
from conftest import Sandbox

from home_tools.cli import main
from home_tools.setup import HOOK_NAMES, QUALITY_SOURCE

STEP_NAMES = ["quality", "Engines", "Hook-Dateien", "Aktivierung"]
CHANGED_STEPS = ["quality: geändert", "Engines: geändert", "Hook-Dateien: geändert", "Aktivierung: geändert"]
UNCHANGED_STEPS = ["quality: unverändert", "Engines: unverändert", "Hook-Dateien: unverändert", "Aktivierung: unverändert"]


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
    """Rule 4: `home setup` prints what it would change."""
    code, lines, _ = run_setup(sandbox, capsys)

    assert (code, lines) == (0, [f"{step}: würde geändert" for step in STEP_NAMES])
    assert uv_installs(sandbox) == []
    assert global_hooks_path(sandbox) == ""
    assert not sandbox.layout.git_hooks_dir.exists()


def test_apply_installs_pinned_quality_engines_and_both_hooks_then_activates(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4, steps 1-4 and rule 2: the pinned source is installed, hooks are mode 755, hooksPath is the rendered directory."""
    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    layout = sandbox.layout
    assert (code, lines) == (0, CHANGED_STEPS)
    assert uv_installs(sandbox) == [f"tool install --force {QUALITY_SOURCE}"]
    assert [oct((layout.git_hooks_dir / name).stat().st_mode & 0o777) for name in HOOK_NAMES] == ["0o755", "0o755"]
    assert global_hooks_path(sandbox) == str(layout.git_hooks_dir)


def test_second_apply_reports_unchanged_four_times_and_reinstalls_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 example: a second --apply is `unverändert` four times and does not reinstall quality."""
    run_setup(sandbox, capsys, "--apply")
    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (0, UNCHANGED_STEPS)
    assert len(uv_installs(sandbox)) == 1


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

    assert (code, lines) == (1, ["quality: geändert"])
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


def test_apply_repoints_a_hooks_path_that_differs(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 4 step 4: core.hooksPath is set when it differs from the rendered directory."""
    subprocess.run(["git", "config", "--global", "core.hooksPath", "/elsewhere"], check=True)

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines[-1]) == (0, "Aktivierung: geändert")
    assert global_hooks_path(sandbox) == str(sandbox.layout.git_hooks_dir)


def test_apply_reinstalls_a_deleted_quality_executable_despite_matching_uv_metadata(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rule 4 step 1 recovery: matching receipt, deleted executable, so quality is reinstalled and reported `geändert`."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.layout.quality_executable.unlink()

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines) == (0, ["quality: geändert", *UNCHANGED_STEPS[1:]])
    assert len(uv_installs(sandbox)) == 2


@pytest.mark.parametrize(
    "receipt",
    [
        "not toml [",
        "[tool]\nrequirements = []\n",
        '[tool]\nrequirements = [{ name = "overnightworks-quality", git = "https://github.com/overnightworks/quality?rev=0000" }]\n',
    ],
    ids=["unreadable", "no requirement", "other revision"],
)
def test_apply_reinstalls_quality_when_uv_recorded_another_requirement(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], receipt: str
) -> None:
    """Rule 4 step 1: a requirement uv recorded that is not QUALITY_SOURCE triggers the install."""
    run_setup(sandbox, capsys, "--apply")
    sandbox.layout.quality_uv_receipt.write_text(receipt)

    code, lines, _ = run_setup(sandbox, capsys, "--apply")

    assert (code, lines[0]) == (0, "quality: geändert")


@pytest.mark.parametrize("damage", ["lost executable bit", "other content"])
def test_apply_repairs_a_damaged_hook_file_and_a_following_apply_is_unchanged(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    """Rule 4 step 3 recovery: a hook without mode 755 or with other content is rewritten and reported `geändert`."""
    run_setup(sandbox, capsys, "--apply")
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    if damage == "other content":
        hook.write_text("#!/bin/sh\nexit 0\n")
    else:
        hook.chmod(0o644)

    repaired = run_setup(sandbox, capsys, "--apply")
    again = run_setup(sandbox, capsys, "--apply")

    assert repaired[1] == [*UNCHANGED_STEPS[:2], "Hook-Dateien: geändert", "Aktivierung: unverändert"]
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

    assert (code, lines[:2]) == (0, ["quality: würde geändert", "Engines: würde geändert"])


def test_apply_tightens_a_hook_whose_mode_is_wider_than_755(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3 and rule 4 step 3: the hook is mode 755; a world-writable hook with the right content is repaired."""
    run_setup(sandbox, capsys, "--apply")
    hook = sandbox.layout.git_hooks_dir / HOOK_NAMES[0]
    hook.chmod(0o777)

    repaired = run_setup(sandbox, capsys, "--apply")

    assert repaired[1][2] == "Hook-Dateien: geändert"
    assert hook.stat().st_mode & 0o777 == 0o755
