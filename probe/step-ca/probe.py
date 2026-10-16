#!/usr/bin/env python3
"""Probe of the internal CA (#38 proofs 1-5): a throwaway compose project and a throwaway HOME.

It runs the real `step-ca/init` against the scratch HOME, starts the real `step-ca` service from
compose.yaml and ca.json under its own compose project name, checks the five proofs and always
removes its containers and volume. It never touches the live stack: the project name and HOME are
set in the environment of every command.

    flock /tmp/probe-stack.lock timeout --kill-after=30 420 \\
        python3 probe/step-ca/probe.py --scratch DIR

Needs 127.0.0.1:9000 free (exit 3 when taken) and an empty or new --scratch directory.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROJECT = "edge-stepca-probe"
CA_URL = "https://127.0.0.1:9000"
PROVISIONER = "acme"
DIRECTORY_URL = f"{CA_URL}/acme/{PROVISIONER}/directory"
READY_SECONDS = 60
ALLOWED_FILES = {"certs/root_ca.crt", "certs/intermediate_ca.crt", "secrets/intermediate_ca_key"}
ALLOWED_DIRECTORY = "db/"


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

    def run(self, command: list[str], *, stdin: bytes | None = None, check: bool = True):
        result = subprocess.run(
            command, capture_output=True, input=stdin, env=self.environment, cwd=REPO,
            check=False, timeout=240, close_fds=True,
        )  # fmt: skip
        if check and result.returncode != 0:
            tail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
            raise ProbeFailed(f"{' '.join(command[:4])} exited {result.returncode}: {tail[-1:]}")
        return result

    def compose(self, *args: str, check: bool = True):
        return self.run(["docker", "compose", *args], check=check)

    def init(self) -> str:
        result = self.run([str(REPO / "step-ca" / "init")])
        return result.stdout.decode() + result.stderr.decode()

    def image_and_volume(self) -> tuple[str, str]:
        config = json.loads(self.compose("config", "--format", "json").stdout)
        return config["services"]["step-ca"]["image"], config["volumes"]["step-ca"]["name"]

    def in_volume(self, *tool: str, writable: bool = False) -> bytes:
        image, volume = self.image_and_volume()
        mount = f"{volume}:/home/step" + ("" if writable else ":ro")
        return self.run(
            ["docker", "run", "--rm", "--network", "none", "--user", "1000:1000", "--entrypoint",
             tool[0], "--volume", mount, image, *tool[1:]]
        ).stdout  # fmt: skip

    def volume_files(self) -> set[str]:
        listing = self.in_volume("find", "/home/step", "-type", "f").decode().splitlines()
        return {line.removeprefix("/home/step/") for line in listing}

    # --- the CA over TLS ----------------------------------------------------------------------

    def directory_status(self, cafile: Path | None) -> int:
        context = ssl.create_default_context(cafile=str(cafile) if cafile else None)
        try:
            with urllib.request.urlopen(DIRECTORY_URL, context=context, timeout=5) as reply:
                return reply.status
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                return -1
            raise

    def wait_until_answering(self) -> None:
        root = self.secrets_dir / "root_ca.crt"
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            try:
                if self.directory_status(root) == 200:
                    return
            except OSError:
                pass
            time.sleep(1)
        logs = self.compose("logs", "--no-color", "--tail", "5", "step-ca", check=False)
        raise ProbeFailed(f"the CA never answered: {logs.stdout.decode()[-300:]}")

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
            for name in ("root_ca_key", "root_ca.crt", "password")
        }
        directory_mode = stat.S_IMODE(self.secrets_dir.stat().st_mode)
        expect(directory_mode == 0o700, f"directory mode {oct(directory_mode)}")
        expect(
            modes == {"root_ca_key": 0o600, "root_ca.crt": 0o644, "password": 0o644},
            f"modes {modes}",
        )
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
        status = self.directory_status(self.secrets_dir / "root_ca.crt")
        expect(status == 200, f"directory answered {status}")
        expect(self.directory_status(None) == -1, "the system trust store verified the CA")
        print(f"proof 2 held: {DIRECTORY_URL} -> 200 with the probe root; fails without it")

    def proof_3_volume(self) -> None:
        files = self.volume_files()
        stray = {f for f in files if f not in ALLOWED_FILES and not f.startswith(ALLOWED_DIRECTORY)}
        expect(not stray, f"files outside the allow-list: {sorted(stray)}")
        expect(files >= ALLOWED_FILES, f"missing: {sorted(ALLOWED_FILES - files)}")
        root_certificate = (self.secrets_dir / "root_ca.crt").read_bytes()
        root_public = self.public_key(["openssl", "x509", "-noout", "-pubkey"], root_certificate)
        password = self.secrets_dir / "password"
        for name in sorted(files):
            content = self.in_volume("cat", f"/home/step/{name}")
            if b"PRIVATE KEY" not in content:
                continue
            public = self.public_key(
                ["openssl", "pkey", "-pubout", "-passin", f"file:{password}"], content
            )
            expect(public != root_public, f"{name} is the root key")
        print(f"proof 3 held: {len(files)} files, all allow-listed; no key in it matches the root")

    def proof_4_restart(self) -> None:
        intermediate = self.in_volume("cat", "/home/step/certs/intermediate_ca.crt")
        before = (self.certificate_fingerprint(intermediate), self.volume_files())
        self.compose("restart", "step-ca")
        self.wait_until_answering()
        intermediate = self.in_volume("cat", "/home/step/certs/intermediate_ca.crt")
        after = (self.certificate_fingerprint(intermediate), self.volume_files())
        expect(after[0] == before[0], f"intermediate changed: {before[0]} -> {after[0]}")
        expect(before[1] <= after[1], f"db files lost: {sorted(before[1] - after[1])}")
        print(f"proof 4 held after restart: {after[0]}; {len(after[1])} files, none lost")

    def proof_5_partial(self) -> None:
        self.compose("stop", "step-ca")
        root_key = (self.secrets_dir / "root_ca_key").read_bytes()
        self.in_volume(
            "rm", "/home/step/certs/intermediate_ca.crt", "/home/step/secrets/intermediate_ca_key",
            writable=True,
        )  # fmt: skip
        expect("certs/intermediate_ca.crt" not in self.volume_files(), "the intermediate stayed")
        output = self.init()
        expect("intermediate" in output, f"init did not report the intermediate: {output[-120:]}")
        expect((self.secrets_dir / "root_ca_key").read_bytes() == root_key, "root key changed")
        expect(self.volume_files() >= ALLOWED_FILES, "the intermediate was not completed")
        self.compose("start", "step-ca")
        self.wait_until_answering()
        print("proof 5 held: init completed the intermediate; root key byte-for-byte unchanged")

    def teardown(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "10", check=False)
        left = self.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"]
        ).stdout.decode()
        volumes = self.run(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={PROJECT}",
            ]
        ).stdout.decode()
        expect(
            not (left.strip() or volumes.strip()), f"left behind: {left.split()} {volumes.split()}"
        )
        print(f"teardown: project {PROJECT} removed (containers and volume)")


def port_is_free() -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", 9000)) != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scratch", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.scratch.exists() and any(arguments.scratch.iterdir()):
        print("PROBE FAILED: --scratch is not empty")
        return 3
    if not port_is_free():
        print("PROBE FAILED: 127.0.0.1:9000 is taken")
        return 3
    arguments.scratch.mkdir(parents=True, exist_ok=True)
    probe = Probe(arguments.scratch)
    try:
        probe.proof_1_init()
        probe.proof_2_directory()
        probe.proof_3_volume()
        probe.proof_4_restart()
        probe.proof_5_partial()
    except (ProbeFailed, subprocess.TimeoutExpired, OSError) as failure:
        print(f"PROBE FAILED: {failure}")
        probe.teardown()
        return 1
    probe.teardown()
    print("PROBE PASSED: proofs 1-5 hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
