"""The one readiness budget a run spends on Docker and the database together."""

from hal.backup.settings import READINESS_POLL_SECONDS, READINESS_TIMEOUT


def test_backup_waits_at_most_the_readiness_budget_for_docker_and_the_database_together(
    edge_backup, machine
):
    machine.daemon_down_polls = 36
    machine.absent_polls = 10_000

    outcome = edge_backup("backup")

    assert outcome.status["exit_category"] == "not-ready"
    assert machine.clock.elapsed <= READINESS_TIMEOUT.total_seconds() + READINESS_POLL_SECONDS
