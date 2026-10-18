"""`edge apply --closed`, `edge stop` and `edge start` against a scripted Compose and a scripted
setup check: no containers, and a `tmp_path` HOME so that no test touches the real switches."""

import io
import json
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from test_edge_core import bind_core_files, sha256

from edge import apply, barrier, catalog, cli, compose, emergency, setup, switches

REPOSITORY = Path(__file__).resolve().parents[1]
PROJECT_PREFIX = 6  # docker compose --project-directory D --file F
STEP_CA_SERVICES = ("step-ca", "traefik-mcp-acme-owner", "traefik-mcp")
ENABLED = "services:\n  step-ca:\n    run: {enabled: true, setup: step-ca/init}\n"
DISABLED = "services:\n  step-ca:\n    run: {enabled: false, setup: step-ca/init}\n"
SWITCHED_OFF_AT = "2026-10-17T22:10:04+02:00"
LATER_SWITCH_AT = "2026-10-17T23:30:00+02:00"
PROBE_ENABLED = ENABLED + "  probe:\n    run: {enabled: true}\n"
PROBE_DISABLED = ENABLED + "  probe:\n    run: {enabled: false}\n"
NOT_FOUND = barrier.Answer(404, b"404 page not found\n", "probe")  # the barrier of unit `probe`
PLAIN_NOT_FOUND = barrier.Answer(404, b"404 page not found\n")  # Traefik's no-route answer
WHOAMI = barrier.Answer(200, b"Hostname: abc\n")
NOW = datetime.fromisoformat("2026-10-18T08:00:00+02:00")
NEVER_RUN = {"down", "-v", "--volumes", "-V", "--renew-anon-volumes"}


def resolved_configuration() -> dict:
    def volume(name: str) -> list[dict]:
        return [{"type": "volume", "source": name, "target": "/data"}]

    return {
        "services": {
            "socket-proxy": {"restart": "unless-stopped"},
            "traefik": {"restart": "unless-stopped", "environment": {"EDGE_DOMAIN": "probe.test"}},
            "hello": {
                "profiles": ["probe"],
                "labels": {"traefik.enable": "true", "edge.name": "hello"},
            },
            "keycloak": {"restart": "unless-stopped", "profiles": ["keycloak"]},
            "step-ca": {
                "restart": "unless-stopped",
                "profiles": ["step-ca"],
                "volumes": volume("step-ca"),
            },
            "traefik-mcp-acme-owner": {
                "restart": "no",
                "profiles": ["step-ca"],
                "volumes": volume("traefik-mcp-acme"),
            },
            "traefik-mcp": {
                "restart": "unless-stopped",
                "profiles": ["step-ca"],
                "volumes": volume("traefik-mcp-acme"),
                "depends_on": {
                    "traefik-mcp-acme-owner": {"condition": "service_completed_successfully"}
                },
            },
        }
    }


