"""The world of an app in the tests: a real bare origin it is checked out from, and a scripted
Docker that holds the containers of its compose project (#255).

`Origin` is real git under `tmp_path`, reached through `url.<bare>.insteadOf` in the test's own
global git config, as the probe reaches it. `ScriptedDocker` answers the calls of
`edge.app_compose` and fails the test that makes one the contract forbids (a `down`, a `-v`, a
file-bound `ps`)."""

import fcntl
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from git_support import plain_git

from edge import apps, compose, switches

KEY = "hopin"
ORGANISATION = "overnightworks"
URL = f"https://github.com/{ORGANISATION}/{KEY}.git"
SENTINEL = "sentinel-6f1c9a3e"  # a synthetic sensitive value that must never be printed
COMPOSE = (
    f"services:\n  {KEY}:\n    build: .\n    restart: unless-stopped\n"
    "    healthcheck: {test: [CMD, 'true']}\n"
)
FLEET = (
    f"github:\n  org: {ORGANISATION}\n\nservices:\n  {KEY}:\n"
    "    repo: {default_branch: master}\n    run: {enabled: true}\n"
)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@dataclass
class Origin:
    """A bare `master` plus the work tree a test builds its history in."""

    bare: Path
    work: Path

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for name, content in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        plain_git(self.work, "add", "--all")
        if files:
            plain_git(self.work, "add", "--force", "--", *files)  # also what .gitignore ignores
        plain_git(
            self.work, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty",
            "-m", message,
        )  # fmt: skip
        return plain_git(self.work, "rev-parse", "HEAD").stdout.strip()

    def push(self, *references: str) -> None:
        plain_git(self.work, "push", "--quiet", "--force", "origin", *references)

    def tag(self, name: str, *, annotated: bool = False) -> None:
        arguments = ["-a", "-m", name] if annotated else []
        plain_git(self.work, "tag", "--force", *arguments, name)
        self.push(f"refs/tags/{name}")

    def release(self, tag: str, files: dict[str, str] | None = None) -> str:
        """A commit on `master` carrying `files`, tagged `tag`; its sha."""
        sha = self.commit(files or {"release": tag})
        self.push("master")
        self.tag(tag)
        return sha


def origin_at(tmp_path: Path, home: Path) -> Origin:
    """A bare origin whose `master` holds `COMPOSE`, reached as `URL` through `home`'s gitconfig."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    plain_git(tmp_path, "init", "--quiet", "--bare", "--initial-branch", "master", str(bare))
    plain_git(tmp_path, "init", "--quiet", "--initial-branch", "master", str(work))
    for key, value in (("user.name", "edge test"), ("user.email", "test@edge.invalid")):
        plain_git(work, "config", key, value)
    plain_git(work, "remote", "add", "origin", bare.as_uri())
    (home / ".gitconfig").write_text(f'[url "{bare.as_uri()}"]\n\tinsteadOf = {URL}\n')
    started = Origin(bare, work)
    started.commit({"compose.yaml": COMPOSE})
    started.push("master")
    return started


@dataclass
class Clock:
    """Time as a counter: sleeping moves it."""

    now: float = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def read(self) -> float:
        return self.now


def switch_lock_is_held(state: Path) -> bool:
    try:
        descriptor = os.open(state / switches.LOCK_FILE, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


@dataclass
class ScriptedDocker:
    """`docker compose -p <key>` over a list of containers; every call is recorded with whether the
    switch lock was held while it ran. `create` and `start` read the services of the checkout's
    compose.yaml; `ps` is asked file-free."""

    state: Path
    directory: Path
    rows: list[dict[str, str]] = field(default_factory=list)
    health_after_start: str = "healthy"
    failing_verb: str | None = None
    interrupted_verb: str | None = None
    calls: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)
    created: int = 0

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _, _ in self.calls]

    def locked_verbs(self) -> set[str]:
        return {verb for verb, _, held in self.calls if held}

    def put(self, service: str, state: str, health: str = "") -> None:
        self.created += 1
        name = f"{KEY}-{service}-{self.created}"
        self.rows.append({"Name": name, "Service": service, "State": state, "Health": health})

    def run(
        self, command: list[str], _timeout: int, _stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert command[:4] == ["docker", "compose", "-p", KEY], command
        rest = command[4:]
        bound = rest[0] == "--project-directory"
        if bound:
            assert rest[:4] == [
                "--project-directory", str(self.directory),
                "--file", str(self.directory / "compose.yaml"),
            ]  # fmt: skip
            rest = rest[4:]
        verb, *arguments = rest
        assert verb in {"ps", "build", "pull", "create", "start"}, f"docker compose {verb}"
        assert "-v" not in arguments
        assert bound == (verb != "ps"), f"{verb} is {'file-bound' if bound else 'file-free'}"
        self.calls.append((verb, tuple(arguments), switch_lock_is_held(self.state)))
        if verb == self.interrupted_verb:
            raise KeyboardInterrupt
        if verb == self.failing_verb:
            return subprocess.CompletedProcess(command, 3, SENTINEL, f"{SENTINEL}\n")
        return subprocess.CompletedProcess(command, 0, getattr(self, f"verb_{verb}")(arguments), "")

    def verb_ps(self, _arguments: list[str]) -> str:
        return "\n".join(json.dumps(row) for row in self.rows)

    def verb_build(self, _arguments: list[str]) -> str:
        return ""

    verb_pull = verb_build

    def verb_create(self, arguments: list[str]) -> str:
        assert arguments == ["--no-build", "--pull", "never", "--remove-orphans"]
        services = list(yaml.safe_load((self.directory / "compose.yaml").read_text())["services"])
        self.rows = []
        for service in services:
            self.put(service, "created")
        return ""

    def verb_start(self, _arguments: list[str]) -> str:
        for row in self.rows:
            if row["State"] == "created":
                row["State"], row["Health"] = "running", self.health_after_start
        return ""


def runtime_of(
    home: Path, state: Path, docker: ScriptedDocker, clock: Clock | None = None
) -> apps.AppRuntime:
    clock = clock or Clock()
    return apps.AppRuntime(
        srv=home / "srv",
        record=apps.DeploymentRecord(state / "deployed"),
        docker=docker.run,
        health=compose.Watch(clock.read, clock.sleep, apps.HEALTH_SECONDS),
        now=lambda: NOW,
    )
