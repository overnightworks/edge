"""`edge apply --closed` against a scripted Compose and a scripted setup check: no containers."""

import io
import json
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from edge import apply, catalog, cli, compose

REPOSITORY = Path(__file__).resolve().parents[1]
PROJECT_PREFIX = 6  # docker compose --project-directory D --file F
STEP_CA_SERVICES = ("step-ca", "traefik-mcp-acme-owner", "traefik-mcp")
ENABLED = "services:\n  step-ca:\n    run: {enabled: true, setup: step-ca/init}\n"
DISABLED = "services:\n  step-ca:\n    run: {enabled: false, setup: step-ca/init}\n"


def resolved_configuration() -> dict:
    def volume(name: str) -> list[dict]:
        return [{"type": "volume", "source": name, "target": "/data"}]

    return {
        "services": {
            "socket-proxy": {"restart": "unless-stopped"},
            "traefik": {"restart": "unless-stopped"},
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
            },
        }
    }


@dataclass
class ScriptedCompose:
    """Compose over a table of service -> container state; records every call."""

    containers: dict[str, str] = field(default_factory=dict)
    failing_verb: str | None = None
    starts_nothing: bool = False
    calls: list[list[str]] = field(default_factory=list)
    config: dict = field(default_factory=resolved_configuration)

    def run(self, command: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(command[PROJECT_PREFIX:])
        _, profile, verb, *arguments = command[PROJECT_PREFIX:]
        if verb == self.failing_verb:
            return subprocess.CompletedProcess(command, 1, "", f"{verb} broke\nlast line")
        output = getattr(self, f"verb_{verb}")(profile, arguments)
        return subprocess.CompletedProcess(command, 0, output, "")

    def verb_config(self, _profile: str, _arguments: list[str]) -> str:
        return json.dumps(self.config)

    def verb_ps(self, _profile: str, _arguments: list[str]) -> str:
        return "\n".join(
            json.dumps({"Service": service, "State": state})
            for service, state in self.containers.items()
        )

    def verb_up(self, _profile: str, arguments: list[str]) -> str:
        for service in arguments[1:]:
            restarts = self.config["services"][service]["restart"] != "no"
            if not self.starts_nothing:
                self.containers[service] = "running" if restarts else "exited"
        return ""

    def verb_stop(self, _profile: str, arguments: list[str]) -> str:
        for service in arguments:
            if self.containers.get(service) == "running":
                self.containers[service] = "exited"
        return ""

    def verb_rm(self, _profile: str, arguments: list[str]) -> str:
        for service in arguments[1:]:
            self.containers.pop(service, None)
        return ""

    def changing_verbs(self) -> list[str]:
        return [call[2] for call in self.calls if call[2] not in {"config", "ps"}]


@dataclass
class Setup:
    """The scripted `<setup> --check`: its exit code and line."""

    exit_code: int = 0
    line: str = "step-ca/init --check: set up"
    commands: list[list[str]] = field(default_factory=list)

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.exit_code, self.line, "")


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    (tmp_path / "step-ca").mkdir()
    setup = tmp_path / "step-ca" / "init"
    setup.write_text("#!/bin/sh\n")
    setup.chmod(setup.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "fleet.yaml").write_text(ENABLED)
    return tmp_path


@pytest.fixture
def scripted() -> ScriptedCompose:
    return ScriptedCompose()


@pytest.fixture
def setup_check() -> Setup:
    return Setup()