@dataclass
class ScriptedCompose:
    """Compose over a table of service -> container state; records every call.

    Like Compose, `up` recreates a container whose service definition changed since it was
    created (it gets a new id) unless `--no-recreate` is given, and always with
    `--force-recreate`. Edge never takes a project down or renews its volumes, so `down`, `-v`
    and `--renew-anon-volumes` fail the test that runs them (#85 P13, cell U14)."""

    containers: dict[str, str] = field(default_factory=dict)
    container_ids: dict[str, int] = field(default_factory=dict)
    changed_definitions: set[str] = field(default_factory=set)
    containers_created: int = 0
    volumes: dict[str, str] = field(default_factory=dict)  # volume key -> what it holds
    failing_verb: str | None = None
    starts_nothing: bool = False
    after_up: Callable[[], None] | None = None  # what changes in the world once `up` returned
    # a container of these dies right after `up` starts it and Docker restarts it: the first `ps`
    # after the `up` still sees it running, every later one restarting
    crash_loops: set[str] = field(default_factory=set)
    just_started: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)
    config: dict = field(default_factory=resolved_configuration)

    def run(self, command: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(command[PROJECT_PREFIX:])
        _, profile, verb, *arguments = command[PROJECT_PREFIX:]
        refused = NEVER_RUN & {verb, *arguments}
        assert not refused, f"edge ran docker compose {verb} {' '.join(arguments)}"
        if verb == self.failing_verb:
            return subprocess.CompletedProcess(command, 1, "", f"{verb} broke\nlast line")
        output = getattr(self, f"verb_{verb}")(profile, arguments)
        return subprocess.CompletedProcess(command, 0, output, "")

    def verb_config(self, _profile: str, _arguments: list[str]) -> str:
        return json.dumps(self.config)

    def verb_ps(self, _profile: str, _arguments: list[str]) -> str:
        output = "\n".join(
            json.dumps({"Service": service, "State": state, "ExitCode": 0})
            for service, state in self.containers.items()
        )
        for service in self.just_started & self.crash_loops:
            self.containers[service] = "restarting"
        self.just_started.clear()
        return output

    def verb_up(self, _profile: str, arguments: list[str]) -> str:
        if self.starts_nothing:
            return ""
        one_shots = compose.one_shot_services(self.config)
        flags = {argument for argument in arguments if argument.startswith("-")}
        for service in (argument for argument in arguments if argument not in flags):
            if self.needs_a_new_container(service, flags):
                self.create_container(service)
            self.containers[service] = "exited" if service in one_shots else "running"
            self.just_started.add(service)
            for volume in compose.named_volumes(self.config["services"][service]):
                self.volumes.setdefault(volume, "created by up")
        if self.after_up:
            self.after_up()
        return ""

    def create_container(self, service: str) -> None:
        self.containers_created += 1
        self.container_ids[service] = self.containers_created
        self.changed_definitions.discard(service)

    def put_containers(self, states: dict[str, str]) -> None:
        """Containers that exist before the command, each with its own id."""
        for service, container_state in states.items():
            self.create_container(service)
            self.containers[service] = container_state

    def needs_a_new_container(self, service: str, flags: set[str]) -> bool:
        if service not in self.containers or "--force-recreate" in flags:
            return True
        return service in self.changed_definitions and "--no-recreate" not in flags

    def verb_stop(self, _profile: str, arguments: list[str]) -> str:
        for service in arguments:
            if self.containers.get(service) == "running":
                self.containers[service] = "exited"
        return ""

    def verb_rm(self, _profile: str, arguments: list[str]) -> str:
        for service in arguments[1:]:
            self.containers.pop(service, None)
            self.container_ids.pop(service, None)
        return ""

    def changing_verbs(self) -> list[str]:
        return [call[2] for call in self.calls if call[2] not in {"config", "ps"}]


@dataclass
class Setup:
    """The scripted `<setup> --check`: its exit code and line."""

    exit_code: int = 0
    line: str = "step-ca/init --check: set up"
    commands: list[list[str]] = field(default_factory=list)
    failing_command: str | None = None  # this command's check exits 1, whatever `exit_code` says

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.failing_command and command[0].endswith(self.failing_command):
            return subprocess.CompletedProcess(
                command, 1, f"{self.failing_command}: not set up", ""
            )
        return subprocess.CompletedProcess(command, self.exit_code, self.line, "")


@dataclass
class SetupRun:
    """The scripted run of a setup in the terminal: a successful run makes `--check` pass."""

    checks: Setup
    exit_code: int = 0
    interrupted: bool = False
    cannot_start: bool = False
    check_afterwards: tuple[int, str] = (0, "step-ca/init --check: set up")
    commands: list[tuple[Path, Path]] = field(default_factory=list)
    meanwhile: Callable[[], None] | None = None  # what changes while the setup runs

    def __call__(self, command: Path, working_directory: Path) -> setup.SetupRun:
        self.commands.append((command, working_directory))
        if self.meanwhile:
            self.meanwhile()
        if self.cannot_start:
            raise PermissionError("not executable")
        if self.exit_code == 0 and not self.interrupted:
            self.checks.exit_code, self.checks.line = self.check_afterwards
        return setup.SetupRun(self.exit_code, self.interrupted)


class Terminal(io.StringIO):
    """A terminal; `while_prompt_waits` runs when the operator's answer is read."""

    while_prompt_waits: Callable[[], None] | None = None

    def isatty(self) -> bool:
        return True

    def readline(self, *arguments) -> str:
        if self.while_prompt_waits:
            self.while_prompt_waits()
        return super().readline(*arguments)


class InterruptedWaitForLock(switches.Switches):
    """Switches whose lock is taken by a Ctrl-C while it waits."""

    def lock(self):
        raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stray real marker would keep a live unit down: every test gets its own HOME."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def state(home: Path) -> Path:
    return switches.default_state_directory()


def marker(state: Path, unit: str = "step-ca") -> str | None:
    return switches.Switches(state).since(unit)


def switch_off(state: Path, since: str = SWITCHED_OFF_AT, unit: str = "step-ca") -> None:
    switches.Switches(state).record(unit, datetime.fromisoformat(since))


def edge_tree(tree: Path) -> Path:
    """A tree of this repository's shape at `tree`, a main worktree (its `.git` is a directory)."""
    (tree / ".git").mkdir(parents=True)
    for directory in ("step-ca", "keycloak"):
        (tree / directory).mkdir(exist_ok=True)
        setup = tree / directory / "init"
        setup.write_text("#!/bin/sh\n")
        setup.chmod(setup.stat().st_mode | stat.S_IXUSR)
    (tree / "fleet.yaml").write_text(ENABLED)
    (tree / "dynamic").mkdir()
    (tree / "traefik.yml").write_text('entryPoints:\n  edge:\n    address: "127.0.0.1:8081"\n')
    return tree


@pytest.fixture
def repository(home: Path) -> Path:
    """The live checkout `$HOME/git/edge` of the test's own HOME (#79 cell 1)."""
    return edge_tree(home / "git" / "edge")


@dataclass
class Ingress:
    """Traefik as the barrier tests see it: it answers a marked 404 for `hello` while the unit's
    barrier file is in `dynamic/`; a file it is `rejecting` leaves its plain no-route 404; `down`
    refuses the connection. Time is a counter."""

    repository: Path
    rejecting: bool = False
    down: bool = False
    asked: list[str] = field(default_factory=list)  # what the world looked like at each request
    on_fetch: Callable[[], None] | None = None  # a look at the world as each request arrives
    now: float = 0.0

    def barrier_file(self) -> Path:
        return self.repository / "dynamic" / "switch-probe.yml"

    def fetch(
        self, _address: str, _host: str, _seconds: float, _clock: Callable[[], float]
    ) -> barrier.Answer:
        self.asked.append("barrier" if self.barrier_file().exists() else "no barrier")
        if self.on_fetch:
            self.on_fetch()
        if self.down:
            raise ConnectionRefusedError
        if not self.barrier_file().exists():
            return WHOAMI
        return PLAIN_NOT_FOUND if self.rejecting else NOT_FOUND

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    @property
    def verifier(self) -> barrier.Verifier:
        return barrier.Verifier(self.fetch, lambda: self.now, self.sleep)

    @property
    def watch(self) -> compose.Watch:
        """How a recreated Traefik is watched, on the same counter."""
        return compose.Watch(lambda: self.now, self.sleep)


@pytest.fixture
def ingress(repository: Path) -> Ingress:
    return Ingress(repository)


@pytest.fixture
def scripted() -> ScriptedCompose:
    return ScriptedCompose()


@pytest.fixture
def setup_check() -> Setup:
    return Setup()


@pytest.fixture
def setup_run(setup_check: Setup) -> SetupRun:
    return SetupRun(setup_check)


@pytest.fixture
def at_terminal(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Stdin and stdout are a terminal; the operator will type `answer`."""

    def operator_types(answer: str, while_prompt_waits: Callable[[], None] | None = None) -> None:
        keyboard = Terminal(f"{answer}\n")
        keyboard.while_prompt_waits = while_prompt_waits
        monkeypatch.setattr(sys, "stdin", keyboard)
        monkeypatch.setattr(sys, "stdout", Terminal())

    return operator_types


@pytest.fixture
def switch(repository, scripted, setup_check, setup_run, at_terminal, capsys, state, home, ingress):
    """Run `edge apply --closed` from the live checkout; the operator types `yes` unless told
    otherwise."""

    def run(answer: str = "yes", while_prompt_waits=None) -> tuple[int, str, str]:
        at_terminal(answer, while_prompt_waits)
        exit_code = apply.apply_closed(
            state,
            compose.Compose(scripted.run, repository),
            repository,
            setup_check,
            setup_run,
            home,
            ingress.verifier,
            ingress.watch,
        )
        return exit_code, sys.stdout.getvalue(), capsys.readouterr().err

    return run


@pytest.fixture
def stop(repository, scripted, capsys, state, ingress):
    """Run `edge stop <unit>` with no terminal: the path an agent takes."""

    def run(unit: str = "step-ca", marks: switches.Switches | None = None) -> tuple[int, str, str]:
        exit_code = emergency.stop_unit(
            marks or switches.Switches(state),
            unit,
            NOW,
            compose.Compose(scripted.run, repository),
            repository,
            ingress.verifier,
        )
        captured = capsys.readouterr()
        return exit_code, captured.out, captured.err

    return run


@pytest.fixture
def start(repository, scripted, at_terminal, capsys, state, ingress, setup_check):
    """Run `edge start <unit>`; the operator types `yes` unless told otherwise."""

    def run(
        answer: str = "yes", unit: str = "step-ca", while_prompt_waits=None
    ) -> tuple[int, str, str]:
        at_terminal(answer, while_prompt_waits)
        exit_code = emergency.start_unit(
            switches.Switches(state),
            unit,
            compose.Compose(scripted.run, repository),
            repository,
            ingress.verifier,
            setup_check,
        )
        return exit_code, sys.stdout.getvalue(), capsys.readouterr().err

    return run


def write_fleet(repository: Path, text: str) -> None:
    (repository / "fleet.yaml").write_text(text)


def test_switching_on_starts_exactly_the_profiles_services(switch, scripted):
    """#52 rule 2: on names exactly the services of the profile; #75 rule 1: never recreating."""
    exit_code, out, _ = switch()

    assert exit_code == 0
    assert scripted.containers == {
        "step-ca": "running",
        "traefik-mcp-acme-owner": "exited",
        "traefik-mcp": "running",
    }
    starting = ["up", "-d", "--no-recreate", *STEP_CA_SERVICES]
    assert ["--profile", "step-ca", *starting] in scripted.calls
    assert "step-ca is on" in out


def test_switching_on_asks_the_setup_check_first(switch, setup_check, repository):
    """#52 rule 5: a unit that would be switched on is checked with `<setup> --check` first."""
    switch()

    assert setup_check.commands == [[str(repository / "step-ca/init"), "--check"]]


def test_switching_off_removes_the_containers_and_names_the_volumes_that_stay(
    switch, scripted, repository
):
    """#52 rules 2, 3, 6: off removes the containers and the plan names the volumes that stay."""
    write_fleet(repository, DISABLED)
    scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "running")

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert scripted.containers == {}
    assert "volumes step-ca, traefik-mcp-acme stay" in out
    assert "step-ca is off" in out


def test_a_unit_already_as_the_catalog_says_changes_nothing_and_asks_nothing(
    switch, scripted, setup_check
):
    """#52 rule 6 / P15: a second run changes nothing and asks no `yes`."""
    switch()
    scripted.calls.clear()

    exit_code, out, _ = switch(answer="no")

    assert exit_code == 0
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []
    assert setup_check.commands[1:] == []


def test_switching_off_what_is_not_there_changes_nothing(switch, scripted, repository):
    """#52 rule 6: off with no container left is "nothing to change"."""
    write_fleet(repository, DISABLED)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []


def test_a_one_shot_container_left_behind_is_removed_when_switched_off(
    switch, scripted, repository
):
    """#52 rule 6: a unit is off only when no container of its services exists."""
    write_fleet(repository, DISABLED)
    scripted.containers = {"traefik-mcp-acme-owner": "exited"}

    assert switch()[0] == 0
    assert scripted.containers == {}


def test_off_never_takes_a_volume_or_a_project_down_and_never_names_the_core(
    switch, scripted, repository
):
    """#52 rule 2: no `down`, `-v` or volume command; while the core's files are as recorded
    (here it has none), traefik and socket-proxy are never named (#85 rule 3)."""
    switch()
    write_fleet(repository, DISABLED)
    switch()
    write_fleet(repository, ENABLED)
    switch()

    words = {word for call in scripted.calls for word in call}
    assert not words & {"down", "-v", "--volumes", "volume"}
    changing = [call for call in scripted.calls if call[2] not in {"config", "ps"}]
    assert not {"traefik", "socket-proxy"} & {word for call in changing for word in call}
    assert {call[2] for call in changing} == {"up", "stop", "rm"}


def test_a_failed_removal_is_partial_and_never_reported_as_applied(switch, scripted, repository):
    """#52 rule 6: a failed `rm -f` is partial (exit 1), never applied."""
    write_fleet(repository, DISABLED)
    scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "running")
    scripted.failing_verb = "rm"

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial" in err
    assert "step-ca" in err
    assert "last line" in err
    assert "is off" not in out


def test_a_profile_of_servers_without_a_restart_policy_is_on_only_while_they_run(
    switch, scripted, repository
):
    """#52 P15: servers that exited 0 are stopped, not done; only a one-shot may be done."""
    scripted.config["services"]["hello"] = {"profiles": ["probe"]}
    scripted.config["services"]["hello-mcp"] = {"profiles": ["probe"]}
    write_fleet(repository, "services:\n  probe:\n    run: {enabled: true}\n")
    scripted.containers = {"hello": "exited", "hello-mcp": "exited"}

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "nothing to change" not in out
    assert scripted.containers == {"hello": "running", "hello-mcp": "running"}


def test_switching_on_off_on_keeps_the_volumes_and_what_they_hold(switch, scripted, repository):
    """#52 R1 / rule 3: off removes containers only, so on serves the same volume contents."""
    scripted.volumes = {"step-ca": "intermediate-A", "traefik-mcp-acme": "issued-B"}
    held = dict(scripted.volumes)

    switch()
    write_fleet(repository, DISABLED)
    switch()
    assert scripted.containers == {}
    assert scripted.volumes == held
    write_fleet(repository, ENABLED)
    exit_code, _, _ = switch()

    assert exit_code == 0
    assert scripted.volumes == held
    assert scripted.containers["step-ca"] == "running"


def test_a_start_that_leaves_the_unit_down_is_partial(switch, scripted):
    """#52 rule 6 / P15: success only for observed state."""
    scripted.starts_nothing = True

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, step-ca is not on after the change" in err
    assert "is on" not in out


# --- #75: switching on starts existing containers as they are, never recreates them -------------

STEP_CA_ON = {"step-ca": "running", "traefik-mcp-acme-owner": "exited", "traefik-mcp": "running"}
APP_EXITED = STEP_CA_ON | {"traefik-mcp": "exited"}
SWITCH_ON_CELLS = {
    # cell: (containers before, services whose definition changed, the line apply prints)
    "cell 1, all running, nothing changed": (STEP_CA_ON, set(), "nothing to change"),
    "cell 2, db running, app exited, db image changed": (APP_EXITED, {"step-ca"}, "step-ca is on"),
    "cell 4, all exited, definitions changed": (
        dict.fromkeys(STEP_CA_SERVICES, "exited"),
        set(STEP_CA_SERVICES),
        "step-ca is on",
    ),
    "cell 5, no containers, the unit was off": ({}, set(), "step-ca is on"),
    "cell 6, one-shot already exited, nothing changed": (APP_EXITED, set(), "step-ca is on"),
}


