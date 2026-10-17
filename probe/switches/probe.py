#!/usr/bin/env python3
"""Probe of the ingress barrier of `edge stop` (#61 probe cells 1-7): a throwaway compose project,
HOME and copy of the repository.

It starts the real Traefik, socket proxy and the `hello` whoami of compose.yaml under their own
project name, drives the real `edge stop`, `edge start` and `edge apply --closed` of a copy, and
checks that `hello.<domain>` answers 404 with `X-Edge-Barrier: probe` on 127.0.0.1:8081 behind
the barrier, also after a Traefik restart with the container up. It always removes its
containers, networks and volumes. Cell 8 (#70) drives `edge stop --all` and `edge start --all` on a
throwaway `systemctl --user` unit `edge-probe-tunnel-<random>.service` (`sleep infinity`) that it
removes again, with the real boot drop-in of `tunnel/`: a start while the global marker exists is
skipped (#76 cells 7-8); the real `cloudflared.service` is never named. It never touches the
live stack or the live state. It needs 127.0.0.1:8081 free, so the live Traefik is down while it
runs.

    flock /tmp/probe-stack.lock timeout --kill-after=30 900 \\
        python3 probe/switches/probe.py --scratch DIR

Needs an empty or new --scratch directory, and `uv`: it runs `uv sync` in the copy.
"""

from __future__ import annotations

import argparse
import grp
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "probe"))  # the helper of the probes, importable by path too
sys.path.insert(0, str(REPO / "src"))  # edge's own facts; its modules need only the stdlib

import edge_copy  # noqa: E402
from edge_copy import ProbeFailed, expect  # noqa: E402

from edge import checkout, tunnel  # noqa: E402

PROJECT = "edge-switches-probe"
UNIT = "probe"
DOMAIN = "probe.invalid"
HOST = f"hello.{DOMAIN}"
EDGE_ADDRESS = ("127.0.0.1", 8081)
FLEET = "cloudflare:\ngithub:\nservices:\n  probe:\n    run:\n      enabled: true\n"
NOT_FOUND = (404, b"404 page not found\n", UNIT)  # status, body, X-Edge-Barrier
WHOAMI_SEEN = b"Hostname:"
HELLO_ANSWERING = "hello answering"
NOTHING_TO_CHANGE = "nothing to change"
READY_SECONDS = 90
BEHIND_BARRIER_SECONDS = 10
REQUEST_SECONDS = 3
POLL_SECONDS = 0.2
COMPOSE_SECONDS = 300
NOT_RUNNABLE = 3
SYSTEMCTL_SECONDS = 60
BOOT_DROP_IN_SOURCE = REPO / "tunnel" / f"{tunnel.CLOUDFLARED_UNIT}.d" / tunnel.BOOT_DROP_IN
TUNNEL_UNIT_TEXT = "[Service]\nExecStart=/usr/bin/sleep infinity\n"


