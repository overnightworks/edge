#!/usr/bin/env python3
"""Probe of the internal CA and of traefik-mcp (#38 proofs 1-5, #48 proofs 6-9): a throwaway
compose project and a throwaway HOME.

It runs the real `step-ca/init` against the scratch HOME, starts the real `step-ca`, `traefik-mcp`
and `hello-mcp` services from compose.yaml and ca.json under their own compose project name, checks
the proofs and always removes its containers, networks and volumes. It never touches the live
stack: the project name and HOME are set in the environment of every command.

    flock /tmp/probe-stack.lock timeout --kill-after=30 1500 \\
        python3 probe/step-ca/probe.py --scratch DIR

The CA and the gateway are only reachable on their internal compose networks, so every call to
them runs from a throwaway container on the network a client would sit on. Needs an empty or new
--scratch directory. Proof 9 waits for a renewal: the run takes about 15 minutes. Needs `uv`:
proof 8 runs `edge-check` through it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROJECT = "edge-stepca-probe"
CA_URL = "https://step-ca:9000"
NETWORK = f"{PROJECT}_mcp-ca"
CERTIFICATE_UNVERIFIED = 60  # curl exit codes
COULD_NOT_RESOLVE = 6
PROVISIONER = "acme"
DIRECTORY_URL = f"{CA_URL}/acme/{PROVISIONER}/directory"
READY_SECONDS = 60
ROOT_CERTIFICATE = "root_ca.crt"
INTERMEDIATE_CERTIFICATE = "certs/intermediate_ca.crt"
INTERMEDIATE_KEY = "secrets/intermediate_ca_key"
STEP_HOME = "/home/step"
INTERMEDIATE_IN_VOLUME = f"{STEP_HOME}/{INTERMEDIATE_CERTIFICATE}"
CONTAINER_USER_ID = 1000  # the step user of the smallstep image
CONTAINER_USER = f"{CONTAINER_USER_ID}:{CONTAINER_USER_ID}"
WORK_DIRECTORY = "/work"  # private tmpfs for the ACME client, not world-writable
ALLOWED_FILES = {
    f"certs/{ROOT_CERTIFICATE}",
    INTERMEDIATE_CERTIFICATE,
    INTERMEDIATE_KEY,
}
ALLOWED_DIRECTORY = "db/"
OUTSIDE_NAME = "outside.example.com"
INSIDE_NAME = "probe.mcp.internal"
REFUSAL = "will not issue certificates for the identifier"  # ACME rejectedIdentifier
ROOT_IN_CLIENT = "/root.crt"
ORIGIN = "hello.mcp.internal"
ORIGIN_URL = f"https://{ORIGIN}/"
ORIGIN_PLAIN_URL = f"http://{ORIGIN}:443/"  # NOSONAR(python:S5332) proof 6: plain http must fail
ORIGIN_ANSWER = f"Host: {ORIGIN}"  # the backend (whoami) echoes the request's Host header
BACKEND_URL = "http://hello-mcp/"  # NOSONAR(python:S5332) the backend speaks plain http by design
CA_HEALTH_URL = f"{CA_URL}/health"
TRAEFIK_USER = "65534:65534"
TRAEFIK_UID = 65534
ACME_STORAGE = "/acme/acme.json"
EDGE_CHECK = ["uv", "run", "--quiet", "edge-check", "--json"]
EXIT_AFTER_TERM = 143  # 128 + SIGTERM; a forced kill after the grace period is 137
BOOT_WITHOUT_CA_SECONDS = 10  # how long the gateway has to sit idle before the CA appears
LISTENER_PORTS_OF_THE_PROBE = (9000, 443)  # the CA and the gateway: container ports only
CA_DEFAULT_LIFETIME_SECONDS = 24 * 3600
CA_BACKDATE_SECONDS = 60  # step-ca starts a certificate one minute before it was issued
SHORT_LIFETIME_MINUTES = 20  # probe-only: renewal inside the poll
RENEWAL_POLL_SECONDS = 12 * 60
RENEWAL_POLL_INTERVAL = 15
FOREIGN_UID = 12345
OVERRIDE = REPO / "probe" / "step-ca" / "compose.probe.yaml"


class ProbeFailed(Exception):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailed(message)


class Probe:
    def __init__(self, scratch: Path) -> None:
        self.home = scratch / "home"
        self.home.mkdir(parents=True)
        self.secrets_dir = self.home / ".secrets" / "step-ca"
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "COMPOSE_PROJECT_NAME": PROJECT,
            "EDGE_DOMAIN": "probe.invalid",
            "DOCKER_GID": "0",
        }
        self.environment.pop("DOCKER_STEPCA_INIT_PASSWORD", None)
        self.scratch = scratch
        self.compose_files: list[str] = []
        self.listeners_before: set[str] = set()
        self.edge_check_before: list[dict] = []

    def run(
        self, command: list[str], *, stdin: bytes | None = None, check: bool = True,
        own_session: bool = False,
    ):  # fmt: skip
        result = subprocess.run(
            command, capture_output=True, input=stdin, env=self.environment, cwd=REPO,
            check=False, timeout=240, close_fds=True, start_new_session=own_session,
        )  # fmt: skip
        if check and result.returncode != 0:
            tail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
            raise ProbeFailed(f"{' '.join(command[:4])} exited {result.returncode}: {tail[-1:]}")
        return result

    def compose(self, *args: str, check: bool = True, own_session: bool = False):
        files = [flag for file in self.compose_files for flag in ("--file", file)]
        return self.run(
            ["docker", "compose", *files, "--profile", "probe", "--profile", "step-ca", *args],
            check=check, own_session=own_session,
        )  # fmt: skip

    def init(self, *, check: bool = True) -> str:
        result = self.run([str(REPO / "step-ca" / "init")], check=check)
        return result.stdout.decode() + result.stderr.decode()

    @cached_property
    def composed(self) -> dict:
        """What compose resolves once per run: the images and the volumes' real names."""
        return json.loads(self.compose("config", "--format", "json").stdout)

    def image_and_volume(self) -> tuple[str, str]:
        return self.composed["services"]["step-ca"]["image"], self.composed["volumes"]["step-ca"][
            "name"
        ]

    def in_volume(self, *tool: str, writable: bool = False) -> bytes:
        image, volume = self.image_and_volume()
        mount = f"{volume}:{STEP_HOME}" + ("" if writable else ":ro")
        return self.run(
            ["docker", "run", "--rm", "--network", "none", "--user", CONTAINER_USER, "--entrypoint",
             tool[0], "--volume", mount, image, *tool[1:]]
        ).stdout  # fmt: skip

    def volume_files(self) -> set[str]:
        listing = self.in_volume("find", STEP_HOME, "-type", "f").decode().splitlines()
        return {line.removeprefix(f"{STEP_HOME}/") for line in listing}

    def expect_volume_has_compose_labels(self) -> None:
        _, volume = self.image_and_volume()
        labels = json.loads(
            self.run(["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume]).stdout
        )
        expected = {"com.docker.compose.project": PROJECT, "com.docker.compose.volume": "step-ca"}
        expect(expected.items() <= labels.items(), f"the volume lacks its compose labels: {labels}")

    # --- the CA over TLS ----------------------------------------------------------------------

    def client(self, network: str, *curl_arguments: str, root: Path | None = None):
        """curl as a throwaway client on one of the project's networks; `root` is the only trust."""
        image, _ = self.image_and_volume()
        trust = ["--volume", f"{root}:{ROOT_IN_CLIENT}:ro"] if root else []
        cacert = ["--cacert", ROOT_IN_CLIENT] if root else []
        return self.run(
            ["docker", "run", "--rm", "--network", f"{PROJECT}_{network}", "--read-only",
             "--user", CONTAINER_USER, *trust, "--entrypoint", "curl", image,
             "--silent", "--max-time", "5", *cacert, *curl_arguments],
            check=False,
        )  # fmt: skip

    def directory_status(self, cafile: Path | None) -> int:
        """The directory's HTTP status as a throwaway container on the CA's network sees it, or -1
        when the certificate does not verify; raises OSError while the CA is not answering."""
        result = self.client(
            "mcp-ca", "--output", "/dev/null", "--write-out", "%{http_code}", DIRECTORY_URL,
            root=cafile,
        )  # fmt: skip
        if result.returncode == CERTIFICATE_UNVERIFIED:
            return -1
        if result.returncode != 0:
            raise ConnectionError(f"curl exited {result.returncode}")
        return int(result.stdout)

    def order_outcome(self, name: str) -> tuple[int, str]:
        """Asks the CA for a certificate for `name` with the pinned image's own ACME client."""
        image, _ = self.image_and_volume()
        owner = f"uid={CONTAINER_USER_ID},gid={CONTAINER_USER_ID}"
        private_tmpfs = f"{WORK_DIRECTORY}:{owner},mode=0700"
        root_mount = f"{self.secrets_dir / ROOT_CERTIFICATE}:/root.crt:ro"
        certificate_path, key_path = f"{WORK_DIRECTORY}/c.crt", f"{WORK_DIRECTORY}/c.key"
        result = self.run(
            ["docker", "run", "--rm", "--network", NETWORK, "--tmpfs", private_tmpfs,
             "--read-only", "--user", CONTAINER_USER, "--volume", root_mount,
             "--entrypoint", "step", image, "ca", "certificate", name, certificate_path, key_path,
             "--acme", DIRECTORY_URL, "--root", "/root.crt", "--force"],
            check=False,
        )  # fmt: skip
        return result.returncode, (result.stderr + result.stdout).decode(errors="replace")

    def expect_outside_name_rejected(self) -> None:
        outside_code, outside_text = self.order_outcome(OUTSIDE_NAME)
        _, inside_text = self.order_outcome(INSIDE_NAME)
        expect(outside_code != 0, f"the CA ordered a certificate for {OUTSIDE_NAME}")
        expect(
            REFUSAL in outside_text,
            f"the refusal of {OUTSIDE_NAME} was not refused: {outside_text.strip()[-160:]}",
        )
        expect(
            REFUSAL not in inside_text,
            f"the CA refused {INSIDE_NAME}: {inside_text.strip()[-160:]}",
        )

    def poll_until(self, what: str, seconds: float, attempt: Callable[[], bool]) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if attempt():
                    return
            except OSError:
                pass
            time.sleep(1)
        raise ProbeFailed(f"{what} did not happen within {seconds:.0f} s")

    def wait_until_answering(self) -> None:
        root = self.secrets_dir / ROOT_CERTIFICATE
        try:
            self.poll_until(
                "the CA answering", READY_SECONDS, lambda: self.directory_status(root) == 200
            )
        except ProbeFailed as failure:
            logs = self.compose("logs", "--no-color", "--tail", "5", "step-ca", check=False)
            raise ProbeFailed(f"{failure}: {logs.stdout.decode()[-300:]}") from failure

    def certificate_fingerprint(self, certificate: bytes) -> str:
        result = self.run(
            ["openssl", "x509", "-noout", "-fingerprint", "-sha256"], stdin=certificate
        )
        return result.stdout.decode().strip()

    def public_key(self, command: list[str], stdin: bytes) -> bytes:
        return self.run(command, stdin=stdin).stdout

    # --- the proofs ---------------------------------------------------------------------------

    def proof_1_init(self) -> None:
        first = self.init()
        password = (self.secrets_dir / "password").read_text()
        modes = {
            name: stat.S_IMODE((self.secrets_dir / name).stat().st_mode)
            for name in ("root_ca_key", ROOT_CERTIFICATE, "password")
        }
        directory_mode = stat.S_IMODE(self.secrets_dir.stat().st_mode)
        expect(directory_mode == 0o700, f"directory mode {oct(directory_mode)}")
        expect(
            modes == {"root_ca_key": 0o600, ROOT_CERTIFICATE: 0o644, "password": 0o644},
            f"modes {modes}",
        )
        self.expect_volume_has_compose_labels()
        second = self.init()
        expect("nothing changed" in second, f"second run: {second.strip()[-120:]}")
        for output in (first, second):
            expect(password not in output, "the output holds the password")
            expect("PRIVATE KEY" not in output, "the output holds a private key")
        print(
            "proof 1 held: modes 0700/0600/0644/0644; second run `nothing changed`; output silent"
        )

    def proof_2_directory(self) -> None:
        self.compose("up", "--detach", "step-ca")
        self.wait_until_answering()
        status = self.directory_status(self.secrets_dir / ROOT_CERTIFICATE)
        expect(status == 200, f"directory answered {status}")
        expect(self.directory_status(None) == -1, "the system trust store verified the CA")
        self.expect_outside_name_rejected()
        print(
            f"proof 2 held: {DIRECTORY_URL} -> 200 with the probe root; fails without it; "
            f"an order for {OUTSIDE_NAME} is refused by the policy"
        )

    def proof_3_volume(self) -> None:
        files = self.volume_files()
        stray = {f for f in files if f not in ALLOWED_FILES and not f.startswith(ALLOWED_DIRECTORY)}
        expect(not stray, f"files outside the allow-list: {sorted(stray)}")
        expect(files >= ALLOWED_FILES, f"missing: {sorted(ALLOWED_FILES - files)}")
        root_certificate = (self.secrets_dir / ROOT_CERTIFICATE).read_bytes()
        root_public = self.public_key(["openssl", "x509", "-noout", "-pubkey"], root_certificate)
        password = self.secrets_dir / "password"
        for name in sorted(files):
            content = self.in_volume("cat", f"{STEP_HOME}/{name}")
            if b"PRIVATE KEY" not in content:
                continue
            public = self.public_key(
                ["openssl", "pkey", "-pubout", "-passin", f"file:{password}"], content
            )
            expect(public != root_public, f"{name} is the root key")
        print(f"proof 3 held: {len(files)} files, all allow-listed; no key in it matches the root")

    def proof_4_restart(self) -> None:
        intermediate = self.in_volume("cat", INTERMEDIATE_IN_VOLUME)
        before = (self.certificate_fingerprint(intermediate), self.volume_files())
        self.compose("restart", "step-ca")
        self.wait_until_answering()
        intermediate = self.in_volume("cat", INTERMEDIATE_IN_VOLUME)
        after = (self.certificate_fingerprint(intermediate), self.volume_files())
        expect(after[0] == before[0], f"intermediate changed: {before[0]} -> {after[0]}")
        expect(before[1] <= after[1], f"db files lost: {sorted(before[1] - after[1])}")
        print(f"proof 4 held after restart: {after[0]}; {len(after[1])} files, none lost")

    def chown_root_key(self, owner_id: int) -> None:
        image, _ = self.image_and_volume()
        self.run(
            ["docker", "run", "--rm", "--network", "none", "--user", "0", "--cap-drop", "ALL",
             "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--volume",
             f"{self.secrets_dir}:/secrets", "--entrypoint", "chown", image,
             f"{owner_id}:{owner_id}", "/secrets/root_ca_key"]
        )  # fmt: skip

    def expect_foreign_owned_root_key_refused(self, root_key: bytes) -> None:
        """A root key the image's user cannot read ends init in one line, with the key untouched."""
        self.chown_root_key(FOREIGN_UID)
        try:
            output = self.init(check=False)
            owner = (self.secrets_dir / "root_ca_key").stat().st_uid
        finally:
            self.chown_root_key(os.getuid())
        expect(owner == FOREIGN_UID, "the foreign owner of the root key was changed")
        expect("FAILED" in output and "permission denied" in output, f"init said: {output[-160:]}")
        expect("PRIVATE KEY" not in output, "the refusal shows a private key")
        expect(len(output.strip().splitlines()) <= 2, f"more than one line: {output[-160:]}")
        expect((self.secrets_dir / "root_ca_key").read_bytes() == root_key, "root key changed")
        expect(INTERMEDIATE_CERTIFICATE not in self.volume_files(), "an intermediate was made")

    def proof_5_partial(self) -> None:
        self.compose("stop", "step-ca")
        root_key = (self.secrets_dir / "root_ca_key").read_bytes()
        self.in_volume(
            "rm", INTERMEDIATE_IN_VOLUME, f"{STEP_HOME}/{INTERMEDIATE_KEY}",
            writable=True,
        )  # fmt: skip
        expect(INTERMEDIATE_CERTIFICATE not in self.volume_files(), "the intermediate stayed")
        self.expect_foreign_owned_root_key_refused(root_key)
        output = self.init()
        expect("intermediate" in output, f"init did not report the intermediate: {output[-120:]}")
        expect((self.secrets_dir / "root_ca_key").read_bytes() == root_key, "root key changed")
        expect(self.volume_files() >= ALLOWED_FILES, "the intermediate was not completed")
        self.compose("start", "step-ca")
        self.wait_until_answering()
        print("proof 5 held: init completed the intermediate; root key byte-for-byte unchanged")

    # --- traefik-mcp ---------------------------------------------------------------------------

    def host_listeners(self) -> set[str]:
        lines = self.run(["ss", "-Hltn"]).stdout.decode().splitlines()
        return {line.split()[3] for line in lines}

    def edge_check_findings(self) -> list[dict]:
        """What edge-check reports about the whole host; exit 1 only means it found something."""
        result = self.run(EDGE_CHECK, check=False)
        try:
            findings = json.loads(result.stdout)
        except ValueError:
            findings = None
        stderr_tail = result.stderr.decode(errors="replace").strip().splitlines()[-1:]
        expect(
            result.returncode in (0, 1) and isinstance(findings, list),
            f"edge-check exited {result.returncode} without a JSON list: {stderr_tail}",
        )
        return findings

    def intermediate_certificate(self) -> dict:
        output = self.in_volume(
            "step", "certificate", "inspect", "--format", "json", INTERMEDIATE_IN_VOLUME
        )  # fmt: skip
        return json.loads(output)

    def served_certificate(self) -> dict:
        """What a client on `mcp` is served for the origin, verified against the probe root."""
        image, _ = self.image_and_volume()
        root_mount = f"{self.secrets_dir / ROOT_CERTIFICATE}:{ROOT_IN_CLIENT}:ro"
        result = self.run(
            ["docker", "run", "--rm", "--network", f"{PROJECT}_mcp", "--read-only", "--user",
             CONTAINER_USER, "--volume", root_mount, "--entrypoint", "step", image,
             "certificate", "inspect", "--format", "json", "--roots", ROOT_IN_CLIENT, ORIGIN_URL],
            check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise ConnectionError(f"step exited {result.returncode}")
        return json.loads(result.stdout)

    def wait_until_served(self) -> dict:
        root = self.secrets_dir / ROOT_CERTIFICATE

        def answers() -> bool:
            result = self.client("mcp", ORIGIN_URL, root=root)
            return ORIGIN_ANSWER in result.stdout.decode(errors="replace")

        self.poll_until(f"{ORIGIN_URL} answering with the backend", READY_SECONDS, answers)
        return self.served_certificate()

    def expect_certificate_length(self, certificate: dict, lifetime_seconds: int) -> None:
        length = certificate["validity"]["length"]
        expected = lifetime_seconds + CA_BACKDATE_SECONDS
        expect(length == expected, f"the certificate is valid for {length} s, not {expected} s")

    def boot_gateway_before_the_ca(self) -> dict:
        """Traefik asks the CA once at start; the gateway must wait for the CA, not stay without."""
        self.compose("stop", "step-ca")
        self.compose("run", "--rm", "--no-deps", "traefik-mcp-acme-owner")
        self.compose("up", "--detach", "--no-deps", "traefik-mcp", "hello-mcp")
        time.sleep(BOOT_WITHOUT_CA_SECONDS)
        answer = self.client("mcp", ORIGIN_URL, root=self.secrets_dir / ROOT_CERTIFICATE)
        expect(ORIGIN_ANSWER not in answer.stdout.decode(errors="replace"), "served without a CA")
        self.expect_waiting_gateway_stops_on_term()
        self.compose("up", "--detach", "--no-deps", "traefik-mcp")
        self.compose("start", "step-ca")
        self.wait_until_answering()
        return self.wait_until_served()

    def expect_waiting_gateway_stops_on_term(self) -> None:
        """The wait is a shell, not Traefik: TERM must end it, not Docker's kill after the grace."""
        logs = self.compose("logs", "--no-color", "traefik-mcp").stdout
        expect(bool(logs.strip()), "the gateway waiting for the CA said nothing")
        self.compose("stop", "traefik-mcp")
        container = self.compose("ps", "--all", "--quiet", "traefik-mcp").stdout.decode().strip()
        code = self.run(["docker", "inspect", "--format", "{{.State.ExitCode}}", container])
        exit_code = int(code.stdout)
        expect(exit_code == EXIT_AFTER_TERM, f"the waiting gateway exited {exit_code} on stop")

    def acme_storage(self) -> tuple[int, int]:
        """Size and owner of the stored certificates in the volume; raises OSError until written."""
        traefik = self.composed["services"]["traefik-mcp"]["image"]
        volume = self.composed["volumes"]["traefik-mcp-acme"]["name"]
        result = self.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", TRAEFIK_USER,
             "--volume", f"{volume}:{Path(ACME_STORAGE).parent}:ro", "--entrypoint", "stat",
             traefik, "-c", "%s %u", ACME_STORAGE],
            check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise FileNotFoundError(ACME_STORAGE)
        size, owner = result.stdout.split()
        return int(size), int(owner)

    def wait_until_stored(self) -> None:
        """The volume is persistent and uid 65534 can write it: Traefik only logs when it cannot."""

        def written() -> bool:
            size, owner = self.acme_storage()
            expect(owner == TRAEFIK_UID, f"{ACME_STORAGE} belongs to uid {owner}")
            return size > 0

        self.poll_until(f"{ACME_STORAGE} written", READY_SECONDS, written)

    def expect_certificate_survives_gateway_restart(self, served: dict) -> None:
        """A restart with the CA down serves the stored certificate; it is not issued again."""
        self.compose("stop", "step-ca")
        self.compose("restart", "traefik-mcp")
        restarted = self.wait_until_served()
        self.compose("start", "step-ca")
        self.wait_until_answering()
        expect(
            restarted["serial_number"] == served["serial_number"],
            "the restarted gateway served another certificate than the stored one",
        )

    def proof_6_gateway(self) -> None:
        self.listeners_before = self.host_listeners()
        self.edge_check_before = self.edge_check_findings()
        root = self.secrets_dir / ROOT_CERTIFICATE
        served = self.boot_gateway_before_the_ca()
        intermediate = self.intermediate_certificate()
        expect(served["issuer_dn"] == intermediate["subject_dn"], "not issued by the intermediate")
        self.expect_certificate_length(served, CA_DEFAULT_LIFETIME_SECONDS)
        unverified = self.client("mcp", ORIGIN_URL)
        expect(
            unverified.returncode == CERTIFICATE_UNVERIFIED,
            f"without the root: {unverified.returncode}",
        )
        plain = self.client("mcp", "--include", ORIGIN_PLAIN_URL, root=root)
        expect(
            ORIGIN_ANSWER not in plain.stdout.decode(errors="replace"),
            "plain HTTP reached the backend",
        )
        self.wait_until_stored()
        self.expect_certificate_survives_gateway_restart(served)
        length, issuer = served["validity"]["length"], intermediate["subject_dn"]
        print(
            f"proof 6 held: gateway booted before the CA and issued once it listened; {ORIGIN_URL} "
            f"-> backend answer with the probe root; certificate of {length} s from `{issuer}`; "
            f"without the root curl exit {unverified.returncode}; plain http on 443: exit "
            f"{plain.returncode}, no backend answer; acme.json kept by uid {TRAEFIK_UID} and "
            f"the same certificate served after a restart with the CA down"
        )

    def proof_7_network_split(self) -> None:
        reachable = {
            ("mcp-backends", BACKEND_URL): True,
            ("mcp-backends", CA_HEALTH_URL): False,
            ("mcp-backends", ORIGIN_URL): False,
            ("mcp", BACKEND_URL): False,
            ("mcp", CA_HEALTH_URL): False,
            ("mcp-ca", BACKEND_URL): False,
        }
        for (network, url), expected in reachable.items():
            result = self.client(network, "--insecure", "--output", "/dev/null", url)
            outcome = "answered" if result.returncode == 0 else f"curl exit {result.returncode}"
            wanted = 0 if expected else COULD_NOT_RESOLVE
            expect(result.returncode == wanted, f"{network} -> {url}: {outcome}")
        print(f"proof 7 held: {len(reachable)} network pairs as ruled; only the control answers")

    def proof_8_no_host_listener(self) -> None:
        new_listeners = self.host_listeners() - self.listeners_before
        expect(not new_listeners, f"new host listeners: {sorted(new_listeners)}")
        ports = {listener.rsplit(":", 1)[1] for listener in self.host_listeners()}
        taken = ports & {str(port) for port in LISTENER_PORTS_OF_THE_PROBE}
        expect(not taken, f"the host listens on {sorted(taken)}")
        new_findings = [f for f in self.edge_check_findings() if f not in self.edge_check_before]
        expect(not new_findings, f"edge-check reports something new: {new_findings}")
        print(
            f"proof 8 held: ss -ltn unchanged ({len(self.listeners_before)} listeners), none on "
            f"{LISTENER_PORTS_OF_THE_PROBE}; edge-check reports nothing new"
        )

    def remove_stored_certificates(self) -> None:
        traefik = self.composed["services"]["traefik-mcp"]["image"]
        volume = self.composed["volumes"]["traefik-mcp-acme"]["name"]
        self.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", TRAEFIK_USER,
             "--volume", f"{volume}:{Path(ACME_STORAGE).parent}", "--entrypoint", "rm", traefik,
             "-f", ACME_STORAGE]
        )  # fmt: skip

    def use_short_lived_certificates(self) -> None:
        """Probe-only ca.json: the repository's, plus a 20 minute default certificate lifetime."""
        ca = json.loads((REPO / "step-ca" / "ca.json").read_text())
        ca["authority"]["provisioners"][0]["claims"] = {
            "defaultTLSCertDuration": f"{SHORT_LIFETIME_MINUTES}m"
        }
        short_ca = self.scratch / "ca-short-lived.json"
        short_ca.write_text(json.dumps(ca))
        self.environment["PROBE_CA_JSON"] = str(short_ca)
        self.compose_files = ["compose.yaml", str(OVERRIDE)]

    def traefik_started_at(self) -> str:
        container = self.compose("ps", "--quiet", "traefik-mcp").stdout.decode().strip()
        started = self.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", container])
        return started.stdout.decode().strip()

    def proof_9_renewal(self) -> None:
        self.compose("stop", "traefik-mcp")
        self.remove_stored_certificates()
        self.use_short_lived_certificates()
        self.compose("up", "--detach", "step-ca")
        self.wait_until_answering()
        self.compose("up", "--detach", "traefik-mcp")
        first = self.wait_until_served()
        self.expect_certificate_length(first, SHORT_LIFETIME_MINUTES * 60)
        started_at = self.traefik_started_at()
        waited = time.monotonic()
        renewed = first
        while renewed["serial_number"] == first["serial_number"]:
            expect(time.monotonic() - waited < RENEWAL_POLL_SECONDS, "no renewal within 12 minutes")
            time.sleep(RENEWAL_POLL_INTERVAL)
            renewed = self.served_certificate()
        expiry = datetime.fromisoformat(first["validity"]["end"])
        expect(datetime.now(UTC) < expiry, "the old certificate had expired before the renewal")
        expect(self.traefik_started_at() == started_at, "traefik-mcp restarted")
        old, new = first["serial_number"][-8:], renewed["serial_number"][-8:]
        print(
            f"proof 9 held: serial ...{old} -> ...{new} after {time.monotonic() - waited:.0f} s, "
            f"the old one valid until {first['validity']['end']}, traefik-mcp not restarted"
        )

    def teardown(self) -> None:
        # Own session: a signal to the probe's process group must not reach these.
        self.compose(
            "down", "--volumes", "--remove-orphans", "--timeout", "10", check=False,
            own_session=True,
        )  # fmt: skip
        left = self.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            own_session=True,
        ).stdout.decode()
        volumes = self.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={PROJECT}_"], own_session=True
        ).stdout.decode()
        expect(
            not (left.strip() or volumes.strip()), f"left behind: {left.split()} {volumes.split()}"
        )
        print(f"teardown: project {PROJECT} removed (containers and volume)")