@pytest.mark.parametrize(
    ("before", "changed", "line"), SWITCH_ON_CELLS.values(), ids=SWITCH_ON_CELLS
)
def test_switching_on_keeps_every_existing_container_and_the_volumes(
    switch, scripted, before, changed, line
):
    """#75 rule 1, cells 1, 2, 4, 5, 6: an existing container keeps its id, a missing one is
    created, the volumes are untouched, and the unit is on."""
    scripted.volumes = {"step-ca": "intermediate-A", "traefik-mcp-acme": "issued-B"}
    held = dict(scripted.volumes)
    scripted.put_containers(before)
    scripted.changed_definitions = set(changed)
    existing_ids = dict(scripted.container_ids)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert line in out
    assert scripted.containers == STEP_CA_ON
    assert {service: scripted.container_ids[service] for service in existing_ids} == existing_ids
    assert scripted.volumes == held


def test_a_switch_on_whose_start_fails_is_partial_as_before(switch, scripted):
    """#75 cell 7 / #52 P15: a failed `up` is partial with Compose's last line, never on."""
    scripted.failing_verb = "up"

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, step-ca: could not start, `docker compose up` exited 1: last line" in err
    assert "is on" not in out


def test_an_interrupt_names_the_unit_and_says_its_state_may_be_partial(switch, scripted, capsys):
    """#52 rule 6: an interrupt names the unit and says its state may be partial (exit 1)."""

    def interrupted_start(*_arguments):
        raise KeyboardInterrupt

    scripted.verb_up = interrupted_start

    exit_code, _, err = switch()

    assert exit_code == 1
    assert "interrupted while changing step-ca" in err
    assert "may be partial" in err


NOT_SET_UP_LINE = "step-ca/init --check: not set up: password is not done"
QUESTION = "Type 'yes' to run `step-ca/init` now"


@pytest.fixture
def missing_setup(setup_check: Setup) -> Setup:
    setup_check.exit_code, setup_check.line = 1, NOT_SET_UP_LINE
    return setup_check


def test_a_missing_setup_is_asked_separately_before_the_plan(
    switch, scripted, missing_setup, setup_run, repository
):
    """#69 rules 1, 4, 5: `yes` runs the setup, `--check` passes, the plan has its own yes."""
    exit_code, out, _ = switch("yes\nyes")

    assert exit_code == 0
    assert f"step-ca is not set up: {NOT_SET_UP_LINE}" in out
    assert out.index(QUESTION) < out.index("the plan") < out.index("apply exactly this plan")
    assert setup_run.commands == [(repository / "step-ca/init", repository)]
    assert missing_setup.commands[-1][-1] == "--check"
    assert scripted.containers["step-ca"] == "running"


def test_one_yes_does_not_cover_both_the_setup_and_the_plan(switch, scripted, missing_setup):
    """#69 rule 1: the plan has its own question; a declined plan names the completed setup."""
    exit_code, _, err = switch("yes\nno")

    assert exit_code == 1
    assert "setup of step-ca completed; nothing switched" in err
    assert "nothing changed" not in err
    assert scripted.changing_verbs() == []


def test_declining_the_setup_aborts_everything_and_runs_nothing(
    switch, scripted, missing_setup, setup_run
):
    """#69 rule 3: `no` aborts: nothing runs, nothing is switched."""
    exit_code, _, err = switch("no")

    assert exit_code == 1
    assert "aborted, nothing changed; run `step-ca/init` yourself or answer yes" in err
    assert setup_run.commands == []
    assert scripted.changing_verbs() == []


def test_a_closed_stdin_counts_as_no_to_the_setup(switch, missing_setup, setup_run):
    """#69 rule 8: EOF is a no."""
    exit_code, _, _ = switch("")

    assert exit_code == 1
    assert setup_run.commands == []


def test_a_set_up_unit_is_not_asked(switch, setup_run):
    """#69 rule 1: only a unit whose check says not set up is asked."""
    _, out, _ = switch()

    assert "not set up" not in out
    assert setup_run.commands == []


def test_a_unit_switched_off_is_never_asked_about_its_setup(
    switch, repository, missing_setup, setup_run
):
    """#69 rule 2: switching a unit off needs no setup."""
    write_fleet(repository, DISABLED)

    switch()

    assert setup_run.commands == []
    assert missing_setup.commands == []


def test_a_unit_kept_stopped_by_its_marker_is_never_asked(switch, state, missing_setup, setup_run):
    """#69 rule 2: STAYS_STOPPED is not switched on."""
    switch_off(state)

    exit_code, _, _ = switch()

    assert exit_code == 0
    assert setup_run.commands == []


@pytest.mark.parametrize(
    ("setup_outcome", "expected_exit", "expected_words"),
    [
        ({"exit_code": 3}, 1, "failed (exit 3)"),
        ({"exit_code": 130, "interrupted": True}, 1, "interrupted during `step-ca/init`"),
        ({"cannot_start": True}, 2, "cannot start `step-ca/init`"),
    ],
)
def test_a_failed_interrupted_or_unstartable_setup_switches_nothing(
    switch, scripted, missing_setup, setup_run, setup_outcome, expected_exit, expected_words
):
    """#69 rules 5, 6: nothing is switched after a setup that did not succeed."""
    for name, value in setup_outcome.items():
        setattr(setup_run, name, value)

    exit_code, _, err = switch("yes\nyes")

    assert exit_code == expected_exit
    assert expected_words in err
    assert len(missing_setup.commands) == 1  # the first check only: no re-check
    assert scripted.changing_verbs() == []


def test_an_interrupted_setup_says_that_keys_may_exist_and_how_to_go_on(
    switch, missing_setup, setup_run
):
    """#69 rule 6."""
    setup_run.interrupted = True

    _, _, err = switch("yes")

    assert "nothing switched; keys may already exist; run `edge apply --closed` again" in err


def test_a_setup_that_leaves_the_check_failing_is_exit_1(
    switch, scripted, missing_setup, setup_run
):
    """#69 rule 5: --check still 1 after the setup."""
    setup_run.check_afterwards = (1, "still no keys")

    exit_code, _, err = switch("yes\nyes")

    assert exit_code == 1
    assert "`step-ca/init` ran, still not set up: still no keys" in err
    assert scripted.changing_verbs() == []


def test_a_recheck_that_cannot_tell_after_the_setup_is_exit_2(
    switch, scripted, missing_setup, setup_run
):
    """#69 rule 5: --check exits 2 after the setup."""
    setup_run.check_afterwards = (2, "docker is down")

    exit_code, _, err = switch("yes\nyes")

    assert exit_code == 2
    assert "docker is down" in err
    assert scripted.changing_verbs() == []


TWO_SETUPS = (
    ENABLED + "  keycloak:\n    run: {enabled: true, setup: keycloak/init}\n"
)  # catalog order: step-ca, keycloak


def test_several_missing_setups_are_all_asked_first_and_a_no_stops_the_rest(
    switch, repository, missing_setup, setup_run
):
    """#69 rule 14: all questions in catalog order, then the runs; a `no` runs none."""
    write_fleet(repository, TWO_SETUPS)

    exit_code, out, _ = switch("yes\nno")

    assert exit_code == 1
    assert out.count("is not set up") == 2
    assert out.index("step-ca is not set up") < out.index("keycloak is not set up")
    assert setup_run.commands == []


def test_several_missing_setups_run_in_catalog_order_before_the_plans_own_question(
    switch, repository, missing_setup, setup_run
):
    """#69 rule 14: questions in catalog order, the setups in turn, then the plan's own yes."""
    write_fleet(repository, TWO_SETUPS)

    exit_code, out, _ = switch("yes\nyes\nyes")

    assert exit_code == 0
    assert (
        out.index("run `step-ca/init` now")
        < out.index("run `keycloak/init` now")
        < out.index("apply exactly this plan")
    )
    assert setup_run.commands == [
        (repository / "step-ca/init", repository),
        (repository / "keycloak/init", repository),
    ]


def test_a_failing_setup_stops_the_rest(switch, repository, missing_setup, setup_run):
    """#69 rule 14: a failed setup runs no later one."""
    write_fleet(repository, TWO_SETUPS)
    setup_run.exit_code = 1

    exit_code, _, _ = switch("yes\nyes")

    assert exit_code == 1
    assert setup_run.commands == [(repository / "step-ca/init", repository)]


def test_every_run_asks_again_until_the_check_says_set_up(
    switch, scripted, missing_setup, setup_run
):
    """#69 rule 9: nothing is remembered; a `no` is asked again, a passing check is not."""
    exit_code, first, _ = switch("no")
    assert exit_code == 1
    assert "step-ca is not set up" in first

    _, second, _ = switch("no")
    assert "step-ca is not set up" in second

    missing_setup.exit_code = 0
    exit_code, third, _ = switch("yes")

    assert exit_code == 0
    assert "not set up" not in third
    assert setup_run.commands == []


def test_the_plan_is_made_again_after_the_setup_from_the_states_it_left(
    switch, state, missing_setup, setup_run
):
    """#69 rule 1: states are re-read after the setup; a switch set meanwhile holds the unit."""
    setup_run.meanwhile = lambda: switch_off(state)

    exit_code, out, _ = switch("yes")

    assert exit_code == 0
    assert "nothing to change" in out


def test_a_unit_that_became_on_during_the_setup_is_refused_without_a_question_and_nothing_switches(
    switch, repository, scripted, missing_setup, setup_run
):
    """#69 rule 1: a unit that became ON during the setup still needs its own set-up check."""

    def enable_keycloak_whose_check_fails() -> None:
        write_fleet(repository, TWO_SETUPS)
        missing_setup.failing_command = "keycloak/init"

    setup_run.meanwhile = enable_keycloak_whose_check_fails

    exit_code, _, err = switch("yes")

    assert exit_code == 1
    assert "keycloak is not set up" in err
    assert scripted.changing_verbs() == []


def test_a_setup_check_that_cannot_tell_is_cannot_plan_and_quotes_its_line(
    switch, scripted, setup_check
):
    """#52 rule 5: a setup check that cannot tell is exit 2 and quoted."""
    setup_check.exit_code, setup_check.line = 2, "Cannot connect to the Docker daemon"

    exit_code, _, err = switch()

    assert exit_code == 2
    assert "Cannot connect to the Docker daemon" in err
    assert scripted.changing_verbs() == []


