"""`hal migrate stack` (#789 rows MV1 to MV8): the plan, the one yes and the Docker calls, against a
fake Docker that answers like the CLI and records what it was asked."""

import json
import re
import subprocess
from dataclasses import dataclass, field

import pytest
from support.terminal_support import type_at_prompt

from hal import cli, migrate_commands
from hal.grammar import ExitCode
from hal.migrate_commands import World
from hal.output.command import Verb, command

CORE = ("socket-proxy", "traefik")
NAMED_VOLUMES = ("keycloak-data", "backup")  # fixed names `compose.yaml` gives its volumes
STALE_VOLUMES = ("socket-proxy-run", "traefik-mcp-acme")
OTHER_PROJECT_VOLUME = "hal-extra_socket-proxy-run"
FIXED_NETWORKS = ("hal_app-one", "hal_app-two")  # networks `compose.yaml` gives a fixed name
TEN_CONTAINERS = tuple(f"service-{number}" for number in range(10))


@dataclass
class FakeDocker:
    """The containers, networks and volumes of Compose projects. `named` is the project
    `compose.yaml` names; `fail` holds the call to refuse: `down`, `up`, `rm <volume>`, or
    `down networks`, a `down` that removes the containers and fails before the networks."""

    named: str = "hal"
    containers: dict[str, list[str]] = field(default_factory=dict)
    networks: dict[str, list[str]] = field(default_factory=dict)
    volumes: dict[str, str] = field(default_factory=dict)  # name -> project label
    resolved_volumes: tuple[str, ...] = ()
    resolved_networks: tuple[str, ...] = ()
    core_up: set[str] = field(default_factory=set)
    fail: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv, timeout, stdin=None):
        self.calls.append(argv)
        key = self._key(argv)
        if key in self.fail:
            return self._refused(argv, key)
        answer = self._answer(argv)
        if key == "down" and "down networks" in self.fail:
            return self._refused(argv, "down networks")
        return subprocess.CompletedProcess(argv, 0, answer, "")

    @staticmethod
    def _refused(argv, key):
        return subprocess.CompletedProcess(argv, 1, "", f"{key} refused by the daemon\n")

    @staticmethod
    def _options(argv):
        """The compose options before the verb: the project given with `-p`, then the rest."""
        rest, project = argv[2:], None
        while rest[0].startswith("-"):
            project = rest[1] if rest[0] == "-p" else project
            rest = rest[2:]
        return project, rest

    def _key(self, argv):
        if argv[1] == "compose":
            return self._options(argv)[1][0]
        return f"rm {argv[3]}" if argv[1:3] == ["volume", "rm"] else "read"

    def _answer(self, argv):
        if argv[1] == "compose":
            return self._compose(*self._options(argv))
        if argv[1:3] == ["volume", "rm"]:
            del self.volumes[argv[3]]
            return ""
        label = argv[argv.index("--filter") + 1].split("=")[-1]
        if argv[1] == "ps":
            return "\n".join(self.containers.get(label, []))
        if argv[1] == "network":
            return "\n".join(self.networks.get(label, []))
        return "\n".join(name for name, owner in self.volumes.items() if owner == label)

    def _compose(self, project, rest):
        project = project or self.named
        match rest[0]:
            case "config":
                services = {name: {} for name in CORE} | {"keycloak": {"profiles": ["auth"]}}
                volumes = {key: {"name": key} for key in self.resolved_volumes}
                networks = {key: {"name": key} for key in self.resolved_networks}
                resolved = {"services": services, "volumes": volumes, "networks": networks}
                return json.dumps({"name": self.named, **resolved})
            case "ps":
                running = [{"Service": name, "State": "running"} for name in CORE]
                return json.dumps(running if project in self.core_up else [])
            case "down":
                self.containers[project] = []
                if "down networks" not in self.fail:
                    self.networks[project] = []
            case "up":
                self.core_up.add(project)
        return ""

    def changes(self) -> list[str]:
        """Every call that changes Docker, as `down`, `rm <volume>` or `up`."""
        changing = []
        for argv in self.calls:
            if argv[1:3] == ["volume", "rm"]:
                changing.append(f"rm {argv[3]}")
            elif argv[1] == "compose" and (verb := self._options(argv)[1][0]) in ("down", "up"):
                changing.append(verb)
        return changing

    def call_of(self, verb: str) -> list[str]:
        return next(argv for argv in self.calls if argv[1] == "compose" and verb in argv)


