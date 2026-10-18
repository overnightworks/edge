#!/usr/bin/env python3
"""Probe of the ingress barrier of `edge stop` (#61 proofs 1-7), of the Traefik that follows its
`traefik.yml` (#85 cells P1-P7) and of the core that follows its compose definition (#87 cells
Q1-Q6): a throwaway compose project, HOME and copy of the repository.

It starts the real Traefik, socket proxy and the `hello` whoami of compose.yaml under their own
project name, drives the real `edge stop`, `edge start` and `edge apply --closed` of a copy, and
checks that `hello.<domain>` answers 404 with `X-Edge-Barrier: probe` on 127.0.0.1:8081 behind
the barrier, also after a Traefik restart with the container up. It always removes its
containers, networks and volumes. Proof 8 (#70) drives `edge stop --all` and `edge start --all` on
a throwaway `systemctl --user` unit `edge-probe-tunnel-<random>.service` (`sleep infinity`) that
it removes again, with the real boot drop-in of `tunnel/`, and shows #76's boot skip: a start
while the global marker exists is skipped. The real `cloudflared.service` is never named.

The #85 cells: P7 prints traefik's volumes as `docker compose config --format json` resolves them
and checks that edge's core derives exactly `traefik.yml` from them; P1 the first apply recreates
traefik and writes `applied.json`; P2 a second apply changes nothing; P3 an edited `traefik.yml`
recreates traefik, which then serves `hello`; P4 the same with `probe` switched off proves its
barrier before the success line; P5 a stopped traefik is named and not started; P6 the volumes,
the marker and the barrier survive P3-P5.

The #87 cells: Q1 prints each core container's real ps fields (Labels, Image) and `config --hash`,
after the plain `up` and after edge's own recreate, and requires label == hash after edge's
recreate (else #87 rule 8 stops the lane); Q2 a harmless label on socket-proxy in the copy's
compose.yaml recreates socket-proxy, then traefik, and `hello` answers through Traefik again; Q3 a
label on traefik alone keeps socket-proxy's container; Q4 a second apply changes nothing; Q5 with
`probe` switched off a socket-proxy change proves its barrier before the success line; Q6 the
volumes, the marker and the barrier survive Q2-Q5. Every transcript line names its item. It never
touches the live stack or the live state. It needs 127.0.0.1:8081 free, so the live Traefik is
down while it runs.

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
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "probe"))  # the helper of the probes, importable by path too
sys.path.insert(0, str(REPO / "src"))  # edge's own facts; its modules need only the stdlib

import edge_copy  # noqa: E402
from edge_copy import ProbeFailed, expect  # noqa: E402

from edge import checkout, compose, core, tunnel  # noqa: E402

PROJECT = "edge-switches-probe"
UNIT = "probe"
DOMAIN = "probe.invalid"
HOST = f"hello.{DOMAIN}"
EDGE_ADDRESS = ("127.0.0.1", 8081)
FLEET = "cloudflare:\ngithub:\nservices:\n  probe:\n    run:\n      enabled: true\n"
NOT_FOUND = (404, b"404 page not found\n", UNIT)  # status, body, X-Edge-Barrier
UNCONFIGURED = (404, b"404 page not found\n", None)  # a Traefik that has loaded no router yet
WHOAMI_SEEN = b"Hostname:"
HELLO_ANSWERING = "hello answering"
NOTHING_TO_CHANGE = "nothing to change"
TRAEFIK_FILE = "traefik.yml"
RECREATED = "traefik is recreated and runs the checked-out traefik.yml"
SOCKET_PROXY_RECREATED = "edge apply --closed: socket-proxy is recreated"
CORE_CURRENT = "the core is current"
CORE = ("socket-proxy", "traefik")
COMPOSE_FILE = "compose.yaml"
HARMLESS_EDIT = "# an edit that changes no setting\n"
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
        self.state = self.home / ".local" / "state" / "edge"
        self.marker = self.state / "stopped" / UNIT
        self.record = core.Record(self.state)
        self.volumes_before_p3: set[str] = set()
        self.volumes_before_q2: set[str] = set()
        self.barrier = self.copy / "dynamic" / f"switch-{UNIT}.yml"

    # --- the stack ---------------------------------------------------------------------------

    def docker(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            ["docker", *arguments], capture_output=True, env=self.environment,
            cwd=self.copy if self.copy.exists() else REPO, check=False, timeout=COMPOSE_SECONDS,
        )  # fmt: skip
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
            raise ProbeFailed(
                f"docker {' '.join(arguments[:2])} exited {result.returncode}: {tail[-1:]}"
            )
        return result

    def compose(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return self.docker("compose", *arguments)

    def container_ids(self) -> dict[str, str]:
        """The container id of each service that has one."""
        listed = self.compose("--profile", "*", "ps", "--all", "--format", "json").stdout.decode()
        containers = [json.loads(line) for line in listed.strip().splitlines()]
        return {container["Service"]: container["ID"] for container in containers}

    def traefik_state(self) -> str:
        listed = self.compose("ps", "--all", "--format", "json", "traefik").stdout.decode()
        states = [json.loads(line)["State"] for line in listed.strip().splitlines()]
        return states[0] if states else "absent"

    def volumes(self) -> set[str]:
        """The project's named volumes, `socket-proxy-run` among them."""
        listed = self.docker(
            "volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={PROJECT}"
        )
        return set(listed.stdout.decode().split())

    def engine(self) -> compose.Compose:
        """edge's own Compose reader, over the copy and the probe's project."""

        def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command, capture_output=True, text=True, env=self.environment, cwd=self.copy,
                check=False, timeout=timeout,
            )  # fmt: skip

        return compose.Compose(run, self.copy)

    def created(self, container_id: str) -> datetime:
        """When Docker created the container, to the microsecond."""
        inspected = self.docker("inspect", "--format", "{{.Created}}", container_id)
        return datetime.fromisoformat(inspected.stdout.decode().strip())

    def label_core_service(self, service: str, value: str) -> None:
        """Give the service a label that changes nothing it does, so only its definition changes."""
        path = self.copy / COMPOSE_FILE
        lines = path.read_text().splitlines(keepends=True)
        label = f"    labels: {{edge.probe-change: {value}}}\n"
        start = lines.index(f"  {service}:\n") + 1
        if lines[start].startswith("    labels: {edge.probe-change:"):
            lines[start] = label
        else:
            lines.insert(start, label)
        path.write_text("".join(lines))

    def edit_traefik_yml(self) -> None:
        with (self.copy / TRAEFIK_FILE).open("a") as file:
            file.write(HARMLESS_EDIT)

    def hello_state(self) -> str:
        containers = self.compose("--profile", "probe", "ps", "--all", "--format", "json", "hello")
        lines = containers.stdout.decode().strip()
        states = [json.loads(line)["State"] for line in lines.splitlines()]
        return states[0] if states else "absent"

    def edge(
        self, *arguments: str, terminal: bool, exit_code: int = 0, typed: bytes = b"yes\n"
    ) -> str:
        return edge_copy.run_edge(
            self.copy,
            self.environment,
            *arguments,
            terminal=terminal,
            exit_code=exit_code,
            typed=typed,
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

    def stays_behind_the_barrier(self, seconds: int, *, traefik_starting: bool = False) -> int:
        """Every request for `seconds` answers the barrier's 404, from the first one; the number of
        requests. With `traefik_starting` the answers of a starting Traefik before the
        first barrier answer do not count, and the window starts at that answer: nothing listens
        yet, or Traefik listens before its providers' first configuration and answers its plain
        404, which reaches no backend either. Whoami answering always fails."""
        asked = 0
        waiting_since = time.monotonic()
        end = waiting_since + seconds
        while time.monotonic() < end:
            answer = self.ask()
            if answer in (None, UNCONFIGURED) and traefik_starting:
                expect(
                    time.monotonic() - waiting_since < READY_SECONDS,
                    f"traefik never served the barrier (last answer {answer})",
                )
                end = time.monotonic() + seconds
                time.sleep(POLL_SECONDS)
                continue
            expect(answer == NOT_FOUND, f"a request behind the barrier answered {answer}")
            traefik_starting = False
            asked += 1
            time.sleep(POLL_SECONDS)
        return asked

    # --- the cells ---------------------------------------------------------------------------

    def bring_up(self) -> None:
        self.copy = edge_copy.repository_copy(REPO, self.home)
        (self.copy / "fleet.yaml").write_text(FLEET)
        self.compose("up", "--detach", "socket-proxy", "traefik")

    def cell_p7_the_real_bind_shape(self) -> None:
        resolved = json.loads(self.compose("--profile", "*", "config", "--format", "json").stdout)
        print(f"#85 P7 traefik volumes: {json.dumps(resolved['services']['traefik']['volumes'])}")
        derived = {
            name: core.file_binds(resolved["services"][name], self.copy)
            for name in core.core_services(resolved)
        }
        expect(
            derived == {"socket-proxy": (), "traefik": (self.copy / TRAEFIK_FILE,)},
            f"the core's file binds are {derived}",
        )
        print("#85 P7 held: the core's only file bind is the copy's traefik.yml, as derived")

    def cell_p1_first_apply(self) -> None:
        before = self.container_ids()["traefik"]
        out = self.edge("apply", "--closed", terminal=True)  # also starts hello
        expect(
            "traefik: recreate (traefik.yml has no applied record)" in out and RECREATED in out,
            f"the first apply said: {out[-200:]}",
        )
        expect(self.container_ids()["traefik"] != before, "the traefik container is the old one")
        expect(TRAEFIK_FILE in self.record.read().entries.get("traefik", {}), "no applied record")
        print("#85 P1 held: the first apply recreated traefik and wrote applied.json")

    def cell_p2_second_apply(self) -> None:
        before = self.container_ids()
        out = self.edge("apply", "--closed", terminal=True, typed=b"no\n")
        expect(NOTHING_TO_CHANGE in out, f"the second apply said: {out[-200:]}")
        expect(self.container_ids() == before, "the second apply replaced a container")
        print("#85 P2 held: a second apply changes nothing and keeps every container id")

    def cell_p3_edited_traefik_yml(self) -> None:
        self.volumes_before_p3 = self.volumes()
        before = self.container_ids()["traefik"]
        self.edit_traefik_yml()
        out = self.edge("apply", "--closed", terminal=True)
        expect(
            "traefik: recreate (traefik.yml changed)" in out and RECREATED in out,
            f"apply after the edit said: {out[-200:]}",
        )
        expect(self.container_ids()["traefik"] != before, "the traefik container is the old one")
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"#85 P3 held: the edit recreated traefik, and {HOST} answers through it")

    def cell_p4_barrier_proven_after_the_recreate(self) -> None:
        self.edge("stop", UNIT, terminal=False)
        self.compose("--profile", UNIT, "up", "--detach", "hello")
        self.edit_traefik_yml()
        out = self.edge("apply", "--closed", terminal=True)
        proven = f"the barrier of {UNIT} is proven"
        expect(
            proven in out and RECREATED in out and out.index(proven) < out.index(RECREATED),
            f"apply with the barrier said: {out[-300:]}",
        )
        expect(self.answers_the_barrier(), f"{HOST} answers {self.ask()} after the recreate")
        expect(self.hello_state() == "running", "hello stopped across the recreate")
        print("#85 P4 held: after the recreate the barrier answered 404, before the success line")

    def cell_p5_stopped_traefik(self) -> None:
        self.compose("stop", "traefik")
        recorded = self.record.read().entries
        self.edit_traefik_yml()
        out = self.edge("apply", "--closed", terminal=True, typed=b"no\n")
        expect(
            "traefik.yml changed; traefik is not running and is not started" in out
            and NOTHING_TO_CHANGE in out,
            f"apply with traefik stopped said: {out[-200:]}",
        )
        expect(self.traefik_state() == "exited", f"traefik is {self.traefik_state()}")
        expect(self.record.read().entries == recorded, "the record changed")
        print("#85 P5 held: a stopped traefik is named, stays exited, and the record stays old")

    def cell_p6_what_survives(self) -> None:
        volumes = self.volumes()
        expect(volumes == self.volumes_before_p3, f"the volumes were {self.volumes_before_p3}")
        expect(f"{PROJECT}_socket-proxy-run" in volumes, f"no socket-proxy-run in {volumes}")
        expect(self.marker.is_file() and self.barrier.is_file(), "the marker or barrier is gone")
        self.compose("up", "--detach", "traefik")
        self.wait_until("the barrier answering", self.answers_the_barrier)
        self.edge("start", UNIT, terminal=True)
        print("#85 P6 held: the named volumes, the marker and the barrier survived P3-P5")

    def cell_q1_the_definition_fact(self, after: str, must_match: tuple[str, ...]) -> None:
        listed = self.compose("ps", "--all", "--format", "json").stdout.decode()
        for line in listed.strip().splitlines():
            container = json.loads(line)
            if container["Service"] in CORE:
                fields = {key: container[key] for key in ("Service", "Image", "Labels")}
                print(f"#87 Q1 {after}, ps: {json.dumps(fields)}")
        hashes = self.compose("--profile", "*", "config", "--hash", ",".join(CORE)).stdout
        print(f"#87 Q1 {after}, config --hash: {hashes.decode().strip()!r}")
        engine = self.engine()
        read = engine.config_hashes(CORE)
        labels = {c.service: c.config_hash for c in engine.containers() if c.service in CORE}
        for service in CORE:
            same = labels[service] == read[service]
            print(f"#87 Q1 {after}: {service} label == config --hash: {same}")
        for service in must_match:
            expect(
                labels[service] == read[service],
                f"#87 rule 8: {service}'s label differs from config --hash {after}; stop the lane",
            )
        required = ", ".join(must_match) or "none required"
        print(f"#87 Q1 held {after}: printed; label == hash for {required}")

    def apply_core_change(self) -> tuple[dict[str, str], dict[str, str], str]:
        """The container ids before and after one `edge apply --closed` with `yes`, and its out."""
        before = self.container_ids()
        out = self.edge("apply", "--closed", terminal=True)
        return before, self.container_ids(), out

    def cell_q2_socket_proxy_definition(self) -> None:
        self.volumes_before_q2 = self.volumes()
        self.label_core_service("socket-proxy", "q2")
        before, after, out = self.apply_core_change()
        expect(
            "socket-proxy: recreate (its compose definition changed)" in out
            and "socket-proxy is recreated)" in out  # traefik's plan line, P5's edit may join it
            and out.index(SOCKET_PROXY_RECREATED) < out.index(RECREATED) < out.index(CORE_CURRENT),
            f"apply after the socket-proxy label said: {out[-400:]}",
        )
        expect(all(after[s] != before[s] for s in CORE), "a core container is the old one")
        expect(
            self.created(after["socket-proxy"]) < self.created(after["traefik"]),
            "traefik was created before socket-proxy",
        )
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"#87 Q2 held: socket-proxy, then traefik recreated; {HOST} answers through Traefik")

    def cell_q3_traefik_definition(self) -> None:
        self.label_core_service("traefik", "q3")
        before, after, out = self.apply_core_change()
        expect(
            "traefik: recreate (its compose definition changed)" in out
            and "socket-proxy: recreate" not in out
            and RECREATED in out,
            f"apply after the traefik label said: {out[-300:]}",
        )
        expect(after["socket-proxy"] == before["socket-proxy"], "socket-proxy was recreated")
        expect(after["traefik"] != before["traefik"], "the traefik container is the old one")
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print("#87 Q3 held: a traefik label recreated traefik alone; socket-proxy's id unchanged")

    def cell_q4_second_run(self) -> None:
        before = self.container_ids()
        out = self.edge("apply", "--closed", terminal=True, typed=b"no\n")
        expect(NOTHING_TO_CHANGE in out, f"the second apply said: {out[-200:]}")
        expect(self.container_ids() == before, "the second apply replaced a container")
        print("#87 Q4 held: a second apply changes nothing and keeps every container id")

    def cell_q5_barrier_after_a_socket_proxy_recreate(self) -> None:
        self.edge("stop", UNIT, terminal=False)
        self.compose("--profile", UNIT, "up", "--detach", "hello")
        self.label_core_service("socket-proxy", "q5")
        _, _, out = self.apply_core_change()
        proven = f"the barrier of {UNIT} is proven"
        expect(
            proven in out
            and SOCKET_PROXY_RECREATED in out
            and out.index(proven) < out.index(SOCKET_PROXY_RECREATED),
            f"apply with the barrier said: {out[-400:]}",
        )
        expect(self.answers_the_barrier(), f"{HOST} answers {self.ask()} after the recreate")
        expect(self.hello_state() == "running", "hello stopped across the recreate")
        print("#87 Q5 held: the barrier answered 404 with X-Edge-Barrier before the success line")

    def cell_q6_what_survives(self) -> None:
        volumes = self.volumes()
        expect(volumes == self.volumes_before_q2, f"the volumes were {self.volumes_before_q2}")
        expect(f"{PROJECT}_socket-proxy-run" in volumes, f"no socket-proxy-run in {volumes}")
        expect(self.marker.is_file() and self.barrier.is_file(), "the marker or barrier is gone")
        self.edge("start", UNIT, terminal=True)
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(
            "#87 Q6 held: socket-proxy-run, the named volumes, the marker and the barrier survived"
        )

    def cell_1_control(self) -> None:
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"#61 proof 1 held: {HOST} answers whoami without a switch")

    def cell_2_stop(self) -> None:
        self.edge("stop", UNIT, terminal=False)
        expect(self.barrier.is_file(), "stop wrote no dynamic/switch-probe.yml")
        expect(self.marker.is_file(), "stop wrote no marker")
        expect(self.answers_the_barrier(), f"{HOST} answers {self.ask()} after stop")
        expect(self.hello_state() == "exited", f"hello is {self.hello_state()} after stop")
        print("#61 proof 2 held: stop exits 0, wrote the barrier and the marker, 404, hello exited")

    def cell_3_up_behind_the_barrier(self) -> None:
        self.compose("--profile", UNIT, "up", "--detach", "hello")
        asked = self.stays_behind_the_barrier(3)
        expect(self.hello_state() == "running", "hello is not running")
        print(f"#61 proof 3 held: hello up behind the barrier, {asked} requests answered 404")

    def cell_4_traefik_restart(self) -> None:
        expect(self.hello_state() == "running", "hello is not running before the restart")
        self.compose("restart", "traefik")
        asked = self.stays_behind_the_barrier(BEHIND_BARRIER_SECONDS, traefik_starting=True)
        expect(self.hello_state() == "running", "hello stopped across the restart")
        print(
            f"#61 proof 4 held: after restarting traefik {asked} requests in 10 s all answered 404"
        )

    def cell_5_apply(self) -> None:
        out = self.edge("apply", "--closed", terminal=True)
        expect(
            "probe stays stopped" in out and NOTHING_TO_CHANGE in out, f"apply said: {out[-160:]}"
        )
        expect(self.hello_state() == "running", "apply stopped hello")
        expect(self.barrier.is_file() and self.answers_the_barrier(), "apply lifted the barrier")
        print(
            "#61 proof 5 held: apply says stays stopped / nothing to change; barrier and hello stay"
        )

    def cell_6_start(self) -> None:
        self.edge("start", UNIT, terminal=True)
        expect(not self.barrier.exists(), "start left the barrier")
        expect(not self.marker.exists(), "start left the marker")
        self.wait_until(HELLO_ANSWERING, self.serves_whoami)
        print(f"#61 proof 6 held: start removed barrier and marker; {HOST} answers whoami")

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
            "#70 proof 8 held: stop --all left the throwaway unit inactive with the marker, and "
            "its boot drop-in skipped a start; start --all proved the unit barrier, reopened it "
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
        print("#61 proof 7 held: traefik down: stop exits 1 partial, both kept; start reopens")


