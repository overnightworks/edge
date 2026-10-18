"""keycloak/go-live against a scripted compose project and Keycloak: no containers, no sleeping."""

import dataclasses
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs

import pytest

from edge import switches

SCRIPT = Path(__file__).resolve().parents[1] / "keycloak" / "go-live"
SECRET_FILES = ("db-password", "config-cli-secret", "bootstrap-admin-password")
OVERLAY = "keycloak/bootstrap.compose.yaml"
OTP_ADMIN = "alice"


@pytest.fixture(scope="module")
def golive() -> Iterator[ModuleType]:
    loader = importlib.machinery.SourceFileLoader("keycloak_go_live", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through it
    loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


# Not "edge_keycloak-db": compose, not the script, decides the project name.
PROJECT_VOLUME = "operators-project_keycloak-db"


@dataclass
class Keycloak:
    """What compose and Keycloak have done so far, answering the way the real ones do."""

    running: bool = False
    database_volume: bool = False  # created by the first start, kept by `down`
    with_overlay: bool = False
    master_moved: bool = False
    master_applied: bool = False
    realm_exists: bool = False  # created first by the apply
    import_completed: bool = False  # the checksum the apply records last
    edge_up: bool = False
    fail_apply_after_master: bool = False
    fail_edge_start: bool = False
    never_answers: bool = False
    compose_without_database_volume: bool = False
    edge_restarting: bool = False  # step 5's recreate: kcadm is refused, 8090 does not answer yet
    docker_daemon_down: bool = False
    compose_config_fails: bool = False
    client_secret_files: list[str] = field(default_factory=list)  # compose's secrets of clients
    compose_declares_environment_secret: bool = False
    echo_secret_file: Path | None = None  # the apply's error text quotes this file's value
    users: dict[str, dict] = field(default_factory=lambda: {"temp-admin": {"otp": True}})
    commands: list[list[str]] = field(default_factory=list)
    stdin_seen: str | None = None
    stdin_of_this_command: str | None = None
    deleted: list[str] = field(default_factory=list)

    def add_admin(self, username: str, *, otp: bool, enabled: bool = True) -> None:
        self.users[username] = {"otp": otp, "enabled": enabled}

    def changed_something(self) -> bool:
        return any(
            {"up", "run", "--force-recreate"} & set(command)
            or "/opt/edge/admin-frontend.sh" in command
            for command in self.commands
        )

    # --- docker ---------------------------------------------------------------------------

    def down(self) -> None:
        """`docker compose down`: the containers are gone, the database volume stays."""
        self.running = self.edge_up = False

    def run(
        self, command: list[str], _timeout: int, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.stdin_of_this_command = stdin
        if stdin is not None:
            self.stdin_seen = stdin
        self.commands.append(command)
        if command[:3] == ["docker", "volume", "inspect"]:
            if self.docker_daemon_down:
                return self.done(returncode=1, stderr="Cannot connect to the Docker daemon")
            if self.database_volume and command[3] == PROJECT_VOLUME:
                return self.done()
            return self.done(returncode=1, stderr=f"Error: No such volume: {command[3]}")
        if command[:2] == ["docker", "inspect"]:
            env = ["KC_DB=postgres"] + (
                ["KC_BOOTSTRAP_ADMIN_USERNAME=temp-admin"] * self.with_overlay
            )
            return self.done(json.dumps(env))
        verb = next(arg for arg in command if arg in {"up", "exec", "run", "ps", "config"})
        return getattr(self, f"compose_{verb}")(command)

    @staticmethod
    def done(
        stdout: str = "", returncode: int = 0, stderr: str = ""
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def compose_config(self, command: list[str]):
        if self.compose_config_fails:
            return self.done(returncode=1, stderr="validating compose.yaml: invalid")
        if self.compose_without_database_volume:
            return self.done("{}")
        secrets = {
            f"secret_{at}": {"file": path} for at, path in enumerate(self.client_secret_files)
        }
        if self.compose_declares_environment_secret:
            secrets["from_environment"] = {"environment": "SOME_VARIABLE"}
        volumes = {"keycloak-db": {"name": PROJECT_VOLUME}}
        return self.done(json.dumps({"volumes": volumes, "secrets": secrets}))

    def compose_ps(self, command: list[str]):
        return self.done("abc123\n" if self.running else "")

    def compose_up(self, command: list[str]):
        if "--force-recreate" in command:
            if self.fail_edge_start:
                return self.done(returncode=1, stderr="traefik: port 8081 is in use")
            self.edge_up = True
        elif "keycloak" in command[command.index("up") + 1 :]:
            if self.never_answers:
                return self.done()
            self.running = self.database_volume = True
            self.with_overlay = OVERLAY in command
        else:
            self.with_overlay = OVERLAY in command
        return self.done()

    def compose_run(self, command: list[str]):
        if self.master_applied and OVERLAY in command:
            return self.done(returncode=1, stderr="401 Unauthorized: TOTP required")
        if not self.master_applied and OVERLAY not in command:
            return self.done(returncode=1, stderr="401 Unauthorized: no client config-cli")
        self.master_applied = True
        if self.fail_apply_after_master:
            quoted = self.echo_secret_file.read_text() if self.echo_secret_file else ""
            return self.done(returncode=1, stderr=f"realm.yaml: import failed {quoted}".strip())
        self.realm_exists = self.import_completed = True
        return self.done()

    def compose_exec(self, command: list[str]):
        if "/opt/edge/admin-frontend.sh" in command:
            assert OVERLAY in command
            self.master_moved = True
            return self.done()
        if not self.running:
            return self.done(returncode=1, stderr="service keycloak is not running")
        if any("kcadm.sh" in argument for argument in command):
            if (
                self.edge_up or self.edge_restarting
            ):  # the real kcadm is refused with 401 once the edge stands
                return self.done(returncode=1, stderr="HTTP 401 Unauthorized")
            return self.kcadm_realm_attributes(command)
        path = command[-1]
        status, body = self.container_answer(path)
        return self.done(f"HTTP/1.0 {status} X\nContent-Type: x\n\n{body}")

    def kcadm_realm_attributes(self, command: list[str]):
        assert command[-1] == "hallucinai"
        if not self.script_reads_secret(command, self.stdin_of_this_command):
            return self.done(returncode=1)
        if not self.realm_exists:
            return self.done(returncode=1, stderr="Resource not found for url")
        attributes = {"de.adorsys.keycloak.config.import-checksum-default": "x"}
        return self.done(json.dumps({"attributes": attributes if self.import_completed else {}}))

    @staticmethod
    def script_reads_secret(command: list[str], stdin: str | None) -> bool:
        """Runs the real shell script of the command with kcadm swapped for a no-op: whether it
        gets as far as kcadm depends on how the secret arrives on standard input."""
        script = command[command.index("-c") + 1].replace(
            "exec /opt/keycloak/bin/kcadm.sh", "true #"
        )
        shell = subprocess.run(
            ["bash", "-c", script, "bash", command[-1]], input=stdin, text=True, check=False
        )
        return shell.returncode == 0

    def container_answer(self, path: str) -> tuple[int, str]:
        if path == "/realms/master" or (path == "/realms/hallucinai" and self.realm_exists):
            return 200, "{}"
        if path.endswith("/.well-known/openid-configuration"):
            host = "http://localhost:8090" if self.master_moved else "https://auth.hallucinai.de"
            return 200, json.dumps({"issuer": f"{host}/realms/master"})
        return 404, ""

    # --- HTTP -----------------------------------------------------------------------------

    def fetch(self, method: str, url: str, *, headers=None, form=None):
        from_script = sys.modules["keycloak_go_live"].Reply
        if not self.edge_up:
            raise ConnectionRefusedError
        if url == "http://127.0.0.1:8081/":
            return from_script(404, b"")
        if url == "http://localhost:8090/realms/master":
            return from_script(200, b"{}")
        if url.endswith("/protocol/openid-connect/token"):
            assert parse_qs(form_text(form))["client_id"] == ["config-cli"]
            if not self.master_applied:  # master.yaml creates the client during the first apply
                return from_script(401, b"")
            return from_script(200, json.dumps({"access_token": "token"}).encode())
        if url == "http://localhost:8090/admin/realms/hallucinai":
            return self.realm_representation(from_script)
        return self.admin_api(method, url.split("/admin/realms/master")[1], from_script)

    def realm_representation(self, reply):
        if not self.realm_exists:
            return reply(404, b"")
        checksum = {"de.adorsys.keycloak.config.import-checksum-default": "x"}
        return reply(
            200, json.dumps({"attributes": checksum if self.import_completed else {}}).encode()
        )

    def admin_api(self, method: str, path: str, reply):
        if method == "GET" and path.startswith("/roles/admin/users"):
            holders = [
                {"id": name, "username": name, "enabled": person.get("enabled", True)}
                for name, person in self.users.items()
            ]
            service = {"id": "sa", "username": "service-account-config-cli"}
            return reply(
                200, json.dumps([*holders, {**service, "serviceAccountClientId": "x"}]).encode()
            )
        if method == "GET" and path.endswith("/credentials"):
            user = self.users[path.split("/")[2]]
            return reply(200, json.dumps([{"type": "otp" if user["otp"] else "password"}]).encode())
        if method == "GET" and path.startswith("/users?username=temp-admin"):
            found = [{"id": "temp-admin"}] if "temp-admin" in self.users else []
            return reply(200, json.dumps(found).encode())
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            self.users.pop(path.rsplit("/", 1)[1])
            return reply(204, b"")
        raise AssertionError(f"unscripted {method} {path}")


def form_text(form: dict[str, str]) -> str:
    return "&".join(f"{key}={value}" for key, value in form.items())


@dataclass
class Session:
    """One operator at the terminal: answers, and everything printed."""

    answers: list[str]
    printed: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    on_enter: object = None
    clock: float = 0.0

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if prompt.startswith("Press Enter") and self.on_enter:
            self.on_enter()
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def monotonic(self) -> float:
        return self.clock

    @property
    def output(self) -> str:
        return "\n".join(self.printed)


@pytest.fixture
def keycloak() -> Keycloak:
    return Keycloak()


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    return tmp_path / ".secrets" / "keycloak"


@pytest.fixture
def session(keycloak: Keycloak) -> Session:
    """Says yes to the backup question, creates the second admin on Enter."""
    operator = Session(answers=["yes", ""])
    operator.on_enter = lambda: keycloak.add_admin(OTP_ADMIN, otp=True)
    return operator


@pytest.fixture
def marks(tmp_path: Path) -> switches.Switches:
    return switches.Switches(tmp_path / "state" / "edge")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operator's HOME, empty: edge-backup is not set up in it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def set_up_backup(home: Path, password_mode: int = 0o600) -> None:
    password = home / ".secrets/restic/password"
    password.parent.mkdir(parents=True)
    password.write_text("planted")
    password.chmod(password_mode)
    repository = home / "backups/restic"
    repository.mkdir(parents=True)
    (repository / "config").write_text("planted")


@pytest.fixture
def go_live(
    golive: ModuleType, keycloak: Keycloak, session: Session, secrets_dir: Path, marks, home: Path
):
    tools = golive.Tools(
        run=keycloak.run,
        fetch=keycloak.fetch,
        ask=session.ask,
        say=session.printed.append,
        sleep=session.sleep,
        monotonic=session.monotonic,
    )
    return golive.GoLive(tools, secrets_dir, marks)


def name_clients(keycloak: Keycloak, secrets_dir: Path, *names: str) -> None:
    keycloak.client_secret_files = [f"{secrets_dir}/clients/{name}" for name in names]


def write_secrets(secrets_dir: Path, *names: str) -> None:
    secrets_dir.mkdir(parents=True, mode=0o700)
    for name in names:
        (secrets_dir / name).write_text(f"value-of-{name}")


def secret_values(secrets_dir: Path) -> list[str]:
    paths = [secrets_dir / name for name in SECRET_FILES]
    paths += sorted((secrets_dir / "clients").glob("*"))
    return [path.read_text() for path in paths if path.exists()]


def test_a_fresh_run_goes_live_and_retires_the_bootstrap(go_live, keycloak, session, secrets_dir):
    """#19 rules 1, 2, 4: one run creates the secrets, applies, and retires the bootstrap."""
    assert go_live.run() == 0

    assert keycloak.import_completed
    assert keycloak.deleted == ["temp-admin"]
    assert sorted(path.name for path in secrets_dir.iterdir()) == [
        "config-cli-secret",
        "db-password",
    ]
    assert "curl -s https://auth.hallucinai.de/realms/hallucinai/.well-known" in session.output
    assert len(session.asked) == 2


def test_every_compose_call_names_the_keycloak_profile(go_live, keycloak):
    """#54 rule 2: the services live behind the profile, so no call may leave it out."""
    go_live.run()

    compose_commands = [
        command for command in keycloak.commands if command[:2] == ["docker", "compose"]
    ]
    assert compose_commands
    for command in compose_commands:
        profiles = {command[at + 1] for at, word in enumerate(command) if word == "--profile"}
        assert "keycloak" in profiles


def test_the_secret_files_are_random_hex_in_a_private_directory(go_live, secrets_dir, keycloak):
    """#19 rule 2: random hex files, 0644, in a 0700 directory."""
    keycloak.fail_apply_after_master = True  # stops with the bootstrap password still on disk

    assert go_live.run() == 1

    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    values = secret_values(secrets_dir)
    assert len(values) == 3
    assert len(set(values)) == 3
    for name in SECRET_FILES:
        assert stat.S_IMODE((secrets_dir / name).stat().st_mode) == 0o644
        assert re.fullmatch(r"[0-9a-f]{64}", (secrets_dir / name).read_text())


def test_a_secret_quoted_by_a_failing_command_is_printed_as_stars(
    go_live, session, secrets_dir, keycloak
):
    """#19 rule 2: secrets are never printed, not even inside a failing command's own error."""
    keycloak.fail_apply_after_master = True
    keycloak.echo_secret_file = secrets_dir / "config-cli-secret"

    go_live.run()

    assert session.printed[-1].endswith("realm.yaml: import failed ***")
    assert [value for value in secret_values(secrets_dir) if value in session.output] == []


def test_a_second_run_changes_nothing(go_live, keycloak, session, secrets_dir):
    """#19 rule 3: a run after a finished one changes and asks nothing."""
    go_live.run()
    commands_before, asked_before = len(keycloak.commands), len(session.asked)

    assert go_live.run() == 0

    assert not Keycloak.changed_something(Keycloak(commands=keycloak.commands[commands_before:]))
    assert len(session.asked) == asked_before
    assert "curl -s https://auth.hallucinai.de" in session.output


def test_a_run_whose_apply_failed_applies_again_and_goes_on(go_live, keycloak, session):
    """#19 rule 3: a run stopped inside the apply resumes at the apply."""
    keycloak.fail_apply_after_master = True
    assert go_live.run() == 1
    assert "FAILED at realms applied" in session.output
    keycloak.fail_apply_after_master = False
    keycloak.commands.clear()

    assert go_live.run() == 0

    assert keycloak.import_completed
    assert not any("/opt/edge/admin-frontend.sh" in command for command in keycloak.commands)
    assert len(session.asked) == 2  # the backup question of the first run, Enter of the second


def test_a_realm_whose_import_never_completed_is_applied_again(go_live, keycloak, session):
    """#19 rule 3: a realm that exists is not an import that finished."""
    assert go_live.run() == 0
    keycloak.import_completed = False
    keycloak.commands.clear()

    assert go_live.run() == 0

    assert keycloak.import_completed
    assert any("keycloak-config-cli" in command for command in keycloak.commands)


def test_a_first_installation_applies_when_the_edge_already_stands(go_live, keycloak, session):
    """#19 rules 1 and 3: Traefik up before the first apply; config-cli does not exist yet."""
    keycloak.edge_up = True

    assert go_live.run() == 0

    assert keycloak.master_applied
    assert keycloak.import_completed


def test_a_completed_apply_is_recognised_before_the_edge_stands(go_live, keycloak, session):
    """#19 rule 3: the run killed between the apply and the edge continues at the edge."""
    keycloak.fail_edge_start = True
    assert go_live.run() == 1
    keycloak.fail_edge_start = False
    keycloak.commands.clear()

    assert go_live.run() == 0

    assert not any("keycloak-config-cli" in command for command in keycloak.commands)


def test_the_apply_check_hands_the_client_secret_over_on_standard_input(
    go_live, keycloak, secrets_dir
):
    """#19 rule 2: the secret is in no command line."""
    assert go_live.run() == 0

    secret = (secrets_dir / "config-cli-secret").read_text()
    assert keycloak.stdin_seen == secret + "\n"
    assert not any(secret in part for command in keycloak.commands for part in command)


def test_a_bootstrap_file_removed_by_hand_does_not_retire_temp_admin(
    go_live, keycloak, session, secrets_dir
):
    """#19 rule 3: the done-state is temp-admin gone, not the password file gone."""
    assert go_live.run() == 0
    keycloak.add_admin("temp-admin", otp=True)
    keycloak.deleted.clear()
    session.answers = ["yes", ""]

    assert go_live.run() == 0

    assert keycloak.deleted == ["temp-admin"]
    assert not (secrets_dir / "bootstrap-admin-password").exists()


def test_a_finished_installation_resumes_after_docker_compose_down(
    go_live, keycloak, session, secrets_dir
):
    """#19 rules 2, 3 and #55 rule 3: a retired bootstrap file is no partial set; the run
    restarts Keycloak from the kept database, applies nothing again and asks no backup question."""
    assert go_live.run() == 0
    keycloak.down()
    keycloak.commands.clear()
    session.asked.clear()

    assert go_live.run() == 0

    assert keycloak.running
    assert keycloak.edge_up
    assert not any("keycloak-config-cli" in command for command in keycloak.commands)
    assert not any("some secret files exist" in line for line in session.printed)
    assert session.asked == []


def test_a_run_stopped_after_the_apply_never_moves_master_again_and_starts_the_edge(
    go_live, keycloak, session
):
    """#19 rule 3: a run killed after the apply goes on with the edge, not with step 4's move."""
    keycloak.fail_edge_start = True
    assert go_live.run() == 1
    assert "FAILED at edge without the overlay" in session.output
    assert keycloak.import_completed
    keycloak.fail_edge_start = False
    keycloak.commands.clear()

    assert go_live.run() == 0

    assert keycloak.edge_up
    assert not any("/opt/edge/admin-frontend.sh" in command for command in keycloak.commands)
    assert len(session.asked) == 2


def test_a_partial_set_missing_only_the_bootstrap_password_is_refused_on_a_fresh_install(
    go_live, keycloak, secrets_dir, session
):
    """#19 rule 2: no database yet, so one line names the missing file and nothing starts."""
    write_secrets(secrets_dir, "db-password", "config-cli-secret")

    assert go_live.run() == 1

    assert session.printed[-1].startswith(
        "go-live FAILED at secrets: the database volume keycloak-db is missing"
    )
    assert not keycloak.changed_something()


def test_a_missing_bootstrap_password_on_a_master_that_has_not_moved_is_refused_by_name(
    go_live, keycloak, secrets_dir, session
):
    """#19 rule 3: step 4 cannot move master without the file and says so; nothing is applied."""
    keycloak.database_volume = True
    write_secrets(secrets_dir, "db-password", "config-cli-secret")

    assert go_live.run() == 1

    assert "bootstrap-admin-password is gone but master is not on" in session.printed[-1]
    assert not keycloak.master_applied


def test_a_docker_failure_while_looking_for_the_database_volume_is_reported_as_it_is(
    go_live, keycloak, secrets_dir, session
):
    """#19 rule 3: a stopped daemon is not an absent volume; the line carries docker's error."""
    keycloak.docker_daemon_down = True
    write_secrets(secrets_dir, "db-password", "config-cli-secret")

    assert go_live.run() == 1

    assert session.printed[-1] == (
        "go-live FAILED at backup: `docker volume inspect` exited 1: "
        "Cannot connect to the Docker daemon"
    )
    assert not keycloak.changed_something()


def test_the_prompt_for_a_hand_removed_bootstrap_file_does_not_point_at_it(
    go_live, keycloak, session, secrets_dir
):
    """#19 rule 3: the instruction never sends the operator to a password file that is gone."""
    assert go_live.run() == 0
    keycloak.add_admin("temp-admin", otp=True)
    session.printed.clear()
    session.answers = ["yes", ""]

    assert go_live.run() == 0

    assert "password: the file" not in session.output
    assert "bootstrap-admin-password file is gone" in session.output


def test_the_backup_is_asked_even_when_the_secret_files_were_made_by_hand(
    go_live, secrets_dir, session
):
    """#19 rule 6: existing secrets are no answer to the backup question."""
    write_secrets(secrets_dir, *SECRET_FILES)

    go_live.run()

    assert session.asked[0].startswith("edge-backup is not set up on this PC")


def test_existing_secret_files_are_never_overwritten(go_live, secrets_dir, session):
    """#19 rule 2: an existing file stays; a partial set is refused naming the missing ones."""
    write_secrets(secrets_dir, "db-password")

    assert go_live.run() == 1

    assert (secrets_dir / "db-password").read_text() == "value-of-db-password"
    assert sorted(path.name for path in secrets_dir.iterdir()) == ["db-password"]
    assert session.printed[-1] == (
        "go-live FAILED at secrets: some secret files exist, "
        "missing: config-cli-secret, bootstrap-admin-password"
    )


@pytest.mark.parametrize(
    ("second_admin", "reason"),
    [
        (None, "no other enabled master user has the realm role admin"),
        ({"otp": False}, "alice has the role admin but no TOTP credential"),
        ({"otp": True, "enabled": False}, "no other enabled master user has the realm role admin"),
    ],
)
def test_the_bootstrap_stays_without_a_second_admin_who_has_otp(
    go_live, keycloak, session, secrets_dir, second_admin, reason
):
    """#19 rule 4: no retirement without a second admin who has TOTP."""
    session.on_enter = (
        None if second_admin is None else lambda: keycloak.add_admin(OTP_ADMIN, **second_admin)
    )

    assert go_live.run() == 1

    assert (
        session.printed[-1]
        == f"go-live FAILED at bootstrap retired: refusing to retire temp-admin: {reason}"
    )
    assert keycloak.deleted == []
    assert (secrets_dir / "bootstrap-admin-password").exists()


def test_a_failure_with_an_unreadable_secret_file_is_still_one_line(go_live, secrets_dir, session):
    """defect:#19 - reporting a failure must not itself raise (rule 2: never a traceback)."""
    secrets_dir.mkdir(parents=True, mode=0o700)
    (secrets_dir / "db-password").mkdir()  # exists, cannot be read as a file

    assert go_live.run() == 1

    assert session.printed[-1].startswith("go-live FAILED at secrets: some secret files exist")


def test_an_unexpected_answer_ends_in_one_line_not_a_traceback(go_live, session):
    """defect:#19 - any exception at the boundary is one line naming the step and its type."""

    def answer_of_the_wrong_shape(*_args, **_kwargs):
        raise AttributeError("'list' object has no attribute 'get'")

    go_live.tools = dataclasses.replace(go_live.tools, fetch=answer_of_the_wrong_shape)

    assert go_live.run() == 1

    assert "unexpected answer (AttributeError)" in session.printed[-1]


def test_the_run_ends_at_the_failed_step_with_one_line_and_exit_one(go_live, keycloak, session):
    """#19 rule 1: a failure is one line naming the step, exit 1."""
    keycloak.fail_apply_after_master = True
    keycloak.master_applied = True  # the overlay grant is refused, the client grant fails too

    assert go_live.run() == 1

    assert session.printed[-1] == (
        "go-live FAILED at realms applied: `docker compose run` exited 1: realm.yaml: import failed"
    )


@pytest.mark.parametrize("answers", [["no"], []])
def test_without_an_accepted_backup_nothing_is_created(
    go_live, secrets_dir, keycloak, session, answers
):
    """#19 rule 6: no accepted backup, no change."""
    session.answers = answers

    assert go_live.run() == 1

    assert not secrets_dir.exists()
    assert not keycloak.changed_something()
    assert "FAILED at backup" in session.printed[-1]


def test_a_set_up_edge_backup_asks_no_backup_question(go_live, keycloak, session, home):
    """B1, #127 rule 3: no question, no volume lookup; the run asks only Enter."""
    set_up_backup(home)

    assert go_live.run() == 0

    assert not any("volume inspect" in command for command in keycloak.commands)
    assert "backup" not in session.output.split("Step 2")[0]
    assert len(session.asked) == 1
    assert session.asked[0].startswith("Press Enter")


def break_password_mode(home: Path) -> None:
    (home / ".secrets/restic/password").chmod(0o644)


def remove_repository_config(home: Path) -> None:
    (home / "backups/restic/config").unlink()


def remove_password_file(home: Path) -> None:
    (home / ".secrets/restic/password").unlink()


def make_password_a_directory(home: Path) -> None:
    (home / ".secrets/restic/password").unlink()
    (home / ".secrets/restic/password").mkdir()


def hide_password_directory(home: Path) -> None:
    (home / ".secrets/restic").chmod(0)


@pytest.mark.parametrize(
    "spoil",
    [
        remove_password_file,
        remove_repository_config,
        break_password_mode,
        make_password_a_directory,
        hide_password_directory,
    ],
)
def test_an_unclear_edge_backup_reads_as_not_set_up(go_live, session, home, spoil):
    """B4, B5, #127 rule 5: missing, wrong mode, a directory or no permission means ask."""
    set_up_backup(home)
    spoil(home)
    session.answers = ["no"]

    try:
        assert go_live.run() == 1
    finally:
        (home / ".secrets/restic").chmod(0o700)

    assert session.asked[0].startswith("edge-backup is not set up on this PC")
    assert "Traceback" not in session.output


def test_a_set_up_edge_backup_never_fails_the_backup_step_on_a_down_docker(
    go_live, keycloak, session, home
):
    """B8: no backup line and no question; Docker is first needed by a later step."""
    set_up_backup(home)
    keycloak.docker_daemon_down = True

    go_live.run()

    assert "FAILED at backup" not in session.output
    assert not any(prompt.startswith("edge-backup is not set up") for prompt in session.asked)


def test_the_backup_question_has_no_default(go_live, session):
    """#19 rule 6: only yes or no answers the backup question."""
    session.answers = ["", "maybe", "yes", ""]
    go_live.run()
    assert session.asked[:3].count(session.asked[0]) == 3


def test_a_keycloak_that_never_answers_ends_the_step_after_the_timeout(go_live, keycloak, session):
    """#19 rule 3: a Keycloak that never answers ends the step at the timeout."""
    keycloak.never_answers = True

    assert go_live.run() == 1

    assert session.printed[-1] == "go-live FAILED at keycloak start: Keycloak not ready after 240s"
    assert session.clock >= 240


def test_each_step_says_what_happens_and_why_before_it_runs(go_live, session):
    """#19 rule 7: every step is announced before it runs."""
    go_live.run()

    announcements = [line for line in session.printed if line.startswith("Step ")]
    assert [line.split(",")[0] for line in announcements] == [
        "Step 1",
        "Step 2",
        "Step 3",
        "Step 4a",
        "Step 4b",
        "Step 5",
        "Step 6-7",
    ]
    assert (
        "only so the realms can be applied once; that admin is deleted in step 7" in session.output
    )


def test_the_pause_says_what_to_do_and_how_to_check_it(go_live, session):
    """#19 rule 7: the browser pause names the action and its check."""
    go_live.run()

    assert "It worked when" in session.output
    assert "http://localhost:8090/admin/" in session.output


def test_help_lists_the_steps_in_plain_words():
    """#19 rule 7: --help lists the steps in plain words."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "3. keycloak start: Start Keycloak with a temporary admin" in result.stdout


def numbers_in(lines: list[str], pattern: str) -> list[str]:
    return [match.group(1) for line in lines if (match := re.match(pattern, line))]


def test_the_readme_and_help_number_the_go_live_steps_alike():
    """#55 rule 5: every number the script prints is in the README once, and no other."""
    help_text = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    ).stdout
    readme = (SCRIPT.parents[1] / "README.md").read_text()
    go_live_section = readme.split("### Keycloak go-live")[1].split("####")[0]
    fallback_section = readme.split("#### Fallback: the steps by hand")[1].split("###")[0]

    help_steps = numbers_in(help_text.splitlines(), r"(\d+[ab]?(?:-\d+)?)\. ")
    readme_pattern = (
        r"- \*\*(\d+[ab]?(?:-\d+)?)\. "  # a bullet keeps "4a." literal; "4a. " is no list marker
    )

    assert help_steps == numbers_in(go_live_section.splitlines(), readme_pattern)
    assert help_steps == numbers_in(fallback_section.splitlines(), readme_pattern)
    assert help_steps[0] == "1"


def test_a_refused_backup_is_asked_again_on_the_next_run(go_live, keycloak, session, secrets_dir):
    """#55 rule 3: a no created nothing, so the question stands."""
    session.answers = ["no", "no"]

    go_live.run()
    go_live.run()

    assert len(session.asked) == 2
    assert not keycloak.database_volume
    assert not secrets_dir.exists()
    assert not keycloak.changed_something()


def test_a_completed_apply_is_not_repeated_while_step_5_restarts_keycloak(
    go_live, keycloak, session
):
    """#55 rule 4: the run waits for 8090 and reads the import there, not through kcadm (401)."""
    assert go_live.run() == 0
    keycloak.edge_up, keycloak.edge_restarting = False, True

    def edge_comes_up(_seconds: float) -> None:
        keycloak.edge_up, keycloak.edge_restarting = True, False

    go_live.tools = dataclasses.replace(go_live.tools, sleep=edge_comes_up)
    keycloak.commands.clear()

    assert go_live.run() == 0

    assert not any("keycloak-config-cli" in command for command in keycloak.commands)


@pytest.fixture
def finished(go_live, keycloak, session):
    assert go_live.run() == 0
    keycloak.down()
    keycloak.commands.clear()
    session.printed.clear()
    session.asked.clear()


CHECK_PREFIX = "keycloak/go-live --check"


def test_check_says_set_up_for_a_finished_installation_with_keycloak_stopped(
    go_live, keycloak, session, finished
):
    """#55 rule 2: Keycloak need not run."""
    assert go_live.check() == 0

    assert session.printed == [f"{CHECK_PREFIX}: set up"]


def test_check_changes_nothing_asks_nothing_and_prints_no_secret(
    go_live, keycloak, session, secrets_dir, finished
):
    """#55 rule 1: only read-only docker questions, no prompt, no file change."""
    files_before = {path.name: path.read_bytes() for path in secrets_dir.iterdir()}

    go_live.check()

    assert not keycloak.changed_something()
    assert not any("exec" in command for command in keycloak.commands)
    assert session.asked == []
    assert {path.name: path.read_bytes() for path in secrets_dir.iterdir()} == files_before
    assert [value for value in secret_values(secrets_dir) if value in session.output] == []


def test_check_on_a_fresh_home_says_not_set_up_and_creates_nothing(
    go_live, keycloak, session, secrets_dir
):
    """#55 rule 2, counterexample 1."""
    assert go_live.check() == 1

    assert session.printed == [
        f"{CHECK_PREFIX}: not set up: no secrets in ~/.secrets/keycloak; run keycloak/go-live"
    ]
    assert not secrets_dir.exists()
    assert keycloak.commands == []


def test_check_with_the_bootstrap_file_present_names_steps_6_and_7(
    go_live, keycloak, session, secrets_dir
):
    """#55 rule 2, counterexample 2: go-live stopped before step 7."""
    keycloak.database_volume = True
    write_secrets(secrets_dir, *SECRET_FILES)

    assert go_live.check() == 1

    assert "steps 6-7" in session.printed[-1]
    assert session.printed[-1].startswith(f"{CHECK_PREFIX}: not set up: ")


def test_check_on_a_partial_set_repeats_go_lives_own_line(go_live, keycloak, session, secrets_dir):
    """#55 rule 2: a partial set that go-live refuses is not set up."""
    write_secrets(secrets_dir, "db-password")

    assert go_live.check() == 1

    assert session.printed == [
        f"{CHECK_PREFIX}: not set up: "
        "some secret files exist, missing: config-cli-secret, bootstrap-admin-password"
    ]


def test_check_names_the_missing_database_volume_when_only_the_bootstrap_file_is_gone(
    go_live, keycloak, session, secrets_dir
):
    """#64 rule 11 (#34 finding 23 c): the line names the volume and deletes nothing."""
    write_secrets(secrets_dir, "db-password", "config-cli-secret")

    assert go_live.check() == 1

    assert session.printed == [
        f"{CHECK_PREFIX}: not set up: the database volume keycloak-db is missing although "
        "bootstrap-admin-password is gone: go-live cannot finish on an empty database; "
        "restore the volume"
    ]
    assert sorted(path.name for path in secrets_dir.iterdir()) == [
        "config-cli-secret",
        "db-password",
    ]


def test_check_cannot_tell_when_docker_does_not_answer(go_live, keycloak, session, secrets_dir):
    """#55 rule 2: exit 2, and the line carries docker's error."""
    keycloak.docker_daemon_down = True
    write_secrets(secrets_dir, "db-password", "config-cli-secret")

    assert go_live.check() == 2

    assert session.printed == [
        f"{CHECK_PREFIX}: cannot tell: `docker volume inspect` exited 1: "
        "Cannot connect to the Docker daemon"
    ]


def test_check_cannot_tell_when_compose_answers_without_the_database_volume(
    go_live, keycloak, session, secrets_dir
):
    """#55 rule 2: an unexpected answer is exit 2 and one line, never a traceback."""
    write_secrets(secrets_dir, "db-password", "config-cli-secret")
    keycloak.compose_without_database_volume = True

    assert go_live.check() == 2

    assert session.printed == [f"{CHECK_PREFIX}: cannot tell: unexpected answer (KeyError)"]


def test_check_through_the_command_line_exits_with_the_answer(tmp_path):
    """#55 rule 1: the real entry point on a fresh HOME exits 1 and writes nothing."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": "/nonexistent"},
    )

    assert result.returncode == 1
    assert result.stdout.startswith(f"{CHECK_PREFIX}: not set up: no secrets")
    assert list(tmp_path.iterdir()) == []


# --- the emergency switch (#64 rule 8) ----------------------------------------------------------

SWITCHED_OFF_AT = "2026-10-17T22:10:04+02:00"
START_VERBS = {"up", "run"}


def switch_off(marks: switches.Switches) -> None:
    marks.record("keycloak", datetime.fromisoformat(SWITCHED_OFF_AT))


def started_something_after(keycloak: Keycloak, command_count: int) -> bool:
    return any(START_VERBS & set(command) for command in keycloak.commands[command_count:])


def test_a_switched_off_keycloak_refuses_at_the_start_and_asks_and_starts_nothing(
    go_live, keycloak, session, marks, secrets_dir
):
    """T10, #64 rule 8: the marker stops the whole run before its first question."""
    name_clients(keycloak, secrets_dir, "a")
    switch_off(marks)

    assert go_live.run() == 1

    assert session.printed == [
        "go-live: refused, keycloak is switched off (since 17.10. 22:10); "
        "`edge start keycloak` ends the switch"
    ]
    assert session.asked == []
    assert keycloak.commands == []
    assert not secrets_dir.exists()


def test_the_check_ignores_the_marker(go_live, keycloak, session, marks, secrets_dir):
    """#64 rule 8: --check starts nothing, so a switched-off unit is judged on its trace."""
    switch_off(marks)
    write_secrets(secrets_dir, "db-password", "config-cli-secret")
    keycloak.database_volume = True

    assert go_live.check() == 0

    assert session.printed == [f"{CHECK_PREFIX}: set up"]


@pytest.mark.parametrize(
    "stopped_before", ["keycloak start", "realms applied", "edge without the overlay"]
)
def test_a_stop_made_before_a_start_wins_and_that_start_does_not_happen(
    go_live, keycloak, session, marks, stopped_before
):
    """#64 rule 8: the marker is read again right before each of the three starts."""
    commands_at_the_stop: list[int] = []

    def stop_when_the_step_is_announced(line: str) -> None:
        session.printed.append(line)
        if re.match(rf"Step \w+(-\w+)?, {stopped_before}:", line):
            switch_off(marks)
            commands_at_the_stop.append(len(keycloak.commands))

    go_live.tools = dataclasses.replace(go_live.tools, say=stop_when_the_step_is_announced)

    assert go_live.run() == 1

    assert session.printed[-1] == (
        f"go-live FAILED at {stopped_before}: keycloak is switched off (since 17.10. 22:10); "
        "`edge start keycloak` ends the switch"
    )
    assert not started_something_after(keycloak, commands_at_the_stop[0])


def stopping_after_a_command(go_live, marks, is_the_one):
    """Swaps the run port for one that writes the marker right after the command that matches."""
    run = go_live.tools.run

    def run_then_stop(command, timeout, stdin=None):
        result = run(command, timeout, stdin)
        if is_the_one(command):
            switch_off(marks)
        return result

    go_live.tools = dataclasses.replace(go_live.tools, run=run_then_stop)


def test_a_stop_made_during_the_edge_start_prevents_the_recreate(go_live, keycloak, session, marks):
    """#64 rule 8: the marker is read again before the recreate, not once for both commands."""
    stopping_after_a_command(go_live, marks, lambda command: command[-2:] == ["up", "-d"])

    assert go_live.run() == 1

    assert session.printed[-1].startswith("go-live FAILED at edge without the overlay: keycloak is")
    assert not any("--force-recreate" in command for command in keycloak.commands)


def test_a_stop_made_between_the_two_realm_attempts_wins(go_live, keycloak, marks, session):
    """#64 rule 8: the fallback `run` reads the marker again after the first attempt failed."""
    keycloak.master_applied = True  # the overlay attempt is refused with 401, as in life
    stopping_after_a_command(go_live, marks, lambda command: "run" in command)

    assert go_live.run() == 1

    assert session.printed[-1] == (
        "go-live FAILED at realms applied: keycloak is switched off (since 17.10. 22:10); "
        "`edge start keycloak` ends the switch"
    )
    assert sum("run" in command for command in keycloak.commands) == 1


def lock_is_free(marks: switches.Switches) -> bool:
    marks.directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(marks.directory / switches.LOCK_FILE, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)


def test_starts_hold_the_switch_lock_and_prompts_leave_it_free(go_live, marks):
    """#64 rule 8: every start runs under the switch lock; the operator's waits never do."""
    run, ask = go_live.tools.run, go_live.tools.ask
    free_during_starts: list[bool] = []
    free_during_prompts: list[bool] = []

    def watched_run(command, timeout, stdin=None):
        if START_VERBS & set(command):
            free_during_starts.append(lock_is_free(marks))
        return run(command, timeout, stdin)

    def watched_ask(prompt):
        free_during_prompts.append(lock_is_free(marks))
        return ask(prompt)

    go_live.tools = dataclasses.replace(go_live.tools, run=watched_run, ask=watched_ask)

    assert go_live.run() == 0

    assert free_during_starts
    assert not any(free_during_starts)
    assert len(free_during_prompts) == 2
    assert all(free_during_prompts)


def test_a_run_without_a_marker_starts_as_before_and_leaves_no_marker(go_live, keycloak, marks):
    """#64 rule 8, counterexample: the guard costs a normal run nothing."""
    assert go_live.run() == 0

    assert marks.since("keycloak") is None


# --- one secret file per confidential app client (K11-SA1, #106) --------------------------------

CLIENT_MISSING_LINE = (
    f"{CHECK_PREFIX}: not set up: the secret of client {{}} is missing "
    "(~/.secrets/keycloak/clients/{}); run keycloak/go-live"
)


def modes(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_fresh_run_creates_one_private_secret_file_per_client(
    go_live, keycloak, session, secrets_dir
):
    """T1: the three service files, plus clients/a and clients/b at 0644 in a 0700 directory."""
    name_clients(keycloak, secrets_dir, "a", "b")

    assert go_live.run() == 0

    assert modes(secrets_dir / "clients") == 0o700
    for client_id in ("a", "b"):
        assert modes(secrets_dir / "clients" / client_id) == 0o644
        assert re.fullmatch(r"[0-9a-f]{64}", (secrets_dir / "clients" / client_id).read_text())
    assert len({(secrets_dir / "clients" / name).read_text() for name in ("a", "b")}) == 2
    assert (secrets_dir / "config-cli-secret").exists()


def test_every_discovery_asks_compose_with_the_apply_profile(go_live, keycloak, secrets_dir):
    """T9, Rule 2: without --profile apply compose drops keycloak-config-cli's secrets."""
    go_live.client_ids()

    assert "apply" in keycloak.commands[-1]


def test_a_rerun_with_one_client_file_missing_creates_only_that_file_and_asks_nothing(
    go_live, keycloak, session, secrets_dir
):
    """T2 and T3: the delta run leaves the other file byte-identical and starts nothing."""
    name_clients(keycloak, secrets_dir, "a", "b")
    assert go_live.run() == 0
    (secrets_dir / "clients" / "b").unlink()
    kept = (secrets_dir / "clients" / "a").read_bytes()
    keycloak.commands.clear()
    session.asked.clear()

    assert go_live.run() == 0

    assert (secrets_dir / "clients" / "a").read_bytes() == kept
    assert (secrets_dir / "clients" / "b").exists()
    assert session.asked == []
    assert not keycloak.changed_something()


def test_check_names_a_missing_client_secret_and_the_next_command(
    go_live, keycloak, session, secrets_dir, finished
):
    """T4: exit 1 and one line."""
    name_clients(keycloak, secrets_dir, "a", "b")
    (secrets_dir / "clients").mkdir()
    (secrets_dir / "clients" / "a").write_text("x")

    assert go_live.check() == 1

    assert session.printed == [CLIENT_MISSING_LINE.format("b", "b")]
    assert not (secrets_dir / "clients" / "b").exists()


def test_check_is_set_up_when_every_client_secret_exists(
    go_live, keycloak, session, secrets_dir, finished
):
    """T5."""
    name_clients(keycloak, secrets_dir, "a")
    (secrets_dir / "clients").mkdir()
    (secrets_dir / "clients" / "a").write_text("x")

    assert go_live.check() == 0


def test_a_compose_config_failure_is_exit_two_for_check_and_one_line_for_a_run(
    go_live, keycloak, session, secrets_dir, finished
):
    """T6."""
    keycloak.compose_config_fails = True

    assert go_live.check() == 2
    assert session.printed == [
        f"{CHECK_PREFIX}: cannot tell: `docker compose config` exited 1: "
        "validating compose.yaml: invalid"
    ]
    session.printed.clear()
    assert go_live.run() == 1
    assert len(session.printed) == 1
    assert session.printed[0].startswith("go-live FAILED at backup:")


def test_a_client_secret_quoted_by_a_failing_command_is_printed_as_stars(
    go_live, keycloak, session, secrets_dir
):
    """T7: the value of a client file is scrubbed like the service passwords."""
    name_clients(keycloak, secrets_dir, "a")
    keycloak.fail_apply_after_master = True
    keycloak.echo_secret_file = secrets_dir / "clients" / "a"

    go_live.run()

    assert session.printed[-1].endswith("realm.yaml: import failed ***")
    assert [value for value in secret_values(secrets_dir) if value in session.output] == []


@pytest.mark.parametrize("name", ["../x", "A", "-a", "a/b"])
def test_a_client_name_that_is_not_an_id_is_refused_and_nothing_is_created(
    go_live, keycloak, session, secrets_dir, name
):
    """T8."""
    name_clients(keycloak, secrets_dir, name)

    assert go_live.run() == 1

    assert "FAILED at client secrets" in session.printed[-1]
    assert not (secrets_dir / "clients").exists()
    assert not (secrets_dir.parent / "x").exists()


@pytest.fixture
def real_compose_go_live(golive, secrets_dir, marks, monkeypatch):
    """Compose resolves `${HOME}/.secrets/keycloak` to the injected secrets directory."""
    monkeypatch.setenv("HOME", str(secrets_dir.parent.parent))
    monkeypatch.setenv("EDGE_DOMAIN", "probe.test")  # the operator's .env supplies both
    monkeypatch.setenv("DOCKER_GID", "999")
    return golive.GoLive(golive.Tools(), secrets_dir, marks)


def test_the_real_compose_file_names_songmakers_client_secret(real_compose_go_live):
    """T9, and #108 T12: the repository's compose.yaml declares songmaker's client secret."""
    assert real_compose_go_live.client_ids() == ["songmaker"]


def test_the_real_compose_discovers_a_client_secret_it_declares(
    golive, real_compose_go_live, tmp_path, monkeypatch
):
    """T9, non-vacuity: the same real Compose and HOME find `clients/x` when compose declares it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "compose.yaml").write_text(
        "name: probe\n"
        "services:\n"
        "  app:\n"
        "    image: scratch\n"
        "    secrets: [x]\n"
        "secrets:\n"
        "  x:\n"
        "    file: ${HOME}/.secrets/keycloak/clients/x\n"
    )
    monkeypatch.setattr(golive, "REPO", repo)

    assert real_compose_go_live.client_ids() == ["x"]


def test_a_top_level_secret_without_a_file_is_not_a_client_secret(go_live, keycloak):
    """T9, Rule 2: a secret from `environment:` has no file and names no client."""
    keycloak.compose_declares_environment_secret = True

    assert go_live.client_ids() == []


def test_a_failed_write_removes_the_file_it_created_and_never_one_that_existed(
    golive, secrets_dir, marks
):
    """T3, Rule 3: no empty file is left to read as done; an existing file is untouched."""
    secrets_dir.mkdir(parents=True)
    existing = secrets_dir / "existing"
    existing.write_text("kept")
    fresh = secrets_dir / "fresh"

    def fail_to_generate() -> str:
        raise OSError("disk full")

    go_live = golive.GoLive(golive.Tools(new_secret=fail_to_generate), secrets_dir, marks)

    with pytest.raises(OSError, match="disk full"):
        go_live.create_secret_file(fresh)
    with pytest.raises(FileExistsError):
        go_live.create_secret_file(existing)

    assert not fresh.exists()
    assert existing.read_text() == "kept"
