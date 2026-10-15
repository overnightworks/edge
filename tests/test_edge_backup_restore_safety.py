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
def test_a_pruned_pinned_image_is_pulled_by_its_digest_and_the_run_goes_on(
    edge_backup, machine, command, image
):
    edge_backup("backup")
    machine.missing_images = {image}

    outcome = edge_backup(command)

    assert outcome.exit_code == 0, outcome.err
    pulls = [argv for label, argv in machine.calls if label == "docker pull"]
    assert [argv[-1] for argv in pulls] == [image]
    assert "@sha256:" in image


@pytest.mark.parametrize("command", ["backup", "restore-test"])
def test_a_pinned_image_that_cannot_be_pulled_fails_early_naming_it(
    edge_backup, machine, home, command
):
    machine.missing_images = {RESTIC_IMAGE}
    machine.failing = {"docker pull": 1}

    outcome = edge_backup(command)

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert RESTIC_IMAGE in line
    assert set(machine.labels()) == {"docker info", "docker image inspect", "docker pull"}
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


def test_restore_names_how_to_choose_the_newest_snapshot_when_none_is_recorded(
    edge_backup, machine, home
):
    edge_backup("backup")
    _lose_the_status(machine)

    outcome = edge_backup("restore", "songmaker", "--target", str(home / "restored"))

    assert "--snapshot" in outcome.err


def test_restore_takes_the_named_snapshot_on_a_machine_without_a_status_file(
    edge_backup, machine, home
):
    edge_backup("backup")
    chosen = machine.snapshots[-1]["id"]
    _lose_the_status(machine)
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--snapshot", chosen, "--target", str(target))

    assert outcome.exit_code == 0, outcome.err
    assert chosen[:8] in outcome.out
    assert (target / "db/db.dump").read_bytes() == DUMP


@pytest.mark.parametrize("prefix_length", [64, 8], ids=["full-id", "short-prefix"])
def test_restore_takes_an_older_snapshot_when_a_newer_one_exists(
    edge_backup, machine, home, prefix_length
):
    edge_backup("backup")
    edge_backup("backup")
    older, newer = machine.snapshots
    older["id"] = "b0b0b0b0" * 8
    older["db"]["db.dump"] = b"older contents"
    target = home / "restored"

    outcome = edge_backup(
        "restore", "songmaker", "--snapshot", older["id"][:prefix_length], "--target", str(target)
    )

    assert outcome.exit_code == 0, outcome.err
    assert (target / "db/db.dump").read_bytes() == b"older contents"
    assert newer["db"]["db.dump"] == DUMP


@pytest.mark.parametrize(
    "snapshot", ["f" * 64, "0" * 7, "00000000"], ids=["unknown", "too-short", "ambiguous"]
)
def test_restore_refuses_a_snapshot_that_is_not_one_single_snapshot_of_the_app(
    edge_backup, machine, home, snapshot
):
    edge_backup("backup")
    edge_backup("backup")
    target = home / "restored"

    outcome = edge_backup("restore", "songmaker", "--snapshot", snapshot, "--target", str(target))

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert "nennt keinen einzelnen Snapshot" in line
    assert not target.exists()


def test_restore_refuses_the_snapshot_of_another_app(edge_backup, machine, home):
    edge_backup("backup")
    machine.snapshots[-1]["app"] = "other"
    target = home / "restored"

    outcome = edge_backup(
        "restore", "songmaker", "--snapshot", machine.snapshots[-1]["id"], "--target", str(target)
    )

    assert outcome.exit_code == 1
    [line] = outcome.err.splitlines()
    assert "nennt keinen einzelnen Snapshot" in line
    assert not target.exists()


def test_restore_test_verifies_the_last_successful_snapshot_not_a_newer_partial_one(
    edge_backup, machine
):
    edge_backup("backup")
    _leave_a_partial_snapshot(machine)

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 0, outcome.err


def test_restore_test_refuses_without_a_recorded_successful_snapshot(edge_backup, machine):
    edge_backup("backup")
    _lose_the_status(machine)

    outcome = edge_backup("restore-test")

    assert outcome.exit_code == 1
    assert "letzten erfolgreichen Backups" in outcome.err