def test_a_setup_that_is_set_up_is_only_ever_checked_never_run(switch, setup_check, setup_run):
    """#69 rule 1: without a missing setup the command is only called with `--check`."""
    switch()

    assert all(command[-1] == "--check" for command in setup_check.commands)
    assert setup_run.commands == []


@pytest.fixture(params=["live checkout", "another copy"])
def any_tree(request, repository: Path, tmp_path: Path) -> Path:
    return repository if request.param == "live checkout" else edge_tree(tmp_path / "x" / "edge")


def test_without_a_terminal_it_refuses_before_anything_else(
    any_tree, scripted, missing_setup, setup_run, monkeypatch, capsys, state, home
):
    """#52 rule 4: no terminal refuses first (exit 1); #79 cell 6: before the live-checkout check,
    from any tree."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))

    exit_code = apply.apply_closed(
        state, compose.Compose(scripted.run, any_tree), any_tree, missing_setup, setup_run, home
    )

    assert exit_code == 1
    assert "refused, stdin and stdout must be a terminal" in capsys.readouterr().err
    assert scripted.calls == []
    assert missing_setup.commands == []
    assert setup_run.commands == []


def linked_worktree(tree: Path) -> Path:
    """`tree` turned into a linked worktree: its `.git` is a file pointing at the main one."""
    (tree / ".git").rmdir()
    (tree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/issue-9-y\n")
    return tree


@pytest.mark.parametrize(
    "foreign_tree",
    [
        pytest.param(
            lambda home, tmp: linked_worktree(edge_tree(tmp / "edge-worktrees" / "issue-9-y")),
            id="cell 2: a linked worktree",
        ),
        pytest.param(lambda home, tmp: edge_tree(tmp / "x" / "edge"), id="cell 3: another copy"),
        pytest.param(
            lambda home, tmp: linked_worktree(home / "git" / "edge"),
            id="cell 5: ~/git/edge with a .git file",
        ),
    ],
)
def test_outside_the_live_checkout_it_refuses_naming_the_tree_and_runs_nothing(
    repository, scripted, setup_check, setup_run, at_terminal, capsys, state, home, tmp_path,
    foreign_tree,
):  # fmt: skip
    """#79 rule 1, cells 2, 3 and 5: exit 2, one line naming the tree, no compose call."""
    tree = foreign_tree(home, tmp_path)
    at_terminal("yes")

    exit_code = apply.apply_closed(
        state, compose.Compose(scripted.run, tree), tree, setup_check, setup_run, home
    )

    assert exit_code == 2
    assert capsys.readouterr().err.splitlines() == [
        f"edge apply --closed: runs only from the live checkout ~/git/edge (this is {tree})"
    ]
    assert scripted.calls == []
    assert setup_check.commands == []
    assert setup_run.commands == []


def test_the_live_checkout_reached_through_a_symlinked_home_applies(
    repository, scripted, setup_check, setup_run, at_terminal, state, home, tmp_path
):
    """#79 cell 4: a HOME that resolves to the code tree's home proceeds as from the checkout."""
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(home)
    at_terminal("yes")

    exit_code = apply.apply_closed(
        state, compose.Compose(scripted.run, repository), repository, setup_check, setup_run,
        linked_home,
    )  # fmt: skip

    assert exit_code == 0
    assert scripted.containers["step-ca"] == "running"


@pytest.mark.parametrize("answer", ["no", "", "Yes", "y", "yes please"])
def test_only_the_exact_yes_applies(switch, scripted, answer):
    """#52 rule 4: any answer but the exact `yes` aborts with nothing changed."""
    exit_code, _, err = switch(answer)

    assert exit_code == 1
    assert "aborted, nothing changed" in err
    assert scripted.changing_verbs() == []


def test_plain_apply_says_the_publish_phase_is_not_built_and_names_closed(capsys):
    """#52 rule 7: plain `edge apply` exits 2 and names `--closed`."""
    exit_code = cli.main(["apply"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "publish phase is not built yet" in err
    assert "edge apply --closed" in err


@pytest.mark.parametrize(
    ("fleet", "named"),
    [
        ("- a\n- b\n", "fleet.yaml: must be a map"),
        ("services: false\n", "services: must be a map"),
        ("services: []\n", "services: must be a map"),
        ("services:\n  stepca:\n    run: {enabled: true}\n", "services.stepca"),
        ("services:\n  step-ca:\n    run: {enabled: 'yes'}\n", "services.step-ca.run.enabled"),
        ("services:\n  step-ca:\n    run: {}\n", "services.step-ca.run.enabled"),
        (
            "services:\n  step-ca:\n    run: {enabled: true, health: /up}\n",
            "services.step-ca.run.health",
        ),
        (
            "services:\n  step-ca:\n    run: {enabled: true, setup: step-ca/missing}\n",
            "services.step-ca.run.setup",
        ),
        (
            "services:\n  step-ca:\n    run: {enabled: true, setup: fleet.yaml}\n",
            "services.step-ca.run.setup",
        ),
        (
            "services:\n  step-ca:\n    repo: overnightworks/x\n    run: {enabled: true}\n",
            "apps follow with block 6",
        ),
    ],
)
def test_an_invalid_catalog_is_cannot_plan_with_one_line_naming_the_key(
    switch, scripted, repository, fleet, named
):
    """#52 rule 1: each catalog counterexample is exit 2 with a line that names the key."""
    write_fleet(repository, fleet)

    exit_code, _, err = switch()

    assert exit_code == 2
    assert named in err
    assert scripted.changing_verbs() == []


def test_the_line_for_an_unknown_profile_lists_the_profiles_that_exist(switch, repository):
    """#52 rule 1: the unknown-profile line lists the profiles that exist."""
    write_fleet(repository, "services:\n  stepca:\n    run: {enabled: true}\n")

    _, _, err = switch()

    assert "profiles: keycloak, probe, step-ca" in err


def test_an_entry_without_run_is_ignored(switch, scripted, repository):
    """#52 rule 1: an entry without `run` belongs to other readers."""
    write_fleet(repository, "services:\n  songmaker:\n    repo: overnightworks/songmaker\n")

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "nothing to change" in out


def test_compose_that_cannot_be_read_is_cannot_plan(switch, scripted):
    """#52 rule 6: unreadable Compose configuration is exit 2."""
    scripted.failing_verb = "config"

    exit_code, _, err = switch()

    assert exit_code == 2
    assert "cannot plan" in err
    assert "last line" in err


def test_a_routed_unit_without_traefiks_edge_domain_is_cannot_plan(switch, scripted, repository):
    """#85 rule 7: a barrier that cannot be named is cannot plan, exit 2, and changes nothing."""
    write_fleet(repository, PROBE_ENABLED)
    del scripted.config["services"]["traefik"]["environment"]["EDGE_DOMAIN"]

    exit_code, _, err = switch()

    assert exit_code == 2
    assert "cannot plan, compose.yaml gives traefik no EDGE_DOMAIN" in err
    assert scripted.changing_verbs() == []


# --- catalog and compose cannot drift (catalog -> compose) --------------------------------------


def repository_profiles() -> dict[str, tuple[str, ...]]:
    return compose.profile_services(yaml.safe_load((REPOSITORY / "compose.yaml").read_text()))


def test_every_unit_of_the_repositorys_catalog_names_a_compose_profile_with_services():
    """#52 rule 9: the repository's catalog and compose.yaml do not drift."""
    document = yaml.safe_load((REPOSITORY / "fleet.yaml").read_text())

    units = catalog.units(document, repository_profiles(), REPOSITORY)

    assert {unit.key for unit in units} >= {"step-ca"}


def test_a_catalog_key_without_a_compose_profile_fails_the_drift_check():
    """#52 rule 9: the drift check fails on a key without a profile."""
    document = {"services": {"keycloack": {"run": {"enabled": False}}}}
    profiles = repository_profiles()

    with pytest.raises(catalog.CatalogError, match=r"services\.keycloack"):
        catalog.units(document, profiles, REPOSITORY)


# --- the emergency switch (#60): marker, edge stop, edge start, apply's precedence ---------------


def put_unit_in(situation: str, scripted, repository, state) -> None:
    """A: on, no marker. B: switched off, stopped. E: disabled in the catalog, gone."""
    if situation == "A":
        scripted.containers = {
            "step-ca": "running",
            "traefik-mcp-acme-owner": "exited",
            "traefik-mcp": "running",
        }
    if situation == "B":
        scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "exited")
        switch_off(state)
    if situation == "E":
        write_fleet(repository, DISABLED)


# (exit, marker afterwards, changing verbs, asked for `yes`)
STATE_TABLE = {
    ("A", "stop"): (0, True, ["stop"], False),
    ("B", "stop"): (0, True, [], False),
    ("E", "stop"): (0, False, [], False),
    ("A", "start"): (0, False, [], False),
    ("B", "start"): (0, False, ["up"], True),
    ("E", "start"): (1, False, [], False),
    ("A", "switch"): (0, False, [], False),
    ("B", "switch"): (0, True, [], False),
    ("E", "switch"): (0, False, [], False),
}


@pytest.mark.parametrize(("situation", "command"), list(STATE_TABLE))
def test_the_state_table_cells_of_stop_start_and_apply(
    situation, command, request, scripted, repository, state
):
    """#60 proof (a) / §8: the states A, B, E crossed with the three commands."""
    put_unit_in(situation, scripted, repository, state)
    scripted.calls.clear()
    run = request.getfixturevalue(command)

    exit_code, out, _ = run()

    exit_wanted, marker_wanted, verbs_wanted, asked_wanted = STATE_TABLE[(situation, command)]
    assert exit_code == exit_wanted
    assert (marker(state) is not None) is marker_wanted
    assert scripted.changing_verbs() == verbs_wanted
    assert ("Type 'yes'" in out) is asked_wanted


def test_stop_records_the_time_in_a_private_marker_before_closing(
    stop, scripted, repository, state
):
    """#60 rule 1 and 2: one 0600 file in a 0700 directory; it exists before the first `stop`."""
    put_unit_in("A", scripted, repository, state)
    seen_at_stop = []
    closing = scripted.verb_stop

    def stop_and_look(profile, arguments):
        seen_at_stop.append(marker(state))
        return closing(profile, arguments)

    scripted.verb_stop = stop_and_look

    exit_code, out, _ = stop()

    assert exit_code == 0
    assert seen_at_stop == [NOW.isoformat(timespec="seconds")]
    assert "step-ca is switched off" in out
    assert stat.S_IMODE((state / "stopped").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "stopped" / "step-ca").stat().st_mode) == 0o600


