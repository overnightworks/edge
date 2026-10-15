"""What a restore may take, and what a run must check before it stages anything."""

import pytest
from edge_backup_support import DUMP, Machine

from edge_backup.settings import POSTGRES_IMAGE, RESTIC_IMAGE


def _leave_a_partial_snapshot(machine: Machine) -> str:
    """A failed run's restic exit 3 leaves a snapshot that no successful backup recorded."""
    partial = {"id": "a973e990" * 8, "db": {"db.dump": b"partial"}, "audio": {}}
    machine.snapshots.append(partial)
    return partial["id"]


def test_restore_takes_the_last_successful_backups_snapshot_not_a_newer_partial_one(
    edge_backup, machine, home
):
    edge_backup("backup")
    good = machine.snapshots[-1]["id"]
    _leave_a_partial_snapshot(machine)
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--target", str(target))

    assert outcome.exit_code == 0, outcome.err
    assert good[:8] in outcome.out
    assert (target / "db/db.dump").read_bytes() == DUMP


def _forget_the_recorded_snapshot(machine: Machine) -> None:
    machine.snapshots.pop()


def _lose_the_status(machine: Machine) -> None:
    machine.status_file.unlink()


@pytest.mark.parametrize("break_history", [_forget_the_recorded_snapshot, _lose_the_status])
def test_restore_refuses_without_the_last_successful_snapshot_and_names_the_newest(
    edge_backup, machine, home, break_history
):
    edge_backup("backup")
    edge_backup("backup")
    break_history(machine)
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--target", str(target))

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert "letzten erfolgreichen Backups" in line
    assert f"der neueste Snapshot ist {machine.snapshots[-1]['id'][:8]}" in line
    assert not target.exists()


@pytest.mark.parametrize("image", [RESTIC_IMAGE, POSTGRES_IMAGE], ids=["restic", "postgres"])
@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_missing_pinned_image_fails_early_naming_it_and_how_to_pull_it(
    edge_backup, machine, home, command, image
):
    machine.missing_images = {image}

    outcome = edge_backup(command)

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert f"docker pull {image}" in line
    assert set(machine.labels()) == {"docker info", "docker image inspect"}
    assert outcome.status["exit_category"] == "configuration"
    assert (
        not (home / ".cache/edge-backup").exists()
        or list((home / ".cache/edge-backup").iterdir()) == []
    )


@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_daemon_that_is_still_starting_is_waited_for_before_the_images_are_checked(
    edge_backup, machine, command
):
    edge_backup("backup")
    polls_before = machine.labels().count("docker info")
    machine.daemon_down_polls = 3

    outcome = edge_backup(command)

    assert outcome.exit_code == 0, outcome.err
    assert outcome.status["state"] == "ok"
    assert machine.labels().count("docker info") - polls_before == 4
