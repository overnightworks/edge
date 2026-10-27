"""`home check`'s platform check: reports every broken compose container, a pending reboot and a nearly full disk.

It asks Docker for named fields only (name, state, health, exit code, the compose project label), never
any other label or the full inspect JSON, which holds each container's environment, and never a log. From the reboot
flag it reads whether the file exists and when it was changed; from the disk of `/` its total and free bytes.
Nothing is started, stopped or repaired. Docker that
cannot be read is a finding of its own, never a pass.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from home_tools.report import Finding, render_findings_report
from home_tools.tidy_home import CommandRunner

REBOOT_REQUIRED_FILE = Path("/var/run/reboot-required")
CHECKED_FILESYSTEM = Path("/")
MINIMUM_FREE_SHARE = 0.10
BYTES_PER_GIGABYTE = 1024**3
DOCKER_CONTAINER_IDS = ["docker", "ps", "-aq"]
INSPECT_FORMAT = (
    '{{.Name}}\t{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}\t{{.State.ExitCode}}'
    '\t{{index .Config.Labels "com.docker.compose.project"}}'
)
BROKEN_STATES = {"restarting": "container-restarting", "dead": "container-dead"}


@dataclass(frozen=True)
class ContainerState:
    name: str
    status: str
    health: str
    exit_code: int
    compose_project: str  # empty when the container belongs to none


@dataclass(frozen=True)
class DiskSpace:
    total: int  # bytes
    free: int  # bytes an unprivileged process can still use

    @property
    def free_share(self) -> float:
        return self.free / self.total

    def describe(self) -> str:
        return f"{self.free // BYTES_PER_GIGABYTE} GB von {self.total // BYTES_PER_GIGABYTE} GB frei ({self.free * 100 // self.total} %)"


DiskSpaceReader = Callable[[Path], DiskSpace]


def read_disk_space(path: Path) -> DiskSpace:
    usage = shutil.disk_usage(path)
    return DiskSpace(usage.total, usage.free)


def _parse_container(line: str) -> ContainerState:
    name, status, health, exit_code, compose_project = line.split("\t")
    return ContainerState(name.removeprefix("/"), status, health, int(exit_code), compose_project)


def _container_finding(container: ContainerState) -> Finding | None:
    if container.status in BROKEN_STATES:
        return Finding(BROKEN_STATES[container.status], container.compose_project, f"{container.name}: Zustand {container.status}")
    if container.status == "exited" and container.exit_code != 0:
        return Finding("container-exited", container.compose_project, f"{container.name}: beendet mit Code {container.exit_code}")
    if container.status == "running" and container.health == "unhealthy":
        return Finding("container-unhealthy", container.compose_project, f"{container.name}: Gesundheitsprüfung schlägt fehl")
    return None


def _inspect_all(run: CommandRunner) -> list[ContainerState] | None:
    """One `docker inspect` over every id; a container removed in between is retried once against a fresh listing."""
    for _attempt in range(2):
        identifiers = run(DOCKER_CONTAINER_IDS)
        if identifiers is None:
            return None
        if not identifiers.split():
            return []
        listing = run(["docker", "inspect", "--format", INSPECT_FORMAT, *identifiers.split()])
        if listing is not None:
            return [_parse_container(line) for line in listing.splitlines() if line]
    return None


def _container_findings(run: CommandRunner) -> tuple[list[Finding], list[str]]:
    containers = _inspect_all(run)
    if containers is None:
        return [Finding("docker-unreadable", "docker", "`docker ps` oder `docker inspect` lässt sich nicht ausführen")], []
    judged = [container for container in containers if container.compose_project]
    findings = [finding for container in judged if (finding := _container_finding(container))]
    return findings, [f"Container mit Compose-Projekt: {len(judged)}, davon in Ordnung {len(judged) - len(findings)}"]


def _reboot_findings(reboot_file: Path) -> tuple[list[Finding], list[str]]:
    """Only the file's existence and modification time are read, never its content."""
    try:
        since = datetime.fromtimestamp(reboot_file.stat().st_mtime).astimezone()
    except FileNotFoundError:
        return [], ["Neustart nicht nötig"]
    return [Finding("reboot-pending", "reboot", f"Neustart nötig seit {since:%d.%m.%Y}")], []


def _disk_findings(disk_space: DiskSpaceReader) -> tuple[list[Finding], list[str]]:
    """An unreadable disk is not caught: it breaks the platform check, which the check reports as broken."""
    space = disk_space(CHECKED_FILESYSTEM)
    status = f"Festplatte {CHECKED_FILESYSTEM}: {space.describe()}"
    if space.free_share < MINIMUM_FREE_SHARE:
        return [Finding("disk-low", str(CHECKED_FILESYSTEM), space.describe())], [status]
    return [], [status]


def inspect(run: CommandRunner, now: datetime, reboot_file: Path, disk_space: DiskSpaceReader) -> tuple[list[Finding], str]:
    """What `home check` reads of the platform; findings and their report."""
    container_findings, container_lines = _container_findings(run)
    reboot_findings, reboot_lines = _reboot_findings(reboot_file)
    disk_findings, disk_lines = _disk_findings(disk_space)
    findings = container_findings + reboot_findings + disk_findings
    return findings, render_findings_report("platform", now, container_lines + reboot_lines + disk_lines, findings)