def test_stop_stops_exactly_the_units_services_and_removes_nothing(
    stop, scripted, repository, state
):
    """#60 rule 2: never rm, down, -v or volume rm; never traefik or socket-proxy."""
    put_unit_in("A", scripted, repository, state)
    scripted.volumes = {"step-ca": "intermediate-A"}

    stop()

    assert ["--profile", "step-ca", "stop", *STEP_CA_SERVICES] in scripted.calls
    assert scripted.volumes == {"step-ca": "intermediate-A"}
    assert {"step-ca", "traefik-mcp"} <= set(scripted.containers)
    words = {word for call in scripted.calls for word in call}
    assert not words & {"down", "rm", "-v", "--volumes", "volume", "traefik", "socket-proxy"}


def test_stop_of_a_unit_without_a_host_says_there_is_no_barrier_and_writes_none(
    stop, scripted, repository, state
):
    """#61 rule 1: step-ca has no router on `edge`, so no `dynamic/switch-step-ca.yml`."""
    put_unit_in("A", scripted, repository, state)

    exit_code, out, _ = stop()

    assert exit_code == 0
    assert "no host behind Traefik, so no barrier" in out
    assert not (repository / "dynamic" / "switch-step-ca.yml").exists()


def test_a_stop_that_fails_leaves_the_switch_and_says_partial(stop, scripted, repository, state):
    """#60 rule 2 partial: the marker stays, exit 1, Compose's last line, never "switched off"."""
    put_unit_in("A", scripted, repository, state)
    scripted.failing_verb = "stop"

    exit_code, out, err = stop()

    assert exit_code == 1
    assert marker(state) is not None
    assert "partial: the switch is recorded (since " in err
    assert "run `edge stop step-ca` again" in err
    assert "last line" in err
    assert "switched off" not in out


def test_a_container_that_still_runs_after_the_stop_is_partial(stop, scripted, repository, state):
    """#60 rule 2 partial: a container still running after the stop is exit 1."""
    put_unit_in("A", scripted, repository, state)
    scripted.verb_stop = lambda *_: ""

    exit_code, out, err = stop()

    assert exit_code == 1
    assert "a container of it still runs" in err
    assert "switched off" not in out


def test_a_switch_that_cannot_be_written_still_closes_the_unit(stop, scripted, repository, state):
    """#60 rule 2: closing wins over the record; exit 1 says apply would start it again."""
    put_unit_in("A", scripted, repository, state)
    state.mkdir(parents=True)
    state.chmod(0o500)
    try:
        exit_code, _, err = stop()
    finally:
        state.chmod(0o700)

    assert exit_code == 1
    assert scripted.containers["step-ca"] == "exited"
    assert "step-ca is stopped, but the switch could not be recorded" in err
    assert "`edge apply` would start it again" in err


def test_stop_stops_a_switched_off_unit_again_when_something_of_it_runs(
    stop, scripted, repository, state
):
    """#60 rule 2 from B: someone started it by hand; the marker keeps its first time."""
    put_unit_in("B", scripted, repository, state)
    scripted.containers["traefik-mcp"] = "running"

    exit_code, _, _ = stop()

    assert exit_code == 0
    assert scripted.containers["traefik-mcp"] == "exited"
    assert marker(state) == SWITCHED_OFF_AT


def test_stop_closes_a_disabled_unit_that_runs_anyway(stop, scripted, repository, state):
    """#60 rule 2 from E: an emergency stop never refuses to close a running unit."""
    write_fleet(repository, DISABLED)
    scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "running")

    exit_code, _, _ = stop()

    assert exit_code == 0
    assert marker(state) is not None
    assert "running" not in scripted.containers.values()


def test_stop_of_an_unknown_unit_is_cannot_plan_and_lists_the_units(stop, scripted, state):
    """#60 rule 2: an unknown unit is exit 2, lists the units and changes nothing."""
    exit_code, _, err = stop("stepca")

    assert exit_code == 2
    assert "units: step-ca" in err
    assert scripted.changing_verbs() == []
    assert marker(state, "step-ca") is None


def test_start_without_a_terminal_refuses_before_anything_else(
    start, scripted, state, monkeypatch, repository, capsys
):
    """#60 rule 3: start is the operator's step; without a terminal nothing happens."""
    switch_off(state)
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))

    exit_code = emergency.start_unit(
        switches.Switches(state), "step-ca", compose.Compose(scripted.run, repository)
    )

    assert exit_code == 1
    assert "refused, stdin and stdout must be a terminal" in capsys.readouterr().err
    assert scripted.calls == []
    assert marker(state) == SWITCHED_OFF_AT


@pytest.mark.parametrize("answer", ["no", "", "Yes", "y"])
def test_start_with_any_answer_but_yes_aborts_and_keeps_the_switch(start, scripted, state, answer):
    """#60 rule 3: only the exact `yes` ends a switch."""
    switch_off(state)

    exit_code, _, err = start(answer)

    assert exit_code == 1
    assert "aborted, nothing changed" in err
    assert marker(state) == SWITCHED_OFF_AT
    assert scripted.changing_verbs() == []


@pytest.mark.parametrize("failure", ["failing_verb", "starts_nothing"])
def test_a_start_that_fails_or_is_not_observed_keeps_the_switch(start, scripted, state, failure):
    """#60 rule 3: the marker is cleared only after the unit is observed on."""
    switch_off(state)
    setattr(scripted, failure, "up" if failure == "failing_verb" else True)

    exit_code, out, err = start()

    assert exit_code == 1
    assert "partial" in err
    assert marker(state) == SWITCHED_OFF_AT
    assert "is on" not in out


def test_start_keeps_the_running_db_container_whose_image_changed(start, scripted, state):
    """#75 rule 1, cell 3: `edge start` starts the exited app and leaves the running db as is."""
    switch_off(state)
    scripted.put_containers(APP_EXITED)
    scripted.changed_definitions = {"step-ca"}
    db_id = scripted.container_ids["step-ca"]

    exit_code, out, _ = start()

    assert exit_code == 0
    assert "step-ca is on" in out
    assert scripted.containers == STEP_CA_ON
    assert scripted.container_ids["step-ca"] == db_id


def test_start_of_a_disabled_unit_is_refused_and_keeps_its_switch(start, repository, state):
    """#60 rule 3 from E: a re-enabled unit stays closed until `edge start`."""
    write_fleet(repository, DISABLED)
    switch_off(state)

    exit_code, _, err = start()

    assert exit_code == 1
    assert "step-ca is disabled in the catalog; enable it by PR and `edge apply --closed`" in err
    assert marker(state) == SWITCHED_OFF_AT


def test_apply_to_a_disabled_switched_off_unit_removes_the_containers_and_keeps_the_switch(
    switch, scripted, repository, state
):
    """#60 rule 4 B to E: the usual off; the volumes and the switch stay."""
    put_unit_in("B", scripted, repository, state)
    write_fleet(repository, DISABLED)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert scripted.containers == {}
    assert "its switch since 17.10. 22:10 stays" in out
    assert marker(state) == SWITCHED_OFF_AT


def test_a_re_enabled_unit_with_a_switch_stays_stopped_and_nothing_is_created(
    switch, scripted, repository, state
):
    """#60 rule 4 E with a marker to B: nothing is created or started."""
    write_fleet(repository, DISABLED)
    switch_off(state)
    write_fleet(repository, ENABLED)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "step-ca stays stopped (switch since 17.10. 22:10)" in out
    assert "nothing to change" in out
    assert scripted.calls[-1][2] == "ps"
    assert scripted.changing_verbs() == []


def test_a_switch_that_appears_after_the_plan_keeps_the_unit_down(switch, scripted, state):
    """#60 rule 5: apply reads the marker again after `yes`, before the `up`."""
    exit_code, out, err = switch(while_prompt_waits=lambda: switch_off(state))

    assert exit_code == 1
    assert "step-ca was switched off meanwhile (since 17.10. 22:10); not started" in err
    assert scripted.changing_verbs() == []
    assert "is on" not in out


def test_a_switch_renewed_during_a_start_is_kept(start, scripted, state):
    """#60 rule 5: start clears the marker only if it still holds the time it read."""
    switch_off(state)
    starting = scripted.verb_up

    def start_while_a_new_stop_arrives(profile, arguments):
        output = starting(profile, arguments)
        (state / "stopped" / "step-ca").write_text(LATER_SWITCH_AT)
        return output

    scripted.verb_up = start_while_a_new_stop_arrives

    exit_code, _, err = start()

    assert exit_code == 1
    assert "switched off again (since 17.10. 23:30)" in err
    assert marker(state) == LATER_SWITCH_AT


def test_a_new_stop_during_the_start_prompt_stops_the_start_before_up(start, scripted, state):
    """#60 rule 5: start reads the marker again after `yes`, immediately before its `up`."""
    switch_off(state)

    exit_code, out, err = start(
        while_prompt_waits=lambda: (state / "stopped" / "step-ca").write_text(LATER_SWITCH_AT)
    )

    assert exit_code == 1
    assert scripted.changing_verbs() == []
    assert "partial: step-ca was switched off meanwhile (since 17.10. 23:30); not started" in err
    assert "is on" not in out
    assert marker(state) == LATER_SWITCH_AT