def stack_of(source: str, named: str) -> FakeDocker:
    """Ten containers of `source`, its four networks and four volumes of which `compose.yaml`
    names two each, and a volume of a project whose name is alike."""
    volumes = {f"{source}_{name}": source for name in STALE_VOLUMES}
    volumes |= {name: source for name in NAMED_VOLUMES}
    volumes[OTHER_PROJECT_VOLUME] = "hal-extra"
    return FakeDocker(
        named=named,
        containers={source: [f"{source}-{name}" for name in TEN_CONTAINERS]},
        networks={source: [f"{source}_default", f"{source}_internal", *FIXED_NETWORKS]},
        volumes=volumes,
        resolved_volumes=NAMED_VOLUMES,
        resolved_networks=(f"{named}_default", *FIXED_NETWORKS),
    )


@pytest.fixture
def docker() -> FakeDocker:
    return stack_of("edge", named="hal")


@pytest.fixture
def through_cli(docker, capsys, monkeypatch):
    """`hal migrate stack` through `hal.cli.main` with a person who types `yes` unless told
    otherwise: the exit code and both streams."""
    terminal = []
    monkeypatch.setattr(migrate_commands, "real_world", lambda: World(docker, lambda: terminal[0]))

    def run(*arguments: str, answer: str | None = "yes", at_terminal: bool = True):
        terminal[:] = [at_terminal]
        type_at_prompt(monkeypatch, answer)
        code = cli.main(["migrate", "stack", *arguments])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def rows_of(text: str) -> dict[tuple[str, str], set[str]]:
    """The rows in `text`, (subject, event) -> the names below it; other lines are skipped."""
    rows: dict[tuple[str, str], set[str]] = {}
    names: set[str] = set()
    for line in text.splitlines():
        columns = re.split(r"\s{2,}", line.strip())
        if line.startswith("    "):
            names.add(line.strip())
        elif len(columns) > 1:
            names = rows.setdefault((columns[0].split()[1], columns[1]), set())
    return rows


def plan_rows(out: str) -> dict[tuple[str, str], set[str]]:
    """The rows of the plan shown before `Type yes`."""
    return rows_of(out.partition("Type yes")[0])


def done_rows(out: str) -> dict[tuple[str, str], set[str]]:
    """The rows written after the answer to `Type yes ...:`."""
    return rows_of(out.partition("Type yes")[2].partition(":")[2])


def removed_volumes(docker) -> set[str]:
    return {change.removeprefix("rm ") for change in docker.changes() if change.startswith("rm ")}


def test_the_plan_names_the_stack_and_one_yes_moves_it(through_cli, docker):
    code, out, err = through_cli()

    plan = out.partition("Type yes")[0]
    assert (code, err) == (ExitCode.OK, "")
    assert all(f"edge-{name}" in plan for name in TEN_CONTAINERS)
    assert all(name in plan for name in (*NAMED_VOLUMES, *CORE))
    assert docker.changes() == [
        "down",
        "rm edge_socket-proxy-run",
        "rm edge_traefik-mcp-acme",
        "up",
    ]
    assert out.splitlines()[-1] == f"next: {command(Verb.APPLY)}"
    assert done_rows(out)[("containers", "removed")] == {f"edge-{n}" for n in TEN_CONTAINERS}


def test_the_plan_removes_what_compose_yaml_does_not_name_and_keeps_what_it_names(through_cli):
    _, out, _ = through_cli()

    rows = plan_rows(out)
    assert rows[("volumes", "remove")] == {f"edge_{name}" for name in STALE_VOLUMES}
    assert rows[("volumes", "keep")] == set(NAMED_VOLUMES)
    assert rows[("networks", "remove")] == {"edge_default", "edge_internal"}
    assert rows[("networks", "take over")] == set(FIXED_NETWORKS)
    assert OTHER_PROJECT_VOLUME not in out.partition("Type yes")[0]


def test_the_core_of_the_named_project_is_started_without_recreating(through_cli, docker):
    through_cli()

    started = docker.call_of("up")
    assert started[-len(CORE) :] == list(CORE)
    assert "--no-recreate" in started
    assert "-p" not in started


def test_dry_run_shows_the_plan_and_changes_nothing(through_cli, docker):
    code, out, _ = through_cli("--dry-run", answer=None)

    assert code == ExitCode.OK
    assert "edge-service-0" in out
    assert docker.changes() == []


@pytest.mark.parametrize("label", ["edge", "hal"])
def test_a_volume_compose_yaml_names_is_not_removed_whatever_its_label(through_cli, docker, label):
    docker.volumes[NAMED_VOLUMES[0]] = label

    through_cli()

    assert NAMED_VOLUMES[0] in docker.volumes