class ThrowawayTunnel:
    """A user unit that stands in for the tunnel, in the user manager's runtime directory (a tmpfs
    that is gone after a logout), with the real boot drop-in of `tunnel/` rendered for the
    probe's HOME, so proof 8 can show #76's boot skip."""

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
        probe.cell_q1_the_definition_fact("after the plain up", must_match=())
        probe.cell_p7_the_real_bind_shape()
        probe.cell_p1_first_apply()
        probe.cell_q1_the_definition_fact("after edge's recreate", must_match=("traefik",))
        probe.cell_p2_second_apply()
        probe.cell_1_control()
        probe.cell_2_stop()
        probe.cell_3_up_behind_the_barrier()
        probe.cell_4_traefik_restart()
        probe.cell_5_apply()
        probe.cell_6_start()
        probe.cell_7_traefik_down()
        probe.cell_8_global_stop()
        probe.cell_p3_edited_traefik_yml()
        probe.cell_p4_barrier_proven_after_the_recreate()
        probe.cell_p5_stopped_traefik()
        probe.cell_p6_what_survives()
        probe.cell_q2_socket_proxy_definition()
        probe.cell_q3_traefik_definition()
        probe.cell_q4_second_run()
        probe.cell_q5_barrier_after_a_socket_proxy_recreate()
        probe.cell_q6_what_survives()
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
        print("PROBE PASSED: #61 proofs 1-7, #70 proof 8, #85 P1-P7 and #87 Q1-Q6 hold")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