def test_another_start_during_the_start_prompt_ends_this_start_without_up(start, scripted, state):
    """#60 rule 5: a marker that is gone means another start ended the switch."""
    switch_off(state)

    exit_code, out, err = start(while_prompt_waits=lambda: (state / "stopped" / "step-ca").unlink())

    assert exit_code == 0
    assert scripted.changing_verbs() == []
    assert "step-ca was started meanwhile" in out
    assert "is on" not in out
    assert err == ""


@pytest.mark.parametrize("stops", [1, 2])
def test_stops_of_a_switched_off_unit_during_the_start_prompt_win_over_the_start(
    start, stop, scripted, repository, state, stops
):
    """#60 rules 2 and 5: a repeated stop on B changes nothing visible, yet the start sees it."""
    put_unit_in("B", scripted, repository, state)

    def stops_arrive():
        for _ in range(stops):
            assert stop()[0] == 0

    exit_code, out, err = start(while_prompt_waits=stops_arrive)

    assert exit_code == 1
    assert scripted.changing_verbs() == []
    assert "partial: step-ca was switched off meanwhile (since 17.10. 22:10); not started" in err
    assert "is on" not in out
    assert marker(state) == SWITCHED_OFF_AT


def test_an_empty_marker_still_means_switched_off_for_apply(switch, scripted, state):
    """#60 rule 1 and 4: the file is the record, whatever it holds."""
    (state / "stopped").mkdir(parents=True)
    (state / "stopped" / "step-ca").write_text("")

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "step-ca stays stopped" in out
    assert scripted.changing_verbs() == []
    assert marker(state) is not None


def test_an_unreadable_marker_directory_still_closes_a_running_unit(
    stop, scripted, repository, state
):
    """#60 rule 2: closing wins over the record; exit 1, never a refusal to stop."""
    put_unit_in("A", scripted, repository, state)
    state.mkdir(parents=True)
    (state / "stopped").write_text("not a directory")

    exit_code, _, err = stop()

    assert exit_code == 1
    assert scripted.containers["step-ca"] == "exited"
    assert "partial" in err


def test_an_interrupt_while_waiting_for_the_lock_is_partial_exit_1(
    stop, scripted, repository, state
):
    """#60 rule 2 partial: an interrupt exits 1 with the recorded switch, not a traceback."""
    put_unit_in("A", scripted, repository, state)

    exit_code, _, err = stop(marks=InterruptedWaitForLock(state))

    assert exit_code == 1
    assert "partial: the switch is recorded" in err
    assert "interrupted while waiting to stop it" in err
    assert marker(state) is not None


def test_an_interrupt_for_an_enabled_unit_with_nothing_running_still_records_the_switch(
    stop, scripted, repository, state
):
    """#60 rule 2: the marker is recorded first, so the partial line never lies about it."""
    put_unit_in("A", scripted, repository, state)
    scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "exited")

    exit_code, _, err = stop(marks=InterruptedWaitForLock(state))

    assert exit_code == 1
    assert "partial: the switch is recorded" in err
    assert "interrupted while waiting to stop it" in err
    assert marker(state) is not None


def test_an_interrupt_while_a_switched_off_unit_waits_for_the_lock_is_partial_exit_1(
    stop, scripted, repository, state
):
    """#60 rule 2 partial from B with nothing running: Ctrl-C behind a start's `up` exits 1."""
    put_unit_in("B", scripted, repository, state)

    exit_code, _, err = stop(marks=InterruptedWaitForLock(state))

    assert exit_code == 1
    assert "partial: the switch is recorded (since 17.10. 22:10); interrupted" in err
    assert "run `edge stop step-ca` again" in err


def test_an_interrupt_after_a_start_cleared_the_marker_still_records_the_switch(
    stop, scripted, repository, state, monkeypatch
):
    """#60 rules 2 and 5: a stop behind a start's `up` records before it says "recorded"."""
    put_unit_in("B", scripted, repository, state)
    marks = switches.Switches(state)
    planned = scripted.verb_ps
    plans = []

    def start_finishes_then_interrupt(profile, arguments):
        plans.append(profile)
        if len(plans) == 1:
            return planned(profile, arguments)
        marks.clear("step-ca", marks.mark("step-ca"))
        raise KeyboardInterrupt

    monkeypatch.setattr(scripted, "verb_ps", start_finishes_then_interrupt)

    exit_code, _, err = stop()

    assert exit_code == 1
    assert "partial: the switch is recorded" in err
    assert marker(state) is not None


def test_stop_of_a_disabled_stopped_unit_with_a_marker_changes_nothing(
    stop, scripted, repository, state
):
    """#60 rule 2 from E: no marker is written, the same file stays."""
    put_unit_in("E", scripted, repository, state)
    switch_off(state)
    inode = (state / "stopped" / "step-ca").stat().st_ino

    exit_code, out, _ = stop()

    assert exit_code == 0
    assert "step-ca is disabled in the catalog and nothing of it runs" in out
    assert (state / "stopped" / "step-ca").stat().st_ino == inode


def test_a_lock_that_cannot_be_taken_does_not_blame_the_record(stop, scripted, repository, state):
    """#60 rule 2: the partial line says the switch is recorded when only the lock failed."""
    put_unit_in("A", scripted, repository, state)
    (state / "switch.lock").mkdir(parents=True)

    exit_code, _, err = stop()

    assert exit_code == 1
    assert scripted.containers["step-ca"] == "exited"
    assert "the switch is recorded" in err
    assert "could not be recorded" not in err


def test_a_stop_of_a_switched_off_unit_looks_again_under_the_lock(
    stop, scripted, repository, state
):
    """#60 rule 5: a start that brought the unit up before the lock opened is stopped again."""
    put_unit_in("B", scripted, repository, state)
    listing = scripted.verb_ps
    looked = []

    def unit_comes_up_after_the_first_look(profile, arguments):
        output = listing(profile, arguments)
        if not looked:
            scripted.containers["step-ca"] = "running"
        looked.append(output)
        return output

    scripted.verb_ps = unit_comes_up_after_the_first_look

    exit_code, _, _ = stop()

    assert exit_code == 0
    assert scripted.containers["step-ca"] == "exited"


# --- the ingress barrier of a unit with a host behind Traefik (#61 proof b) ----------------------


@pytest.fixture
def probe_unit(repository, scripted, state, ingress):
    """`probe` is A: its `hello` runs, no marker, no barrier."""
    write_fleet(repository, PROBE_ENABLED)
    scripted.containers = {"hello": "running"}


def put_probe_in_b(scripted, state, ingress) -> None:
    scripted.containers = {"hello": "exited"}
    switch_off(state, unit="probe")


def test_stop_writes_the_barrier_before_the_stop_and_reports_only_what_was_verified(
    probe_unit, stop, scripted, ingress, state
):
    """#61 rule 3: marker, barrier, verification, then `docker compose stop`."""
    events = []
    closing = scripted.verb_stop
    scripted.verb_stop = lambda profile, arguments: (
        events.append(f"stop with {ingress.asked[-1]} verified"),
        closing(profile, arguments),
    )[1]

    exit_code, out, _ = stop("probe")

    assert exit_code == 0
    assert events == ["stop with barrier verified"]
    assert ingress.asked == ["barrier"]
    assert "probe is switched off" in out
    assert ingress.barrier_file().exists()
    assert marker(state, "probe") == NOW.isoformat(timespec="seconds")


def test_a_barrier_traefik_rejects_is_partial_and_still_stops_the_unit(
    probe_unit, stop, scripted, ingress, state
):
    """#61 rule 3: exit 1, containers stopped, marker and barrier kept."""
    ingress.rejecting = True

    exit_code, out, err = stop("probe")

    assert exit_code == 1
    assert "switched off" not in out
    assert "partial" in err
    assert (
        "Traefik did not answer the barrier's 404 (X-Edge-Barrier: probe) "
        "for hello.probe.test within 15 s" in err
    )
    assert scripted.containers == {"hello": "exited"}
    assert marker(state, "probe") is not None
    assert ingress.barrier_file().exists()


def test_a_stop_while_traefik_is_down_is_partial_with_everything_kept(
    probe_unit, stop, scripted, ingress, state
):
    """#61 rule 3: Traefik down, exit 1, containers stopped, marker and barrier kept."""
    ingress.down = True

    exit_code, _, err = stop("probe")

    assert exit_code == 1
    assert "partial" in err
    assert scripted.containers == {"hello": "exited"}
    assert marker(state, "probe") is not None
    assert ingress.barrier_file().exists()


def test_stop_of_a_switched_off_unit_renders_a_lost_barrier_again_and_verifies_it(
    probe_unit, stop, scripted, ingress, state
):
    """#61 rule 3 from B: a `git clean -x` took the file; the marker still stands."""
    put_probe_in_b(scripted, state, ingress)
    scripted.calls.clear()

    exit_code, out, _ = stop("probe")

    assert exit_code == 0
    assert "already switched off" in out
    assert ingress.barrier_file().exists()
    assert ingress.asked == ["barrier"]
    assert scripted.changing_verbs() == []


def test_stop_names_the_barrier_it_repaired_and_only_then(
    probe_unit, stop, scripted, ingress, state
):
    """#67 rule 3: a stop from B says when it had to put the barrier back."""
    put_probe_in_b(scripted, state, ingress)
    stop("probe")
    _, standing, _ = stop("probe")
    ingress.barrier_file().unlink()

    _, repaired, _ = stop("probe")

    assert "its barrier was missing" not in standing
    assert "is already switched off (since" in repaired
    assert "its barrier was missing and stands again" in repaired


@pytest.mark.parametrize(
    "damaged", [b"a rejected barrier", b"\xff\xfe"], ids=["differing", "not text"]
)
def test_stop_repairs_a_barrier_file_that_differs_or_cannot_be_decoded(
    damaged, probe_unit, stop, scripted, ingress, state
):
    """#67 rule 3: a rejected barrier is repaired and named, never a traceback."""
    put_probe_in_b(scripted, state, ingress)
    stop("probe")
    ingress.barrier_file().write_bytes(damaged)

    exit_code, out, _ = stop("probe")

    assert exit_code == 0
    assert "stands again" in out
    assert ingress.barrier_file().read_bytes() != damaged