@contextmanager
def stop_signals_ignored() -> Iterator[None]:
    """Teardown is what removes the containers and the volume, so a late SIGTERM or Ctrl-C must
    not cut it short."""
    stop_signals = (signal.SIGTERM, signal.SIGINT)
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in stop_signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.scratch.exists() and any(arguments.scratch.iterdir()):
        print("PROBE FAILED: --scratch is not empty")
        return 3
    arguments.scratch.mkdir(parents=True, exist_ok=True)
    probe = Probe(arguments.scratch)
    signal.signal(signal.SIGTERM, raise_interrupt)  # `timeout` ends the probe with SIGTERM
    exit_code = 1
    try:
        exit_code = prove(probe)
    finally:
        with stop_signals_ignored():
            try:
                probe.teardown()
            except ProbeFailed as failure:
                print(f"PROBE FAILED: {failure}")
                exit_code = 1
    if exit_code == 0:
        print("PROBE PASSED: proofs 1-9 hold")
    return exit_code


def raise_interrupt(_signal: int, _frame: object) -> None:
    raise KeyboardInterrupt


def prove(probe: Probe) -> int:
    try:
        probe.proof_1_init()
        probe.proof_2_directory()
        probe.proof_3_volume()
        probe.proof_4_restart()
        probe.proof_5_partial()
        probe.proof_6_gateway()
        probe.proof_7_network_split()
        probe.proof_8_no_host_listener()
        probe.proof_9_renewal()
    except (ProbeFailed, subprocess.TimeoutExpired, OSError) as failure:
        print(f"PROBE FAILED: {failure}")
        return 1
    except KeyboardInterrupt:
        print("PROBE FAILED: interrupted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