class Probe:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.home = scratch / "home"
        self.home.mkdir(parents=True)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "COMPOSE_PROJECT_NAME": PROJECT,
            "EDGE_DOMAIN": DOMAIN,
            "DOCKER_GID": str(grp.getgrnam("docker").gr_gid),
        }
        self.environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
        self.copy = checkout.live_checkout(self.home)
        self.marker = self.home / ".local" / "state" / "edge" / "stopped" / UNIT
        self.barrier = self.copy / "dynamic" / f"switch-{UNIT}.yml"

    # --- the stack ---------------------------------------------------------------------------

    def compose(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["docker", "compose", *arguments], capture_output=True, env=self.environment,
            cwd=self.copy if self.copy.exists() else REPO, check=False, timeout=COMPOSE_SECONDS,
        )  # fmt: skip
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
            raise ProbeFailed(
                f"docker compose {arguments[0]} exited {result.returncode}: {tail[-1:]}"
            )
        return result

    def hello_state(self) -> str:
        containers = self.compose("--profile", "probe", "ps", "--all", "--format", "json", "hello")
        lines = containers.stdout.decode().strip()
        states = [json.loads(line)["State"] for line in lines.splitlines()]
        return states[0] if states else "absent"

    def edge(self, *arguments: str, terminal: bool, exit_code: int = 0) -> str:
        return edge_copy.run_edge(
            self.copy, self.environment, *arguments, terminal=terminal, exit_code=exit_code
        )

    def teardown(self) -> None:
        self.compose("--profile", "*", "down", "--volumes", "--remove-orphans")

    # --- what a client of the tunnel sees ----------------------------------------------------

    def ask(self) -> tuple[int, bytes, str | None] | None:
        """`GET /` with the unit's Host on the edge entrypoint: status, body and the barrier mark;
        None when nothing listens."""
        connection = http.client.HTTPConnection(*EDGE_ADDRESS, timeout=REQUEST_SECONDS)
        try:
            connection.request("GET", "/", headers={"Host": HOST})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("X-Edge-Barrier")
        except OSError:
            return None
        finally:
            connection.close()

    def wait_until(self, what: str, reached, seconds: int = READY_SECONDS) -> None:
        deadline = time.monotonic() + seconds
        while not reached():
            if time.monotonic() > deadline:
                raise ProbeFailed(
                    f"{what} did not happen within {seconds} s (last answer {self.ask()})"
                )
            time.sleep(POLL_SECONDS)

    def serves_whoami(self) -> bool:
        answer = self.ask()
        return bool(answer) and answer[0] == 200 and WHOAMI_SEEN in answer[1]

    def answers_the_barrier(self) -> bool:
        return self.ask() == NOT_FOUND

    def stays_behind_the_barrier(self, seconds: int, *, may_not_listen_yet: bool = False) -> int:
        """Every request for `seconds` answers the barrier's 404, from the first one; the number of
        requests. With `may_not_listen_yet` the nothing-listens answers before the first answer
        do not count, and the window starts at that first answer."""
        asked = 0
        waiting_since = time.monotonic()
        end = waiting_since + seconds
        while time.monotonic() < end:
            answer = self.ask()
            if answer is None and may_not_listen_yet:
                expect(time.monotonic() - waiting_since < READY_SECONDS, "traefik never listened")
                end = time.monotonic() + seconds
                time.sleep(POLL_SECONDS)
                continue
            expect(answer == NOT_FOUND, f"a request behind the barrier answered {answer}")
            may_not_listen_yet = False
            asked += 1
            time.sleep(POLL_SECONDS)
        return asked

    # --- the cells ---------------------------------------------------------------------------

    def bring_up(self) -> None:
        self.copy = edge_copy.repository_copy(REPO, self.home)
        (self.copy / "fleet.yaml").write_text(FLEET)
        self.compose("up", "--detach", "socket-proxy", "traefik")
        self.edge("apply", "--closed", terminal=True)  # starts hello

    def cell_1_control(self) -> None:
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"proof 1 held: {HOST} answers whoami without a switch")

    def cell_2_stop(self) -> None:
        self.edge("stop", UNIT, terminal=False)
        expect(self.barrier.is_file(), "stop wrote no dynamic/switch-probe.yml")
        expect(self.marker.is_file(), "stop wrote no marker")
        expect(self.answers_the_barrier(), f"{HOST} answers {self.ask()} after stop")
        expect(self.hello_state() == "exited", f"hello is {self.hello_state()} after stop")
        print("proof 2 held: stop exits 0, wrote the barrier and the marker, 404, hello exited")

    def cell_3_up_behind_the_barrier(self) -> None:
        self.compose("--profile", UNIT, "up", "--detach", "hello")
        asked = self.stays_behind_the_barrier(3)
        expect(self.hello_state() == "running", "hello is not running")
        print(f"proof 3 held: hello up behind the barrier, {asked} requests answered 404")

    def cell_4_traefik_restart(self) -> None:
        expect(self.hello_state() == "running", "hello is not running before the restart")
        self.compose("restart", "traefik")
        asked = self.stays_behind_the_barrier(BEHIND_BARRIER_SECONDS, may_not_listen_yet=True)
        expect(self.hello_state() == "running", "hello stopped across the restart")
        print(f"proof 4 held: after restarting traefik {asked} requests in 10 s all answered 404")

    def cell_5_apply(self) -> None:
        out = self.edge("apply", "--closed", terminal=True)
        expect(
            "probe stays stopped" in out and NOTHING_TO_CHANGE in out, f"apply said: {out[-160:]}"
        )
        expect(self.hello_state() == "running", "apply stopped hello")
        expect(self.barrier.is_file() and self.answers_the_barrier(), "apply lifted the barrier")
        print("proof 5 held: apply says stays stopped / nothing to change; barrier and hello stay")

    def cell_6_start(self) -> None:
        self.edge("start", UNIT, terminal=True)
        expect(not self.barrier.exists(), "start left the barrier")
        expect(not self.marker.exists(), "start left the marker")
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"proof 6 held: start removed barrier and marker; {HOST} answers whoami")

    def cell_8_global_stop(self) -> None:
        throwaway = ThrowawayTunnel(self.home)
        self.environment["EDGE_TUNNEL_UNIT"] = throwaway.name
        self.environment["EDGE_TUNNEL_MANAGER"] = "user"
        global_marker = self.home / ".local" / "state" / "edge" / "global-stop"
        try:
            self.edge(
                "stop", UNIT, terminal=False
            )  # the unit barrier to be proven before reopening
            throwaway.install()
            stopped = self.edge("stop", "--all", terminal=False)
            expect("the tunnel is stopped" in stopped, f"stop --all said: {stopped[-160:]}")
            expect(
                throwaway.state() == "inactive",
                f"the tunnel is {throwaway.state()} after stop --all",
            )
            expect(global_marker.is_file(), "stop --all wrote no global marker")
            throwaway.start_is_skipped()
            self.barrier.unlink()  # start --all renders the barrier again before it proves it
            started = self.edge("start", "--all", terminal=True)
            expect(
                f"the barrier of {UNIT} is proven" in started, f"start --all said: {started[-200:]}"
            )
            expect(
                throwaway.state() == "active",
                f"the tunnel is {throwaway.state()} after start --all",
            )
            expect(not global_marker.exists(), "start --all left the global marker")
            expect(self.marker.is_file() and self.answers_the_barrier(), "the unit switch fell")
        finally:
            throwaway.remove()
            self.environment.pop("EDGE_TUNNEL_UNIT")
            self.environment.pop("EDGE_TUNNEL_MANAGER")
        self.edge("start", UNIT, terminal=True)
        print(
            "PROOF 8 held: stop --all left the throwaway unit inactive with the marker, and its "
            "boot drop-in skipped a start; start --all proved the unit barrier, reopened it "
            "and cleared only the marker"
        )

    def cell_7_traefik_down(self) -> None:
        self.compose("stop", "traefik")
        out = self.edge("stop", UNIT, terminal=False, exit_code=1)
        expect("partial" in out, f"the stop without traefik said: {out[-160:]}")
        expect(self.hello_state() == "exited", "hello still runs after the partial stop")
        expect(self.marker.is_file() and self.barrier.is_file(), "marker or barrier missing")
        self.compose("up", "--detach", "traefik")
        self.wait_until("the barrier answering", self.answers_the_barrier)
        self.edge("start", UNIT, terminal=True)
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print("proof 7 held: traefik down: stop exits 1 partial, both kept; start reopens")