def test_a_volume_of_another_project_with_a_similar_name_is_not_removed(through_cli, docker):
    through_cli()

    assert OTHER_PROJECT_VOLUME in docker.volumes


@pytest.mark.parametrize(
    ("answer", "at_terminal", "expected"),
    [
        (None, False, ExitCode.REFUSED),
        ("Yes", True, ExitCode.FINDINGS),
        ("y", True, ExitCode.FINDINGS),
        (None, True, ExitCode.FINDINGS),
    ],
    ids=["no-terminal", "wrong-case", "short", "closed-stdin"],
)
def test_without_a_terminal_or_the_exact_yes_nothing_changes(
    through_cli, docker, answer, at_terminal, expected
):
    code, _, err = through_cli(answer=answer, at_terminal=at_terminal)

    assert code == expected
    assert "nothing changed" in err
    assert docker.changes() == []


def test_a_compose_yaml_naming_neither_project_is_refused(through_cli, docker):
    docker.named = "other"

    code, out, err = through_cli()

    assert (code, out, docker.changes()) == (ExitCode.REFUSED, "", [])
    assert "nothing changed" in err


@pytest.mark.parametrize("networks", [(), FIXED_NETWORKS], ids=["none", "fixed-names"])
def test_nothing_of_the_source_and_a_running_core_is_nothing_to_move(through_cli, docker, networks):
    docker.named, docker.containers, docker.volumes = "edge", {}, {}
    docker.networks, docker.resolved_networks = {"hal": list(networks)}, FIXED_NETWORKS
    docker.core_up = {"edge"}

    code, out, err = through_cli()

    assert (code, err, docker.changes()) == (ExitCode.OK, "", [])
    assert "nothing to move" in out


@pytest.mark.parametrize("failing", ["down", "rm edge_socket-proxy-run", "up"])
def test_a_failing_step_stops_before_the_next_and_names_the_cause(through_cli, docker, failing):
    docker.fail = {failing}

    code, _, err = through_cli()

    assert code == ExitCode.FAILURE
    assert f"{failing} refused by the daemon" in err
    assert docker.changes()[-1] == failing


@pytest.mark.parametrize(
    ("failing", "left"),
    [
        ("down", ["down", "rm edge_socket-proxy-run", "rm edge_traefik-mcp-acme", "up"]),
        (
            "down networks",
            ["down", "rm edge_socket-proxy-run", "rm edge_traefik-mcp-acme", "up"],
        ),
        ("rm edge_traefik-mcp-acme", ["rm edge_traefik-mcp-acme", "up"]),
        ("up", ["up"]),
    ],
)
def test_a_rerun_after_a_partial_run_finishes_what_is_left_and_then_has_nothing_to_move(
    through_cli, docker, failing, left
):
    docker.fail = {failing}
    through_cli()
    docker.fail = set()
    docker.calls.clear()

    code, _, _ = through_cli()
    finishing = docker.changes()
    docker.calls.clear()
    again = through_cli()

    assert (code, finishing) == (ExitCode.OK, left)
    assert (again[0], docker.changes()) == (ExitCode.OK, [])
    assert "nothing to move" in again[1]


def test_a_rerun_with_only_networks_left_names_them_in_the_done_row(through_cli, docker):
    docker.fail = {"down networks"}
    through_cli()
    docker.fail = set()

    _, out, _ = through_cli()

    assert done_rows(out)[("networks", "removed")] == {"edge_default", "edge_internal"}


def test_the_route_back_takes_hal_down_and_starts_the_core_of_edge(through_cli, docker):
    docker.__dict__.update(stack_of("hal", named="edge").__dict__)  # the other direction

    code, _, _ = through_cli()

    assert code == ExitCode.OK
    assert removed_volumes(docker) == {f"hal_{name}" for name in STALE_VOLUMES}
    assert set(NAMED_VOLUMES) <= set(docker.volumes)
    assert docker.call_of("down")[3] == "hal"
    assert docker.core_up == {"edge"}


def test_down_names_every_profile_the_source_and_orphans_and_never_removes_volumes(
    through_cli, docker
):
    through_cli()

    argv = docker.call_of("down")
    assert argv[argv.index("-p") + 1] == "edge"
    assert argv[argv.index("--profile") + 1] == "*"
    assert "--remove-orphans" in argv[argv.index("down") :]
    assert {"--volumes", "-v"}.isdisjoint(argv)