def test_stop_from_a_over_a_barrier_file_that_cannot_be_decoded_still_stops_the_unit(
    probe_unit, stop, scripted, ingress, state
):
    """#67 rule 3: a damaged barrier file does not keep the services running."""
    ingress.barrier_file().write_bytes(b"\xff\xfe")

    exit_code, _, _ = stop("probe")

    assert exit_code == 0
    assert scripted.changing_verbs() == ["stop"]


def test_a_lost_barrier_that_cannot_be_proven_makes_the_second_stop_partial(
    probe_unit, stop, scripted, ingress, state
):
    """#61 rule 3 from B: a re-rendered barrier that is not proven is partial."""
    put_probe_in_b(scripted, state, ingress)
    ingress.rejecting = True

    exit_code, out, err = stop("probe")

    assert exit_code == 1
    assert "already switched off" not in out
    assert "Traefik did not answer the barrier's 404" in err


def test_start_removes_the_barrier_after_the_up_is_observed_and_before_the_marker_clears(
    probe_unit, start, scripted, ingress, state
):
    """#61 rule 4: up, observe on, remove the barrier, verify it is gone, clear the marker."""
    put_probe_in_b(scripted, state, ingress)
    seen = []
    ingress.asked = seen
    fetching = ingress.fetch

    def look_around(address, host, seconds, clock):
        seen.append(f"marker {'kept' if marker(state, 'probe') else 'cleared'}")
        seen.append(f"hello {scripted.containers['hello']}")
        return fetching(address, host, seconds, clock)

    ingress.fetch = look_around

    exit_code, out, _ = start(unit="probe")

    assert exit_code == 0
    assert "probe is on" in out
    assert [seen[0], seen[1]] == ["marker kept", "hello running"]
    assert not ingress.barrier_file().exists()
    assert marker(state, "probe") is None


def test_a_start_that_leaves_the_barrier_answering_keeps_the_marker(
    probe_unit, start, scripted, ingress, state
):
    """#61 rule 4: exit 1, partial; a second `edge start` finishes the job."""
    put_probe_in_b(scripted, state, ingress)
    ingress.fetch = lambda *_: NOT_FOUND  # the host still answers the barrier's 404

    exit_code, out, err = start(unit="probe")

    assert exit_code == 1
    assert "probe is on" not in out
    assert "Traefik did not open hello.probe.test within 15 s" in err
    assert marker(state, "probe") is not None


def test_start_removes_a_barrier_left_behind_without_a_marker(
    probe_unit, start, scripted, ingress, state
):
    """#61 rule 4 from A: an interrupted start left the file; `yes` removes it."""
    ingress.barrier_file().write_text("leftover")

    exit_code, out, _ = start(unit="probe")

    assert exit_code == 0
    assert "remove the barrier a stop left behind" in out
    assert not ingress.barrier_file().exists()


def test_start_of_a_running_unit_without_marker_or_barrier_changes_nothing(
    probe_unit, start, scripted, ingress, state
):
    """#61 rule 4: nothing recorded and no barrier, so start changes nothing."""
    exit_code, out, _ = start(unit="probe")

    assert exit_code == 0
    assert "no change" in out
    assert ingress.asked == []


@pytest.mark.parametrize("catalog_text", [PROBE_DISABLED, PROBE_ENABLED])
def test_apply_leaves_the_barrier_in_b_to_e_and_in_e_with_a_marker_to_b(
    catalog_text, probe_unit, switch, scripted, ingress, state
):
    """#61 rule 5, narrowed by #85 S9: apply never writes or removes a barrier; it re-renders
    and proves them only after a Traefik recreate, which this core without file binds never
    plans."""
    put_probe_in_b(scripted, state, ingress)
    ingress.barrier_file().write_text("the barrier")
    write_fleet(ingress.repository, catalog_text)

    exit_code, _, _ = switch()

    assert exit_code == 0
    assert ingress.barrier_file().read_text() == "the barrier"
    assert marker(state, "probe") is not None
    assert ingress.asked == []


def standing_barrier(repository) -> Path:
    barrier_file = repository / "dynamic" / "switch-step-ca.yml"
    barrier_file.parent.mkdir(exist_ok=True)
    barrier_file.write_text("the barrier")
    return barrier_file


def test_start_from_b_of_a_unit_that_is_not_set_up_is_refused_before_the_prompt(
    start, scripted, repository, state, setup_check
):
    """#67 rule 2: the unit's `--check` is asked first; the marker stays, nothing is asked."""
    put_unit_in("B", scripted, repository, state)
    setup_check.exit_code = 1
    setup_check.line = "step-ca/init --check: no keys"
    barrier_file = standing_barrier(repository)
    scripted.calls.clear()

    exit_code, out, err = start()

    assert exit_code == 1
    assert "step-ca is not set up: run `step-ca/init`, then `edge start step-ca` again" in err
    assert "step-ca/init --check: no keys" in err
    assert "start step-ca" not in out
    assert scripted.changing_verbs() == []
    assert marker(state) == SWITCHED_OFF_AT
    assert barrier_file.exists()


def test_start_from_b_whose_setup_check_cannot_tell_exits_2(
    start, scripted, repository, state, setup_check
):
    """#67 rule 2: an unclear check is `cannot plan`, with the same effect."""
    put_unit_in("B", scripted, repository, state)
    setup_check.exit_code = 3
    barrier_file = standing_barrier(repository)

    exit_code, out, err = start()

    assert exit_code == 2
    assert "cannot tell" in err
    assert "start step-ca" not in out
    assert scripted.changing_verbs() == []
    assert marker(state) == SWITCHED_OFF_AT
    assert barrier_file.exists()


def test_start_from_b_asks_the_setup_check_of_a_set_up_unit_and_starts_it(
    start, scripted, repository, state, setup_check
):
    """#67 rule 2: a set-up unit is checked once, then started and the marker cleared."""
    put_unit_in("B", scripted, repository, state)

    exit_code, _, _ = start()

    assert exit_code == 0
    assert setup_check.commands == [[str(repository / "step-ca" / "init"), "--check"]]
    assert marker(state) is None


def test_start_from_b_of_a_unit_without_a_setup_runs_no_check(
    probe_unit, start, scripted, ingress, state, setup_check
):
    """#67 rule 2: a unit without run.setup is not checked."""
    put_probe_in_b(scripted, state, ingress)

    exit_code, _, _ = start(unit="probe")

    assert exit_code == 0
    assert setup_check.commands == []


def test_start_from_a_runs_no_setup_check(start, scripted, repository, state, setup_check):
    """#67 rule 2: a start from A has no marker to clear, so nothing is checked."""
    put_unit_in("A", scripted, repository, state)

    exit_code, _, _ = start()

    assert exit_code == 0
    assert setup_check.commands == []


RECREATE_TRAEFIK = ["--profile", "*", "up", "-d", "--no-deps", "--force-recreate", "traefik"]
PROBE_STOPPED = DISABLED + "  probe:\n    run: {enabled: true}\n"
RECREATED = "traefik is recreated and runs the checked-out traefik.yml"
UNFINISHED = {"traefik": {}}


@pytest.fixture
def core_files(scripted, repository):
    """The core as compose.yaml resolves it, with traefik's bind of `traefik.yml`, both running;
    no unit has anything to change."""
    bind_core_files(scripted.config, repository)
    scripted.put_containers({"socket-proxy": "running", "traefik": "running"})
    write_fleet(repository, DISABLED)


def applied(state: Path) -> dict | None:
    record = state / "applied.json"
    return json.loads(record.read_text()) if record.exists() else None


def record_traefik(state: Path, digest: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "applied.json").write_text(json.dumps({"traefik": {"traefik.yml": digest}}))


def probe_switched_off(scripted, repository, state) -> None:
    """`probe` is B: its marker stands and `hello` is stopped, so its barrier is active."""
    write_fleet(repository, PROBE_STOPPED)
    scripted.containers["hello"] = "exited"
    switch_off(state, unit="probe")


def edit_traefik_yml(repository: Path) -> None:
    with (repository / "traefik.yml").open("a") as file:
        file.write("# a harmless edit\n")


def unit_calls(scripted, unit: str = "step-ca") -> list[list[str]]:
    return [call for call in scripted.calls if call[:2] == ["--profile", unit]]


def test_a_first_apply_recreates_traefik_proves_the_barriers_and_records_its_file(
    core_files, switch, scripted, repository, state
):
    """#85 rules 2, 3, cell U1: no record, so recreate exactly traefik, prove, then record."""
    probe_switched_off(scripted, repository, state)
    ids = dict(scripted.container_ids)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "traefik: recreate (traefik.yml has no applied record)" in out
    assert [call for call in scripted.calls if call[2] == "up"] == [RECREATE_TRAEFIK]
    assert scripted.container_ids["traefik"] != ids["traefik"]
    assert scripted.container_ids["socket-proxy"] == ids["socket-proxy"]
    assert out.index("the barrier of probe is proven") < out.index(RECREATED)
    assert applied(state) == {"traefik": {"traefik.yml": sha256(repository / "traefik.yml")}}


def test_a_second_apply_with_the_recorded_traefik_yml_changes_nothing(core_files, switch, scripted):
    """#85 rule 2, P15, cell U2: nothing to change, no prompt, no changing compose verb."""
    switch()
    ids = dict(scripted.container_ids)
    scripted.calls.clear()

    exit_code, out, _ = switch(answer="no")

    assert exit_code == 0
    assert "nothing to change" in out
    assert "apply exactly this plan" not in out
    assert scripted.changing_verbs() == []
    assert scripted.container_ids == ids