class ThrowawayTunnel:
    """A user unit that stands in for the tunnel, in the user manager's runtime directory (a tmpfs
    that is gone after a logout), with the real boot drop-in of `tunnel/` rendered for the
    probe's HOME (#76 cells 7-8)."""

    def __init__(self, home: Path) -> None:
        self.name = f"edge-probe-tunnel-{secrets.token_hex(4)}.service"
        runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / "user"
        self.unit_file = runtime / self.name
        self.drop_in = runtime / f"{self.name}.d" / tunnel.BOOT_DROP_IN
        self.drop_in_text = BOOT_DROP_IN_SOURCE.read_text().replace("@HOME@", str(home))

    def systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *arguments], capture_output=True, text=True, check=False,
            timeout=SYSTEMCTL_SECONDS,
        )  # fmt: skip

    def install(self) -> None:
        self.drop_in.parent.mkdir(parents=True)
        self.unit_file.write_text(TUNNEL_UNIT_TEXT)
        self.drop_in.write_text(self.drop_in_text)
        self.systemctl("daemon-reload")
        expect(
            self.systemctl("start", self.name).returncode == 0, "the throwaway unit did not start"
        )
        expect(self.state() == "active", f"without the marker the unit is {self.state()}")

    def start_is_skipped(self) -> None:
        """With the global marker present a start, as at boot, leaves the unit off."""
        started = self.systemctl("start", self.name)
        expect(started.returncode == 0, f"the skipped start failed: {started.stderr.strip()}")
        expect(self.state() == "inactive", f"with the marker the unit is {self.state()}")
        condition = self.systemctl("show", "--property=ConditionResult", "--value", self.name)
        expect(condition.stdout.strip() == "no", "the boot drop-in's condition was not checked")

    def state(self) -> str:
        return self.systemctl("is-active", self.name).stdout.strip()

    def remove(self) -> None:
        self.systemctl("stop", self.name)
        self.drop_in.unlink(missing_ok=True)
        if self.drop_in.parent.exists():
            self.drop_in.parent.rmdir()
        self.unit_file.unlink(missing_ok=True)
        self.systemctl("daemon-reload")


def port_is_busy() -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(EDGE_ADDRESS) == 0


def raise_interrupt(_signal: int, _frame: object) -> None:
    raise KeyboardInterrupt


def prove(probe: Probe) -> int:
    try:
        probe.bring_up()
        probe.cell_1_control()
        probe.cell_2_stop()
        probe.cell_3_up_behind_the_barrier()
        probe.cell_4_traefik_restart()
        probe.cell_5_apply()
        probe.cell_6_start()
        probe.cell_7_traefik_down()
        probe.cell_8_global_stop()
    except (ProbeFailed, subprocess.TimeoutExpired, OSError) as failure:
        print(f"PROBE FAILED: {failure}")
        return 1
    except KeyboardInterrupt:
        print("PROBE FAILED: interrupted")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.scratch.exists() and any(arguments.scratch.iterdir()):
        print("PROBE FAILED: --scratch is not empty")
        return NOT_RUNNABLE
    if port_is_busy():
        print("PROBE FAILED: 127.0.0.1:8081 is in use, the live edge must be down")
        return NOT_RUNNABLE
    arguments.scratch.mkdir(parents=True, exist_ok=True)
    probe = Probe(arguments.scratch)
    signal.signal(signal.SIGTERM, raise_interrupt)  # `timeout` ends the probe with SIGTERM
    exit_code = prove(probe)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        probe.teardown()
    except (ProbeFailed, subprocess.TimeoutExpired) as failure:
        print(f"PROBE FAILED: teardown, {failure}")
        exit_code = 1
    if exit_code == 0:
        print("PROBE PASSED: cells 1-8 hold")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
