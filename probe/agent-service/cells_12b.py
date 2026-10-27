#!/usr/bin/env python3
"""The login command (#457, agent-runtime#440's 12b row): the real `agent-service` of compose.yaml
in a throwaway compose project, running the login directory it is given, read-only, with the
stand-in issuer, a bootstrapped entrypoint and `max_parallel_cli_runs = 1` in a scratch
service.toml. It drives the A1d conditions of agent-runtime#440 (config, U2, U3, U6, U7, U8 and I6
against the container's own image) and the B8 S00 census of every writable mount, and prints only
verdicts, counts and role names. An agent runs it only with a dummy login made with `mktemp`.

    python3 probe/agent-service/cells_12b.py --login-directory DIR

The command holds /tmp/probe-stack.lock itself, so it runs without an outer flock. Exit 0: every
cell held, 1: a cell failed or was not driven, 2: no `--login-directory` was given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # egress.py, importable by path too

import image
from census_client import CensusClient
from containment import SERVICE_COMMAND
from egress import LABEL, expect
from issuer import ISSUER_SERVICE, IssuerProbe
from login_checks import (
    Gate,
    Login,
    LoginFailed,
    Outcome,
    Verdict,
    begin_teardown,
    check_resolved,
    describe,
    failed_command,
    gating,
    guarded,
    mount_line,
    probe_lock,
    read_login,
    run_login,
    s00_verdicts,
    scratch_parent,
    start_time_bound,
)
from service_log import BOOTSTRAP
from unit import SERVICE, ServiceConditions, login_directory

PROBE_LOCK = Path("/tmp/probe-stack.lock")  # NOSONAR(S5443) the lock every probe run shares
COMMAND_SECONDS = 1500  # the alarm: the drive stops here and the teardown still has time to run
GATEWAY = "agent-egress"
STARTED = (GATEWAY, ISSUER_SERVICE, SERVICE)
CONFIGURATION_TARGET = "/etc/agent-service/service.toml"
PARALLEL_RUNS_SETTING = "\n[runs]\nmax_parallel_cli_runs = 1\n"
LOCAL_LOGGING = {"driver": "local"}
IMAGE_OPTIONS = ("--label", LABEL, "--log-driver=none")


class LoginProbe(ServiceConditions, IssuerProbe):
    """The service with the given login bound read-only. Nothing a command or the container
    prints reaches the output: a failed command is a `LoginFailed`, and `hold` names the cell."""

    def __init__(self, scratch: Path, login: Login, gate: Gate) -> None:
        self.texts: list[str] = []  # every command's output, in memory, for S1
        self.failed: list[str] = []
        self.not_driven: list[str] = []
        self.login = login
        self.gate = gate
        super().__init__(scratch)
        text = self.configuration.read_text(encoding="utf-8")
        expect("[runs]" not in text, "service.toml has a [runs] table")
        self.configuration.write_text(text + PARALLEL_RUNS_SETTING, encoding="utf-8")
        overlay = scratch / "compose.login.yaml"
        overlay.write_text(
            json.dumps({"services": {
                SERVICE: {
                    "entrypoint": ["python", "-I", "-c", BOOTSTRAP],
                    "command": SERVICE_COMMAND,
                    "logging": LOCAL_LOGGING,
                    "volumes": [{
                        "type": "bind", "source": str(login.directory), "read_only": True,
                        "target": login_directory(), "bind": {"create_host_path": False},
                    }],
                },
                GATEWAY: {"logging": LOCAL_LOGGING},
                ISSUER_SERVICE: {"logging": LOCAL_LOGGING},
            }}),
            encoding="utf-8",
        )  # fmt: skip
        self.overlays.append(overlay)

    def services_to_start(self) -> tuple[str, ...]:
        return STARTED

    def serves(self, command: list[str]) -> bool:
        return [Path(command[0]).name, *command[1:]] == [
            "python",
            "-I",
            "-c",
            BOOTSTRAP,
            *SERVICE_COMMAND,
        ]

    def run(self, command, *, stdin=None, check=True, timeout=300):
        result = super().run(command, stdin=stdin, check=False, timeout=timeout)
        self.texts.append(result.stdout + result.stderr)
        if check and result.returncode != 0:
            raise failed_command(command, result.returncode)
        return result

    def hold(self, cell: str, how: str) -> None:
        print(f"{cell} held", flush=True)
        self.held.append(cell)

    def __enter__(self) -> LoginProbe:
        resolved = json.loads(self.compose("config", "--format", "json").stdout)
        check_resolved(
            resolved, service=SERVICE, started=STARTED,
            login=(login_directory(), self.login.directory),
            configuration=(CONFIGURATION_TARGET, self.configuration),
        )  # fmt: skip
        super().__enter__()
        return self

    def __exit__(self, *arguments: object) -> None:
        begin_teardown()  # also after a failed enter, whose signals are still armed
        super().__exit__(*arguments)

    def verdict(self, cell: str, evidence: str) -> None:
        """A cell of the command's own: its evidence is built from counts and role names."""
        print(f"{cell} held: {evidence}", flush=True)
        self.held.append(cell)

    # --- the cells ----------------------------------------------------------------------------

    def drive(self) -> None:
        if not guarded("listen", self.wait_until_listening, self.failed):
            return
        for cell, step in (
            ("config", self.cell_config), ("U2", self.cell_u2), ("U3", self.cell_u3),
            ("U6", self.cell_u6), ("U7", self.cell_u7), ("U8", self.cell_u8),
            ("I6", self.cell_i6), ("S00", self.cell_s00), ("S1", self.cell_s1),
        ):  # fmt: skip
            guarded(cell, step, self.failed)

    def cell_config(self) -> None:
        inside = self.inside("sha256", CONFIGURATION_TARGET)["sha256"]
        expect(inside == hashlib.sha256(self.configuration.read_bytes()).hexdigest(), "config")
        self.verdict(
            "config", "the service runs the scratch service.toml, max_parallel_cli_runs = 1"
        )

    def cell_i6(self) -> None:
        """The image the login container runs, in a container of its own without the login."""
        inspected = self.run(
            ["docker", "inspect", "--format", "{{.Image}}", self.container(SERVICE)]
        )
        image.cell_i6(inspected.stdout.strip(), IMAGE_OPTIONS)
        self.hold("I6", "")

    def census(self) -> CensusClient:
        return CensusClient(self, self.container(SERVICE), self.configuration)

    def cell_s00(self) -> None:
        """B8's S00 snapshot of every writable mount as an observation, then edge's two frame
        checks over it."""
        census = self.census()
        self.gate.add(census.key.hex())
        mounts = census.mounts()
        roles = {census.names.show_mount(census.working_root): "working root"}
        for shown, mount in sorted(mounts.items()):
            print(mount_line(roles.get(shown, shown), mount), flush=True)
        for verdict in s00_verdicts(mounts):
            self.record(verdict)

    def record(self, verdict: Verdict) -> None:
        if verdict.outcome is Outcome.HELD:
            self.verdict(verdict.cell, verdict.evidence)
            return
        failures = self.failed if verdict.outcome is Outcome.FAILED else self.not_driven
        failures.append(verdict.cell)
        print(f"{verdict.cell} {verdict.outcome}: {verdict.evidence}", flush=True)

    def cell_s1(self) -> None:
        """The no-token cell: the gate changed no line, and no text in memory holds the token."""
        in_log = self.container_log(SERVICE).count(self.login.token)
        in_texts = sum(text.count(self.login.token) for text in self.texts)
        counts = (
            f"{self.gate.hits} gate hits, {in_log} in the service log, "
            f"{in_texts} in all command outputs"
        )
        if self.gate.hits or in_log or in_texts:
            self.failed.append("S1")
            print(f"S1 FAILED: {counts}", flush=True)
        else:
            self.verdict("S1", counts)


def run_command(
    login: Login, lock: Path = PROBE_LOCK, environment: Mapping[str, str] = os.environ
) -> int:
    def announce() -> None:
        print("waiting for the probe lock", flush=True)

    try:
        parent = scratch_parent(login, environment)
        with probe_lock(lock, announce):
            start_time_bound(COMMAND_SECONDS)
            scratch = Path(tempfile.mkdtemp(prefix="login-probe-", dir=parent)).resolve()
            try:
                with gating([login.token]) as gate:
                    return run_login(lambda: LoginProbe(scratch, login, gate))
            finally:
                shutil.rmtree(scratch, ignore_errors=True)  # throwaway material only
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        return 1
    except Exception as problem:  # the lock, the scratch directory: nothing but the type is printed
        print(describe("setup", problem), flush=True)
        print("PROBE FAILED: setup", flush=True)
        return 1


def main(
    arguments: Sequence[str] | None = None, start: Callable[[Login], int] = run_command
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login-directory", required=True)
    try:
        login = read_login(parser.parse_args(arguments).login_directory)
    except LoginFailed as refusal:
        print(f"PROBE FAILED: {refusal}", flush=True)
        return 1
    return start(login)


if __name__ == "__main__":
    sys.exit(main())
