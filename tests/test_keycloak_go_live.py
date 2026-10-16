"""keycloak/go-live against a scripted compose project and Keycloak: no containers, no sleeping."""

import dataclasses
import importlib.machinery
import importlib.util
import json
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs

import pytest

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
    docker_daemon_down: bool = False
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
        return self.done(json.dumps({"volumes": {"keycloak-db": {"name": PROJECT_VOLUME}}}))

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
            if self.edge_up:  # the real kcadm is refused with 401 once the edge stands
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
def go_live(golive: ModuleType, keycloak: Keycloak, session: Session, secrets_dir: Path):
    tools = golive.Tools(
        run=keycloak.run,
        fetch=keycloak.fetch,
        ask=session.ask,
        say=session.printed.append,
        sleep=session.sleep,
        monotonic=session.monotonic,
    )
    return golive.GoLive(tools, secrets_dir)


def write_secrets(secrets_dir: Path, *names: str) -> None:
    secrets_dir.mkdir(parents=True, mode=0o700)
    for name in names:
        (secrets_dir / name).write_text(f"value-of-{name}")


def secret_values(secrets_dir: Path) -> list[str]:
    return [
        (secrets_dir / name).read_text() for name in SECRET_FILES if (secrets_dir / name).exists()
    ]


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
        assert profiles == {"keycloak"}


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
    """#19 rules 2, 3: a retired bootstrap file is no partial set; the run restarts Keycloak
    from the kept database without applying anything again."""
    assert go_live.run() == 0
    keycloak.down()
    keycloak.commands.clear()
    session.answers = ["yes"]

    assert go_live.run() == 0

    assert keycloak.running
    assert keycloak.edge_up
    assert not any("keycloak-config-cli" in command for command in keycloak.commands)
    assert not any("some secret files exist" in line for line in session.printed)


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

    assert session.printed[-1] == (
        "go-live FAILED at secrets: some secret files exist, missing: bootstrap-admin-password"
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
        "go-live FAILED at secrets: `docker volume inspect` exited 1: "
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

    assert session.asked[0].startswith("K11b")


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
        "Step 4",
        "Step 4",
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