@pytest.fixture
def at_terminal(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Stdin and stdout are a terminal; the operator will type `answer`."""

    def operator_types(answer: str) -> None:
        monkeypatch.setattr(sys, "stdin", Terminal(f"{answer}\n"))
        monkeypatch.setattr(sys, "stdout", Terminal())

    return operator_types


@pytest.fixture
def switch(repository, scripted, setup_check, at_terminal, capsys):
    """Run `edge apply --closed`; the operator types `yes` unless told otherwise."""

    def run(answer: str = "yes") -> tuple[int, str, str]:
        at_terminal(answer)
        exit_code = apply.apply_closed(
            compose.Compose(scripted.run, repository), repository, setup_check
        )
        return exit_code, sys.stdout.getvalue(), capsys.readouterr().err

    return run


def write_fleet(repository: Path, text: str) -> None:
    (repository / "fleet.yaml").write_text(text)


def test_switching_on_starts_exactly_the_profiles_services(switch, scripted):
    exit_code, out, _ = switch()

    assert exit_code == 0
    assert scripted.containers == {
        "step-ca": "running",
        "traefik-mcp-acme-owner": "exited",
        "traefik-mcp": "running",
    }
    assert ["--profile", "step-ca", "up", "-d", *STEP_CA_SERVICES] in scripted.calls
    assert "step-ca is on" in out


def test_switching_on_asks_the_setup_check_first(switch, setup_check, repository):
    switch()

    assert setup_check.commands == [[str(repository / "step-ca/init"), "--check"]]


def test_switching_off_removes_the_containers_and_names_the_volumes_that_stay(
    switch, scripted, repository
):
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
    switch()
    scripted.calls.clear()

    exit_code, out, _ = switch(answer="no")

    assert exit_code == 0
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []
    assert setup_check.commands[1:] == []


def test_switching_off_what_is_not_there_changes_nothing(switch, scripted, repository):
    write_fleet(repository, DISABLED)

    exit_code, out, _ = switch()

    assert exit_code == 0
    assert "nothing to change" in out
    assert scripted.changing_verbs() == []


def test_a_one_shot_container_left_behind_is_removed_when_switched_off(
    switch, scripted, repository
):
    write_fleet(repository, DISABLED)
    scripted.containers = {"traefik-mcp-acme-owner": "exited"}

    assert switch()[0] == 0
    assert scripted.containers == {}


def test_off_never_takes_a_volume_or_a_project_down_and_never_names_the_core(
    switch, scripted, repository
):
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
    write_fleet(repository, DISABLED)
    scripted.containers = dict.fromkeys(STEP_CA_SERVICES, "running")
    scripted.failing_verb = "rm"

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial" in err and "step-ca" in err
    assert "last line" in err
    assert "is off" not in out


def test_a_start_that_leaves_the_unit_down_is_partial(switch, scripted):
    scripted.starts_nothing = True

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "partial, step-ca is not on after the change" in err
    assert "is on" not in out


def test_an_interrupt_names_the_unit_and_says_its_state_may_be_partial(switch, scripted, capsys):
    def interrupted_start(*_arguments):
        raise KeyboardInterrupt

    scripted.verb_up = interrupted_start

    exit_code, _, err = switch()

    assert exit_code == 1
    assert "interrupted while changing step-ca" in err and "may be partial" in err


def test_a_missing_setup_refuses_before_yes_and_changes_nothing(switch, scripted, setup_check):
    setup_check.exit_code, setup_check.line = 1, "FAILED at password: password is missing"

    exit_code, out, err = switch()

    assert exit_code == 1
    assert "step-ca is not set up: run `step-ca/init`, then `edge apply --closed` again" in err
    assert "password is missing" in err
    assert "Type" not in out
    assert scripted.changing_verbs() == []


def test_a_setup_check_that_cannot_tell_is_cannot_plan_and_quotes_its_line(
    switch, scripted, setup_check
):
    setup_check.exit_code, setup_check.line = 2, "Cannot connect to the Docker daemon"

    exit_code, _, err = switch()

    assert exit_code == 2
    assert "Cannot connect to the Docker daemon" in err
    assert scripted.changing_verbs() == []


def test_the_setup_command_is_only_ever_run_with_check(switch, setup_check):
    switch()

    assert all(command[-1] == "--check" for command in setup_check.commands)


def test_without_a_terminal_it_refuses_before_anything_else(
    repository, scripted, setup_check, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))

    exit_code = apply.apply_closed(
        compose.Compose(scripted.run, repository), repository, setup_check
    )

    assert exit_code == 1
    assert "refused, stdin and stdout must be a terminal" in capsys.readouterr().err
    assert scripted.calls == [] and setup_check.commands == []


@pytest.mark.parametrize("answer", ["no", "", "Yes", "y", "yes please"])
def test_only_the_exact_yes_applies(switch, scripted, answer):
    exit_code, _, err = switch(answer)

    assert exit_code == 1
    assert "aborted, nothing changed" in err
    assert scripted.changing_verbs() == []


def test_plain_apply_says_the_publish_phase_is_not_built_and_names_closed(capsys):
    exit_code = cli.main(["apply"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "publish phase is not built yet" in err and "edge apply --closed" in err


@pytest.mark.parametrize(
    ("fleet", "named"),
    [
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
    write_fleet(repository, fleet)

    exit_code, _, err = switch()

    assert exit_code == 2
    assert named in err
    assert scripted.changing_verbs() == []


def test_the_line_for_an_unknown_profile_lists_the_profiles_that_exist(switch, repository):
    write_fleet(repository, "services:\n  stepca:\n    run: {enabled: true}\n")

    _, _, err = switch()

    assert "profiles: keycloak, step-ca" in err


def test_an_entry_without_run_is_ignored(switch, scripted, repository):
    write_fleet(repository, "services:\n  songmaker:\n    repo: overnightworks/songmaker\n")

    exit_code, out, _ = switch()

    assert exit_code == 0 and "nothing to change" in out


def test_compose_that_cannot_be_read_is_cannot_plan(switch, scripted):
    scripted.failing_verb = "config"

    exit_code, _, err = switch()

    assert exit_code == 2
    assert "cannot plan" in err and "last line" in err


# --- catalog and compose cannot drift (catalog -> compose) --------------------------------------


def repository_profiles() -> dict[str, tuple[str, ...]]:
    return compose.profile_services(yaml.safe_load((REPOSITORY / "compose.yaml").read_text()))


def test_every_unit_of_the_repositorys_catalog_names_a_compose_profile_with_services():
    document = yaml.safe_load((REPOSITORY / "fleet.yaml").read_text())

    units = catalog.units(document, repository_profiles(), REPOSITORY)

    assert {unit.key for unit in units} >= {"step-ca"}


def test_a_catalog_key_without_a_compose_profile_fails_the_drift_check():
    document = {"services": {"keycloack": {"run": {"enabled": False}}}}

    with pytest.raises(catalog.CatalogError, match=r"services\.keycloack"):
        catalog.units(document, repository_profiles(), REPOSITORY)
