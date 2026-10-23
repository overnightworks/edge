"""`home check`'s platform check (overnightworks/edge#340 C1-C3, C6, S1, S4, R1, Z2, Z3) against a fake docker and a reboot flag file."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from home_tools import platform_check

NOW = datetime(2026, 10, 23, 12, 0)
CANARY = "CANARY-ENV-VALUE-s3cr3t"


class FakeDocker:
    """Answers `docker ps` and one `docker inspect --format` call; an inspect with a field other than the named ones leaks the canary."""

    def __init__(self) -> None:
        self.containers: list[tuple[str, str, str, int, str]] = []  # name, status, health, exit code, compose project
        self.unreadable = False
        self.inspect_calls: list[list[str]] = []

    def add(self, name: str, status: str = "running", health: str = "", exit_code: int = 0, project: str = "edge") -> None:
        self.containers.append((name, status, health, exit_code, project))

    def __call__(self, argv: Sequence[str]) -> str | None:
        if self.unreadable:
            return None
        if argv[:2] == ["docker", "ps"]:
            return "".join(f"id{i}\n" for i in range(len(self.containers)))
        self.inspect_calls.append(list(argv))
        if "--format" not in argv:
            return f'[{{"Config":{{"Env":["TOKEN={CANARY}"]}}}}]'
        return "".join(f"/{n}\t{s}\t{h}\t{c}\t{p}\n" for n, s, h, c, p in (self.containers[int(i.removeprefix("id"))] for i in argv[4:]))


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def reboot_file(tmp_path: Path) -> Path:
    return tmp_path / "reboot-required"


def found(docker: FakeDocker, reboot_file: Path) -> dict[tuple[str, str], str]:
    findings, _report = platform_check.inspect(docker, NOW, reboot_file)
    return {(f.rule, f.subject): f.detail for f in findings}


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
def test_a_broken_compose_container_is_a_finding_named_by_its_state(docker, reboot_file, status, health, exit_code, rule) -> None:
    docker.add("edge-step-ca-1", status, health, exit_code)

    assert set(found(docker, reboot_file)) == {(rule, "edge-step-ca-1")}


@pytest.mark.parametrize(
    ("status", "health", "exit_code", "project"),
    [
        ("running", "healthy", 0, "edge"),
        ("running", "starting", 0, "edge"),
        ("running", "", 0, "edge"),
        ("exited", "", 0, "edge"),
        ("created", "", 0, "edge"),
        ("exited", "", 1, ""),
        ("running", "unhealthy", 0, ""),
    ],
)
def test_a_container_that_is_fine_or_outside_compose_is_no_finding(docker, reboot_file, status, health, exit_code, project) -> None:
    docker.add("some-container", status, health, exit_code, project)

    assert found(docker, reboot_file) == {}


def test_a_machine_without_containers_is_clean(docker, reboot_file) -> None:
    assert found(docker, reboot_file) == {}


def test_docker_that_cannot_be_read_is_a_finding_never_a_pass(docker, reboot_file) -> None:
    docker.unreadable = True

    assert set(found(docker, reboot_file)) == {("docker-unreadable", "docker")}


def test_many_containers_are_inspected_in_one_call_and_healthy_ones_appear_only_as_a_count(docker, reboot_file) -> None:
    for number in range(5):
        docker.add(f"edge-service-{number}-1", "running", "healthy")
    docker.add("edge-broken-1", "running", "unhealthy")

    findings, report = platform_check.inspect(docker, NOW, reboot_file)

    assert len(docker.inspect_calls) == 1
    assert [f.subject for f in findings] == ["edge-broken-1"]
    assert "edge-service-0-1" not in report
    assert "davon in Ordnung 5" in report


def test_the_inspect_asks_only_for_named_fields_and_the_environment_never_reaches_the_report(docker, reboot_file) -> None:
    docker.add("edge-step-ca-1", "running", "unhealthy")

    _findings, report = platform_check.inspect(docker, NOW, reboot_file)

    assert "--format" in docker.inspect_calls[0]
    assert "Env" not in docker.inspect_calls[0][3]
    assert CANARY not in report


def test_a_pending_reboot_is_a_finding_with_the_date_the_flag_appeared(docker, reboot_file) -> None:
    reboot_file.write_text(CANARY)
    flagged = datetime(2026, 10, 16, 8, 0).timestamp()
    os.utime(reboot_file, (flagged, flagged))

    findings, report = platform_check.inspect(docker, NOW, reboot_file)

    assert [(f.rule, f.subject) for f in findings] == [("reboot-pending", "reboot")]
    assert "seit 16.10.2026" in findings[0].detail
    assert CANARY not in report


def test_the_reboot_finding_keeps_its_identity_as_the_flag_ages(docker, reboot_file) -> None:
    reboot_file.touch()
    first, _ = platform_check.inspect(docker, NOW, reboot_file)
    os.utime(reboot_file, (1.0, 1.0))
    later, _ = platform_check.inspect(docker, NOW, reboot_file)

    assert [f.identity for f in first] == [f.identity for f in later]
