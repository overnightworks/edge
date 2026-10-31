"""`hal check`'s exposure area: what this PC lets in from beyond loopback.

`hal` runs the scan and hands its findings down as values, so this package never imports `hal.exposure`.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum

from hal.workstation.report import Finding, render_findings_report


class ExposureRule(StrEnum):
    REACHABLE_BEYOND_LOOPBACK = "reachable-beyond-loopback"
    ROUTING_NAME_UNUSABLE = "routing-name-unusable"


ExposureReader = Callable[[], Sequence[Finding]]
"""Scans the PC and returns one finding per process, container or routing name; raises when the scan cannot run."""


def inspect(read: ExposureReader, now: datetime) -> tuple[list[Finding], str]:
    findings = list(read())
    return findings, render_findings_report("exposure", now, [], findings)
