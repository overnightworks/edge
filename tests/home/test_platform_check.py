"""`home check`'s platform check (overnightworks/edge#340 C1-C3, C6, S1, S4, R1, Z2, Z3) against a fake docker and a reboot flag file."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from conftest import ALLOWED_PLATFORM_INSPECT_FORMAT, CANARY_INSPECT_LINE, INSPECT_CANARY, FakeDisk

from home_tools import platform_check

NOW = datetime(2026, 10, 23, 12, 0)


class FakeDocker:
    """Answers `docker ps` and `docker inspect`; an inspect with any format but the allowed one answers the canary line."""

    def __init__(self) -> None:
        self.containers: list[tuple[str, str, str, int, bool]] = []  # name, status, health, exit code, in a compose project (the project is called edge)
        self.unreadable = False
        self.removed_while_inspected: list[str] = []  # each inspect call takes one name: it fails and that container is gone
        self.inspect_calls: list[list[str]] = []

    def add(self, name: str, status: str = "running", health: str = "", exit_code: int = 0, in_project: bool = True) -> None:
        self.containers.append((name, status, health, exit_code, in_project))

    def __call__(self, argv: Sequence[str]) -> str | None:
        if self.unreadable:
            return None
        if argv[:2] == ["docker", "ps"]:
            return "".join(f"{container[0]}\n" for container in self.containers)
        self.inspect_calls.append(list(argv))
        if argv[3] != ALLOWED_PLATFORM_INSPECT_FORMAT:
            return CANARY_INSPECT_LINE
        if self.removed_while_inspected:
            gone = self.removed_while_inspected.pop(0)
            self.containers = [container for container in self.containers if container[0] != gone]
            return None
        listed = [container for container in self.containers if container[0] in argv[4:]]
        return "".join(f"/{n}\t{s}\t{h}\t{c}\t{'edge' if p else ''}\n" for n, s, h, c, p in listed)


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def reboot_file(tmp_path: Path) -> Path:
    return tmp_path / "reboot-required"


def rules_found(docker: FakeDocker, reboot_file: Path, disk: FakeDisk) -> set[tuple[str, str]]:
    findings, _report = platform_check.inspect(docker, NOW, reboot_file, disk)
    return {(f.rule, f.subject) for f in findings}


@pytest.mark.parametrize(
    ("status", "health", "exit_code", "rule"),
    [
        ("running", "unhealthy", 0, "container-unhealthy"),
        ("restarting", "", 1, "container-restarting"),
        ("dead", "", 0, "container-dead"),
        ("exited", "", 137, "container-exited"),
        ("exited", "", 1, "container-exited"),
    ],
)
def test_a_broken_compose_container_is_a_finding_named_by_its_state(docker, reboot_file, fake_disk, status, health, exit_code, rule) -> None:
    docker.add("edge-step-ca-1", status, health, exit_code)

    assert rules_found(docker, reboot_file, fake_disk) == {(rule, "edge")}


@pytest.mark.parametrize(
    ("status", "health", "exit_code", "in_project"),
    [
        ("running", "healthy", 0, True),
        ("running", "starting", 0, True),
        ("running", "", 0, True),
        ("exited", "", 0, True),
        ("exited", "unhealthy", 0, True),
        ("created", "", 0, True),
        ("exited", "", 1, False),
        ("running", "unhealthy", 0, False),
    ],
)
def test_a_container_that_is_fine_or_outside_compose_is_no_finding(docker, reboot_file, fake_disk, status, health, exit_code, in_project) -> None:
    docker.add("some-container", status, health, exit_code, in_project)

    assert rules_found(docker, reboot_file, fake_disk) == set()


def test_a_machine_without_containers_is_clean(docker, reboot_file, fake_disk) -> None:
    assert rules_found(docker, reboot_file, fake_disk) == set()


def test_docker_that_cannot_be_read_is_a_finding_never_a_pass(docker, reboot_file, fake_disk) -> None:
    docker.unreadable = True

    assert rules_found(docker, reboot_file, fake_disk) == {("docker-unreadable", "docker")}


def test_a_container_removed_during_the_inspection_is_no_finding_and_the_rest_is_still_judged(docker, reboot_file, fake_disk) -> None:
    docker.add("edge-job-1", "exited", "", 1)
    docker.add("edge-broken-1", "running", "unhealthy")
    docker.removed_while_inspected = ["edge-job-1"]

    assert rules_found(docker, reboot_file, fake_disk) == {("container-unhealthy", "edge")}


def test_the_only_container_removed_during_the_inspection_leaves_a_clean_machine(docker, reboot_file, fake_disk) -> None:
    docker.add("edge-job-1", "exited", "", 1)
    docker.removed_while_inspected = ["edge-job-1"]

    assert rules_found(docker, reboot_file, fake_disk) == set()


def test_two_failed_inspections_in_a_row_are_docker_unreadable(docker, reboot_file, fake_disk) -> None:
    docker.add("edge-first-1")
    docker.add("edge-second-1")
    docker.removed_while_inspected = ["edge-first-1", "edge-second-1"]

    assert rules_found(docker, reboot_file, fake_disk) == {("docker-unreadable", "docker")}


def test_many_containers_are_inspected_in_one_call_and_healthy_ones_appear_only_as_a_count(docker, reboot_file, fake_disk) -> None:
    for number in range(5):
        docker.add(f"edge-service-{number}-1", "running", "healthy")
    docker.add("edge-broken-1", "running", "unhealthy")

    findings, report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert len(docker.inspect_calls) == 1
    assert [f.subject for f in findings] == ["edge"]
    assert "edge-service-0-1" not in report
    assert "davon in Ordnung 5" in report


def test_a_wider_projection_than_the_named_fields_would_show_in_the_report_and_the_allowed_one_keeps_it_out(docker, reboot_file, fake_disk) -> None:
    docker.add("edge-step-ca-1", "running", "unhealthy")

    findings, report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert docker.inspect_calls[0][3] == ALLOWED_PLATFORM_INSPECT_FORMAT
    assert [f.subject for f in findings] == ["edge"]
    assert INSPECT_CANARY not in report


def test_the_retry_after_a_failed_inspection_asks_for_the_same_named_fields_only(docker, reboot_file, fake_disk) -> None:
    docker.add("edge-job-1", "exited", "", 1)
    docker.add("edge-step-ca-1", "running", "unhealthy")
    docker.removed_while_inspected = ["edge-job-1"]

    _findings, report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert [call[3] for call in docker.inspect_calls] == [ALLOWED_PLATFORM_INSPECT_FORMAT] * 2
    assert INSPECT_CANARY not in report


def test_a_pending_reboot_is_a_finding_with_the_date_the_flag_appeared(docker, reboot_file, fake_disk) -> None:
    reboot_file.write_text(INSPECT_CANARY)
    flagged = datetime(2026, 10, 16, 8, 0).timestamp()
    os.utime(reboot_file, (flagged, flagged))

    findings, report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert [(f.rule, f.subject) for f in findings] == [("reboot-pending", "reboot")]
    assert "seit 16.10.2026" in findings[0].detail
    assert INSPECT_CANARY not in report


def test_the_reboot_finding_keeps_its_identity_as_the_flag_ages(docker, reboot_file, fake_disk) -> None:
    reboot_file.touch()
    first, _ = platform_check.inspect(docker, NOW, reboot_file, fake_disk)
    os.utime(reboot_file, (1.0, 1.0))
    later, _ = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert [f.identity for f in first] == [f.identity for f in later]


@pytest.mark.parametrize("free_share", [0.09, 0.0], ids=["9 percent", "none left"])
def test_less_than_a_tenth_free_on_the_root_disk_is_a_finding_and_only_the_root_disk_is_asked(docker, reboot_file, fake_disk, free_share) -> None:
    fake_disk.free_share = free_share

    assert rules_found(docker, reboot_file, fake_disk) == {("disk-low", "/")}
    assert fake_disk.asked == [Path("/")]


@pytest.mark.parametrize("free_share", [0.10, 0.25], ids=["exactly 10 percent", "25 percent"])
def test_a_tenth_free_or_more_is_no_finding(docker, reboot_file, fake_disk, free_share) -> None:
    fake_disk.free_share = free_share

    assert rules_found(docker, reboot_file, fake_disk) == set()


def test_the_report_carries_the_free_and_total_gigabytes_and_the_share(docker, reboot_file, fake_disk) -> None:
    fake_disk.free_share = 0.19
    _findings, quiet_report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)
    fake_disk.free_share = 0.08
    findings, report = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert "- Festplatte /: 190 GB von 1000 GB frei (19 %)" in quiet_report
    assert "- Festplatte /: 80 GB von 1000 GB frei (8 %)" in report
    assert "| disk-low | / | 80 GB von 1000 GB frei (8 %) |" in report
    assert [f.subject for f in findings] == ["/"]


def test_the_disk_finding_keeps_its_identity_as_the_free_space_shrinks(docker, reboot_file, fake_disk) -> None:
    fake_disk.free_share = 0.09
    first, _ = platform_check.inspect(docker, NOW, reboot_file, fake_disk)
    fake_disk.free_share = 0.07
    later, _ = platform_check.inspect(docker, NOW, reboot_file, fake_disk)

    assert [f.identity for f in first] == [f.identity for f in later]
