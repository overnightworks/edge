"""The grammar of `hal backup` and `hal restore` on the scripted Docker host: what each flag
combination does, how a failed step and SIGTERM end, and what the realm step reads."""

import json
import os
import signal
import sys

import pytest
from edge_backup_support import declare_two_apps, no_restore_server_left


@pytest.mark.parametrize(
    "command",
    [("backup",), ("restore", "--test"), ("backup", "--unlock"), ("restore", "--list")],
    ids=" ".join,
)
def test_no_backup_command_reads_stdin(edge_backup, monkeypatch, command):
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"read stdin ({name})")

    edge_backup("backup")
    monkeypatch.setattr(sys, "stdin", Forbidden())

    assert edge_backup(*command).exit_code == 0


def test_pre_apply_json_answers_one_object_naming_the_snapshot(edge_backup, machine, home):
    outcome = edge_backup(*declare_two_apps(home), "backup", "radio", "--pre-apply", "--json")

    assert outcome.exit_code == 0, outcome.err
    assert json.loads(outcome.out) == {"snapshot": machine.snapshots[-1]["id"]}
    assert outcome.out.count("\n") == 1


@pytest.mark.parametrize(
    ("arguments", "program"),
    [
        (("backup", "nobody"), "hal backup"),
        (("restore", "--test", "nobody"), "hal restore"),
    ],
)
def test_an_undeclared_app_runs_nothing_and_writes_no_status(
    edge_backup, machine, arguments, program
):
    """#238 matrix cell 7."""
    outcome = edge_backup(*arguments)

    assert (outcome.exit_code, outcome.err) == (
        3,
        f"error: {program}: fehlgeschlagen: nobody ist nicht deklariert\n",
    )
    assert machine.calls == []
    assert outcome.status is None


TARGETLESS = "--test restores to a throwaway, so --target does not apply"
STANDALONE = "--init and --unlock stand alone, without an app, --pre-apply or --json"


@pytest.mark.parametrize(
    ("arguments", "complaint"),
    [
        (("backup", "--pre-apply"), "--pre-apply needs the name of an app"),
        (("backup", "--json"), "--json needs --pre-apply"),
        (("backup", "radio", "--json"), "--json needs --pre-apply"),
        (
            ("backup", "--init", "radio"),
            STANDALONE,
        ),
        (
            ("backup", "--init", "--unlock"),
            STANDALONE,
        ),
        (
            ("backup", "--unlock", "--pre-apply", "radio"),
            STANDALONE,
        ),
        (
            ("backup", "--init", "--json"),
            STANDALONE,
        ),
        (("restore", "--test", "--snapshot", "0" * 12), "--snapshot needs the name of an app"),
        (("restore", "--test", "--snapshot", ""), "--snapshot needs the name of an app"),
        (
            ("restore", "--test", "--target", "somewhere"),
            TARGETLESS,
        ),
        (("restore", "--list", "radio"), "--list stands alone"),
        (("restore", "--list", "--test"), "--list stands alone"),
        (("restore",), "restore needs the name of an app and --target"),
        (("restore", "radio"), "restore needs the name of an app and --target"),
        (("restore", "--target", "somewhere"), "restore needs the name of an app and --target"),
    ],
)
def test_a_wrong_flag_combination_is_a_usage_error_that_runs_nothing(
    edge_backup, machine, arguments, complaint
):
    outcome = edge_backup(*arguments)

    program = f"hal {arguments[0]}"
    assert outcome.exit_code == 2
    assert outcome.err.splitlines() == [f"error: {complaint}", f"next: {program} --help"]
    assert machine.calls == []
    assert outcome.status is None


def _terminated_by_systemd():
    """What `systemctl stop` does to the run: SIGTERM, which must reach the run as Ctrl-C."""
    assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler, "SIGTERM would kill"
    os.kill(os.getpid(), signal.SIGTERM)


@pytest.mark.parametrize(
    ("command", "label"),
    [
        (("backup",), "pg_dump"),
        (("backup", "radio", "--pre-apply", "--json"), "restic backup"),
        (("restore", "--test"), "pg_restore"),
        (("restore", "--test", "radio", "--snapshot", "latest"), "pg_restore"),
        (("backup", "--init"), "restic init"),
        (("backup", "--unlock"), "restic unlock"),
        (("restore", "--list"), "restic snapshots"),
        (("restore", "radio", "--target", "restored"), "restic restore"),
    ],
    ids=" ".join,
)
def test_a_failed_step_of_any_verb_exits_3_with_one_error_line_and_frees_the_lock(
    edge_backup, machine, home, monkeypatch, command, label
):
    monkeypatch.chdir(home)
    edge_backup(*declare_two_apps(home), "backup")
    machine.failing[label] = 1
    snapshot = machine.snapshots[-1]["id"]
    command = [snapshot if part == "latest" else part for part in command]

    outcome = edge_backup(*declare_two_apps(home), *command)

    program = f"hal {command[0]}"
    assert outcome.exit_code == 3
    [line] = outcome.err.splitlines()
    assert line.startswith(f"error: {program}: fehlgeschlagen: ")
    assert outcome.out == ""
    assert machine._host_lock_is_free()


@pytest.mark.parametrize(
    ("command", "label"),
    [
        (("backup",), "pg_dump"),
        (("backup", "radio", "--pre-apply", "--json"), "pg_dump"),
        (("restore", "--test"), "pg_restore"),
        (("backup", "--init"), "restic init"),
        (("backup", "--unlock"), "restic unlock"),
        (("restore", "--list"), "restic snapshots"),
        (("restore", "songmaker", "--target", "restored"), "restic restore"),
    ],
    ids=" ".join,
)
def test_sigterm_ends_a_run_as_an_interruption_that_cleans_up_and_frees_the_lock(
    edge_backup, machine, home, monkeypatch, command, label
):
    monkeypatch.chdir(home)
    edge_backup(*declare_two_apps(home), "backup")
    sigterm_before = signal.getsignal(signal.SIGTERM)
    machine.failing[label] = _terminated_by_systemd

    outcome = edge_backup(*declare_two_apps(home), *command)

    assert outcome.exit_code == 130
    assert signal.getsignal(signal.SIGTERM) is sigterm_before
    assert machine._host_lock_is_free()
    assert no_restore_server_left(machine)
    assert list((home / ".cache/edge-backup").iterdir()) == []
    if label in {"pg_dump", "pg_restore"}:  # the verbs that record their outcome
        assert outcome.status["state"] == "failed"
        assert outcome.status["exit_category"] == "interrupted"
        assert outcome.err.endswith("fehlgeschlagen: abgebrochen\n")