def test_a_changed_traefik_yml_is_recorded_only_after_the_barriers_are_proven(
    core_files, switch, scripted, repository, state, ingress
):
    """#85 rules 2, 3, cell U3: `prove_active` runs before the record is written."""
    switch()
    probe_switched_off(scripted, repository, state)
    edit_traefik_yml(repository)
    records_seen = []
    ingress.on_fetch = lambda: records_seen.append(applied(state))

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "traefik: recreate (traefik.yml changed)" in out
    assert RECREATE_TRAEFIK in scripted.calls
    assert records_seen[0] == UNFINISHED
    assert applied(state) == {"traefik": {"traefik.yml": sha256(repository / "traefik.yml")}}


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ("not json", "RECORD is malformed: it is not JSON"),
        (
            '{"traefik": ["traefik.yml"]}',
            "RECORD is malformed: the traefik entry is not an object of files",
        ),
        ('{"socket-proxy": {}}', "traefik.yml has no applied record"),
        (
            '{"traefik": {"traefik.yml": "DIGEST", "old.yml": "DIGEST"}}',
            "applied.json names old.yml, which traefik does not mount",
        ),
    ],
)
def test_a_record_that_cannot_be_trusted_counts_as_changed(
    record, why, core_files, switch, scripted, repository, state
):
    """#85 rule 2, cell U4: malformed, missing entry or unknown key: recreate, and say why."""
    digest = sha256(repository / "traefik.yml")
    state.mkdir(parents=True, exist_ok=True)
    (state / "applied.json").write_text(record.replace("DIGEST", digest))

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert f"traefik: recreate ({why.replace('RECORD', str(state / 'applied.json'))})" in out
    assert RECREATE_TRAEFIK in scripted.calls
    assert applied(state)["traefik"] == {"traefik.yml": digest}


@pytest.mark.parametrize("traefik", ["exited", None])
def test_a_changed_traefik_yml_with_no_live_traefik_is_named_and_traefik_is_not_started(
    traefik, core_files, switch, scripted, state
):
    """#85 rule 4, cell U5: exited or absent: no recreate, record old, no prompt, exit 0."""
    if traefik:
        scripted.containers["traefik"] = traefik
    else:
        scripted.containers.pop("traefik")
    record_traefik(state, "0000")

    exit_code, out, _ = switch(answer="no")

    assert exit_code == 0
    assert "traefik.yml changed; traefik is not running and is not started" in out
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []
    assert scripted.containers.get("traefik") == traefik
    assert applied(state) == {"traefik": {"traefik.yml": "0000"}}


def test_a_restarting_traefik_is_recreated(core_files, switch, scripted):
    """#85 rule 3, cell U6: a restarting container is live, so it is recreated."""
    scripted.containers["traefik"] = "restarting"

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert RECREATE_TRAEFIK in scripted.calls
    assert RECREATED in out


def test_a_failed_recreate_is_partial_marks_the_record_unfinished_and_touches_no_unit(
    core_files, switch, scripted, repository, state
):
    """#85 rule 5, cells U7, U11: `could not recreate traefik`, exit 1, step-ca not started."""
    write_fleet(repository, ENABLED)
    scripted.failing_verb = "up"

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, could not recreate traefik, `docker compose up` exited 1" in err
    assert applied(state) == UNFINISHED
    assert unit_calls(scripted) == []
    assert RECREATED not in out


@pytest.mark.parametrize(
    ("step", "record_left", "recreated"),
    [
        ("mark traefik unfinished in applied.json", None, False),
        ("record traefik in applied.json", UNFINISHED, True),
    ],
)
def test_a_record_that_cannot_be_written_is_partial_names_the_step_and_touches_no_unit(
    step, record_left, recreated, core_files, switch, scripted, repository, state
):
    """#85 rule 5: a read-only state directory before the recreate leaves traefik untouched; one
    after it leaves the entry unfinished; either way exit 1 names the step and no unit moves."""
    write_fleet(repository, ENABLED)
    state.mkdir(parents=True, exist_ok=True)
    ids = dict(scripted.container_ids)
    if recreated:
        scripted.after_up = lambda: state.chmod(0o500)
    else:
        state.chmod(0o500)
    try:
        exit_code, out, err = switch()
    finally:
        state.chmod(0o700)

    assert exit_code == 1
    assert f"partial, could not {step}, cannot write {state / 'applied.json'}" in err
    assert (RECREATE_TRAEFIK in scripted.calls) is recreated
    assert (scripted.container_ids["traefik"] != ids["traefik"]) is recreated
    assert scripted.container_ids["socket-proxy"] == ids["socket-proxy"]
    assert applied(state) == record_left
    assert unit_calls(scripted) == []
    assert RECREATED not in out


def test_a_traefik_not_running_after_the_recreate_is_partial(core_files, switch, scripted, state):
    """#85 rule 5, cell U8: not observed running, so partial, exit 1, record unfinished."""
    scripted.after_up = lambda: scripted.containers.update(traefik="exited")

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, traefik is not running after the recreate" in err
    assert applied(state) == UNFINISHED
    assert RECREATED not in out


def test_a_traefik_that_dies_right_after_the_recreate_is_partial_and_the_next_run_recreates_again(
    core_files, switch, scripted, state
):
    """#85 rule 3, rule 5: a traefik.yml Traefik cannot load lets it run for a moment, then it
    restarts on every start; that is not running, so partial, exit 1, the record says unfinished
    and the next run plans the recreate again."""
    scripted.crash_loops.add("traefik")

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, traefik is not running after the recreate" in err
    assert applied(state) == UNFINISHED
    assert RECREATED not in out
    assert scripted.containers["traefik"] == "restarting"
    _, again, _ = switch("no")
    assert "traefik: recreate (the last recreate did not finish)" in again


def test_an_unproven_barrier_after_the_recreate_is_partial_and_the_next_run_recreates_again(
    core_files, switch, scripted, repository, state, ingress
):
    """#85 rule 5, S9, cell U9: partial naming the unit, that it may be reachable, and the
    emergency stop; record unfinished, the next run plans again."""
    probe_switched_off(scripted, repository, state)
    ingress.rejecting = True

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, the barrier of probe is not proven after the traefik recreate (" in err
    assert (
        "): probe may be reachable through Traefik; to close the tunnel now: edge stop --all; "
        "no unit switched" in err
    )
    assert applied(state) == UNFINISHED
    assert RECREATED not in out
    ingress.rejecting = False
    exit_code, out, _ = switch()
    assert exit_code == 0
    assert "traefik: recreate (the last recreate did not finish)" in out


@pytest.mark.parametrize("failure", ["unproven barrier", "traefik not running"])
def test_a_recreate_that_did_not_finish_is_planned_again_after_traefik_yml_is_reverted(
    failure, core_files, switch, scripted, repository, state, ingress
):
    """#85 rule 5, S9: a partial recreate leaves no entry that the reverted file could match, so
    the next run recreates again instead of saying `nothing to change`."""
    probe_switched_off(scripted, repository, state)
    assert switch()[0] == 0
    recorded = (repository / "traefik.yml").read_text()
    edit_traefik_yml(repository)
    if failure == "unproven barrier":
        ingress.rejecting = True
    else:
        scripted.after_up = lambda: scripted.containers.update(traefik="exited")
    assert switch()[0] == 1
    (repository / "traefik.yml").write_text(recorded)
    ingress.rejecting = False
    scripted.after_up = None
    scripted.containers["traefik"] = "running"

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "traefik: recreate (the last recreate did not finish)" in out
    assert RECREATED in out
    assert applied(state) == {"traefik": {"traefik.yml": sha256(repository / "traefik.yml")}}


def test_a_traefik_yml_changed_during_the_recreate_is_recreated_again_by_the_next_run(
    core_files, switch, scripted, repository, state
):
    """#85 rule 3, cell U10: the record holds the plan-time digest, not the file's later one."""
    planned = sha256(repository / "traefik.yml")
    scripted.after_up = lambda: edit_traefik_yml(repository)

    assert switch()[0] == 0
    assert applied(state) == {"traefik": {"traefik.yml": planned}}
    scripted.after_up = None
    exit_code, out, _ = switch()
    assert exit_code == 0
    assert "traefik: recreate (traefik.yml changed)" in out


def test_a_core_change_and_a_unit_switch_share_one_plan_and_one_yes_core_first(
    core_files, switch, scripted, repository
):
    """#85 rule 1, cell U11: the core lines come first and the core is changed first."""
    write_fleet(repository, ENABLED)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert out.index("traefik: recreate") < out.index("step-ca: on")
    assert out.count("apply exactly this plan") == 1
    starting_step_ca = unit_calls(scripted)[0]
    assert scripted.calls.index(RECREATE_TRAEFIK) < scripted.calls.index(starting_step_ca)
    assert "step-ca is on" in out


def test_a_missing_traefik_yml_cannot_be_planned_and_is_named(
    core_files, switch, scripted, repository
):
    """#85 rule 1, cell U12: a deleted traefik.yml never passes silently."""
    (repository / "traefik.yml").unlink()

    exit_code, _, err = switch()

    assert exit_code == 2
    assert f"cannot plan, cannot read {repository / 'traefik.yml'}" in err
    assert scripted.changing_verbs() == []


def test_an_enabled_unit_whose_barrier_stands_without_a_marker_stays_stopped(
    switch, scripted, repository, ingress
):
    """#85 rule 7, #34 finding 41, cell U13: a barrier alone switches the unit off."""
    write_fleet(repository, PROBE_ENABLED)
    scripted.put_containers(
        {
            "step-ca": "running",
            "traefik-mcp-acme-owner": "exited",
            "traefik-mcp": "running",
            "hello": "exited",
        }
    )
    ingress.barrier_file().write_text("the barrier")

    exit_code, out, _ = switch(answer="no")

    assert exit_code == 0
    assert "probe stays stopped (its barrier stands; no switch recorded)" in out
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []


@pytest.mark.parametrize(
    "arguments", [["down"], ["rm", "-v", "hello"], ["up", "-d", "--renew-anon-volumes", "hello"]]
)
def test_the_scripted_compose_fails_a_test_that_takes_volumes_or_the_project_down(
    arguments, scripted
):
    """#85 P13, cell U14: every test above runs on a fake that refuses these."""
    with pytest.raises(AssertionError):
        scripted.run(["docker", "compose", "-p", "d", "-f", "f", "--profile", "*", *arguments], 1)
