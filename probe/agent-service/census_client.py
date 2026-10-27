"""The host side of `tree_census.py` (#339): sends its source and one settings line to
`python -I -` inside the service container and returns the parsed records. The HMAC key is made
once per client, so one opaque name means the same thing in every answer; a raw name never leaves
the container."""

from __future__ import annotations

import json
import secrets
import tomllib
from pathlib import Path
from typing import NamedTuple

import tree_census
from egress import Probe, ProbeFailed, expect

SERVICE_UID = 1000  # `user: "1000:1000"` of the service in compose.yaml
SAMPLE_INTERVAL = 0.25


class Process(NamedTuple):
    pid: int
    ppid: int
    pgid: int
    sid: int
    name: str  # as `tree_census.Names` shows it


class CensusClient:
    def __init__(self, probe: Probe, container: str, configuration: Path) -> None:
        settings = tomllib.loads(configuration.read_text(encoding="utf-8"))
        self.working_root = settings["cli"]["working_root"]
        self.token_file = settings["profiles"]["claude"]["token_file"]
        self.probe = probe
        self.container = container
        self.key = secrets.token_bytes(16)
        self.names = tree_census.Names(self.key, SERVICE_UID)

    @property
    def sleep(self) -> str:
        """The opaque name the census shows for `sleep`."""
        return self.names.show("sleep")

    def processes(self, duration: float = 0) -> tuple[Process, ...]:
        """Every distinct process seen in a window of `duration` seconds; 0 is one pass."""
        report = self.ask({
            "mode": "sample", "proc": "/proc", "duration": duration,
            "interval": SAMPLE_INTERVAL, "group_leader": 0,
        })  # fmt: skip
        return tuple(Process(*record) for record in report["processes"])

    def listed(self, *mounts: str) -> tuple[int, ...]:
        """How many entries the census lists below each writable mount (a path), in order."""
        report = self.ask({"mode": "census", "token_file": self.token_file, "harness_owned": []})
        counts = []
        for mount in mounts:
            shown = self.names.show_mount(mount)
            found = [entry["listings"] for entry in report["mounts"] if entry["mount"] == shown]
            expect(len(found) == 1, f"the census shows {len(found)} writable mounts for one asked")
            counts.append(len(found[0]))
        return tuple(counts)

    def ask(self, settings: dict) -> dict:
        full = json.dumps({"key": self.key.hex(), **settings})
        program = Path(tree_census.__file__).read_text(encoding="utf-8") + f"\nemit({full!r})\n"
        report = self.probe.client_with(["exec", "-i", self.container], (), program)
        if "error" in report:
            raise ProbeFailed(f"the census failed in {settings['mode']} mode")
        return report
