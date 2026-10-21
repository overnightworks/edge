"""The realm step of `edge apply --closed` (#242): when it runs (the plan) and what it does (the
step), with Compose, the observer and edge-backup faked. Each test names its cell of the item's
scenario matrix."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from edge import compose, core, switches
from edge.realm import reconcile
from edge.realm.reconcile import RealmAction, RealmPlan

REPOSITORY = Path(__file__).resolve().parents[1]
SNAPSHOT = "ab12cd34"
ATTRIBUTE = "de.adorsys.keycloak.config.import-checksum-default"


def group(name: str, members: int = 0) -> dict:
    return {"fields": {"name": name, "members": members}, "digests": {}}


def inventory(*groups: dict) -> dict:
    return {"hallucinai": {"group": {entry["fields"]["name"]: entry for entry in groups}}}


@dataclass
class World:
    """The repository, the record, the switches and every fake the step drives, with one log of
    what they were asked in order."""

    root: Path
    events: list[str] = field(default_factory=list)
    definition: str = "definition one"
    checksum: str = "checksum one"
    config_cli_running: bool = False
    failing: set[str] = field(default_factory=set)
    interrupted: set[str] = field(default_factory=set)
    before: dict = field(default_factory=lambda: inventory(group("apps")))
    after: dict = field(default_factory=lambda: inventory(group("apps")))
    meanwhile: Callable[[], None] | None = None  # what changes right before config-cli runs

    def __post_init__(self) -> None:
        (self.root / "keycloak").mkdir(parents=True)
        self.write_file("realm.yaml", "realm: one\n")
        self.write_file("master.yaml", "master: one\n")
        self.write_file("entrypoint.sh", "#!/bin/sh\n")
        self.record = core.Record(self.root / "state")
        self.marks = switches.Switches(self.root / "state")

    def write_file(self, name: str, text: str) -> None:
        (self.root / "keycloak" / name).write_text(text)

    @property
    def config(self) -> dict:
        """config-cli as compose.yaml resolves it: its entrypoint is a file bind, `keycloak/` a
        directory bind."""
        binds = [
            {"type": "bind", "source": str(self.root / "keycloak/entrypoint.sh")},
            {"type": "bind", "source": str(self.root / "keycloak")},
        ]
        return {"services": {reconcile.CONFIG_CLI: {"volumes": binds}}}

    # the fakes ---------------------------------------------------------------------------------

    def config_hashes(self, services: tuple[str, ...]) -> dict[str, str]:
        return {service: self.definition for service in services}

    def states(self) -> dict[str, set[str]]:
        return {reconcile.CONFIG_CLI: {"running"}} if self.config_cli_running else {}

    def run_once(self, service: str, arguments: list[str], stdin: str = "") -> str:
        assert service == reconcile.CONFIG_CLI
        assert not arguments
        assert not stdin
        self.act("config-cli")
        if self.meanwhile:
            self.meanwhile()
        return ""

    def act(self, name: str) -> None:
        self.events.append(name)
        if name in self.interrupted:
            raise KeyboardInterrupt
        if name in self.failing:
            raise reconcile.RealmError(f"{name} broke")

    def pre_apply(self) -> str:
        self.act("backup")
        return SNAPSHOT

    def restore_test(self, snapshot: str) -> None:
        assert snapshot == SNAPSHOT
        self.act("restore test")

    def inventory(self) -> dict:
        self.act("observe")
        done = "config-cli" in self.events
        return self.after if done else self.before

    def checksums(self) -> dict:
        self.act("checksums")
        return {"master": {}, "hallucinai": {ATTRIBUTE: self.checksum}}

    # the step ----------------------------------------------------------------------------------

    def plan(self, gate: core.Gate | None = core.Gate.ON, starts_now: bool = False) -> RealmPlan:
        return reconcile.plan_realm(
            self, self, self.record, self.config, self.root, gate, starts_now
        )

    def run(self, plan: RealmPlan, mark: switches.Mark | None = None) -> reconcile.StepResult:
        realm = reconcile.Realm(
            self,
            self,
            self,
            self.record,
            self.config,
            self.root,
            self.marks,
            mark,
            clock=lambda: 0.0,
        )
        return realm.run(plan)

    def step(self, mark: switches.Mark | None = None) -> reconcile.StepResult:
        """Plan with every fault disarmed, then run the step with them armed."""
        faults = self.failing, self.interrupted
        self.failing, self.interrupted = set(), set()
        plan = self.plan()
        self.events.clear()
        self.failing, self.interrupted = faults
        return self.run(plan, mark)

    def applied_once(self) -> reconcile.StepResult:
        result = self.step()
        self.events.clear()
        return result


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "repository")


# --- When the step runs ------------------------------------------------------------------------


def test_nothing_changed_since_the_last_apply_plans_no_step_and_no_line(world: World) -> None:
    """Cells 1 and 4: a second apply right after the first does nothing, quietly."""
    assert world.applied_once().failure is None

    plan = world.plan()

    assert (plan.action, plan.lines, plan.settled) == (RealmAction.NOTHING, [], True)


def test_no_recorded_apply_plans_the_step(world: World) -> None:
    plan = world.plan()

    assert (plan.action, plan.settled) == (RealmAction.RUN, False)
    assert plan.lines == ["realm: apply keycloak/*.yaml (no realm apply is recorded)"]


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (
            lambda world: world.write_file("realm.yaml", "realm: two\n"),
            "keycloak/realm.yaml changed",
        ),
        (
            lambda world: world.write_file("master.yaml", "master: two\n"),
            "keycloak/master.yaml changed",
        ),
        (
            lambda world: world.write_file("entrypoint.sh", "#!/bin/sh\nexit 1\n"),
            "keycloak/entrypoint.sh changed",
        ),
        (
            lambda world: setattr(world, "definition", "definition two"),
            "config-cli's definition changed",
        ),
        (
            lambda world: setattr(world, "checksum", "restored from an older snapshot"),
            "Keycloak's import checksum is not the recorded one",
        ),
    ],
    ids=["realm file", "master file", "entrypoint", "config-cli definition", "database restored"],
)
def test_each_input_that_changed_plans_the_step_and_names_it(
    world: World, change: Callable[[World], None], why: str
) -> None:
    """Cells 3, 5, 6 and 7, and the entrypoint config-cli binds."""
    world.applied_once()

    change(world)

    assert world.plan().lines == [f"realm: apply keycloak/*.yaml ({why})"]


def test_an_apply_that_did_not_finish_is_planned_again(world: World) -> None:
    world.record.mark_unfinished([reconcile.CONFIG_CLI], {reconcile.CONFIG_CLI})

    assert "the last realm apply did not finish" in world.plan().line


def test_an_unreadable_record_plans_the_step(world: World) -> None:
    world.record.path.parent.mkdir(parents=True)
    world.record.path.write_text("not json")

    assert "is malformed" in world.plan().line


@pytest.mark.parametrize("gate", [core.Gate.OFF, core.Gate.SWITCHED_OFF])
def test_a_changed_file_while_keycloak_is_off_is_one_line_and_reads_nothing(
    world: World, gate: core.Gate
) -> None:
    """Cells 14 and 15: no step, Keycloak is not asked."""
    plan = world.plan(gate)

    assert plan.action is RealmAction.HELD
    assert plan.settled
    assert plan.lines == [
        f"realm: no realm apply is recorded; Keycloak is {gate}, so it is not applied"
    ]
    assert world.events == []


def test_unchanged_files_while_keycloak_is_off_say_nothing(world: World) -> None:
    world.applied_once()

    assert world.plan(core.Gate.OFF).lines == []


def test_keycloak_starting_in_this_run_is_not_checked_and_one_line_says_so(world: World) -> None:
    plan = world.plan(starts_now=True)

    assert (plan.action, plan.settled) == (RealmAction.NOT_CHECKED, True)
    assert plan.lines == ["realm: not checked, Keycloak starts now; the next `hal apply` checks it"]


def test_a_catalog_without_keycloak_plans_nothing(world: World) -> None:
    assert world.plan(None) == RealmPlan()


def test_a_keycloak_that_cannot_be_read_is_planned_as_a_partial_step(world: World) -> None:
    world.failing.add("checksums")

    plan = world.plan()

    assert (plan.action, plan.settled) == (RealmAction.UNREADABLE, False)
    assert plan.line == "realm: Keycloak could not be read; not checked (checksums broke)"
    assert world.run(plan).failure == plan.line
    assert "backup" not in world.events


# --- What the step does ------------------------------------------------------------------------


def test_the_step_backs_up_proves_observes_applies_observes_and_lists_the_changes(
    world: World,
) -> None:
    """Cell 3: the order, and the lines equal what changed."""
    world.after = inventory(group("apps"), group("operators", members=2))

    result = world.step()

    assert world.events == [
        "backup", "restore test", "observe", "config-cli", "observe", "checksums",
    ]  # fmt: skip
    assert result.failure is None
    assert result.lines == (
        f"realm: applied keycloak/*.yaml; pre-apply snapshot {SNAPSHOT} "
        "(0s backup, 0s restore test)",
        "hallucinai: addition group operators (2 members)",
    )


def test_an_apply_that_changes_nothing_says_so(world: World) -> None:
    assert world.step().lines[1:] == ("realm: no changes",)


def test_a_removal_is_printed_as_reset_to_the_file_with_the_snapshot(world: World) -> None:
    """Cell 13 (R4): a console change under a declared object, reset by the apply."""
    world.before = inventory(group("apps"), group("console-made"))

    result = world.step()

    assert result.failure is None
    assert result.lines[1:] == (
        "hallucinai: removal group console-made (0 members): reset to the file, "
        f"pre-apply snapshot {SNAPSHOT}",
    )


def test_a_failed_backup_applies_nothing(world: World) -> None:
    """Cell 8."""
    world.failing.add("backup")

    result = world.step()

    assert world.events == ["backup"]
    assert result.failure == "realm: backup broke; nothing applied"


def test_a_failed_restore_test_applies_nothing_and_names_the_snapshot_as_unproven(
    world: World,
) -> None:
    """Cell 9."""
    world.failing.add("restore test")

    result = world.step()

    assert "config-cli" not in world.events
    assert result.failure == (
        f"realm: restore test broke; nothing applied; pre-apply snapshot {SNAPSHOT} is unproven"
    )


def test_an_observer_that_fails_before_applies_nothing(world: World) -> None:
    """Cell 10."""
    world.failing.add("observe")

    result = world.step()

    assert "config-cli" not in world.events
    assert result.failure == (
        f"realm: observe broke; nothing applied; pre-apply snapshot {SNAPSHOT} is proven"
    )


def test_a_failed_config_cli_is_partial_names_the_way_back_and_is_planned_again(
    world: World,
) -> None:
    """Cell 11."""
    world.failing.add("config-cli")

    result = world.step()

    assert result.failure == (
        "realm: config-cli broke; the realm may be partly applied; "
        f'pre-apply snapshot {SNAPSHOT}, the way back is docs/backup.md "Restore Keycloak"; '
        "the next run repeats the step"
    )
    assert world.plan().action is RealmAction.RUN


def test_a_checksum_read_that_fails_after_config_cli_is_partial_and_not_recorded(
    world: World,
) -> None:
    """Cell 12."""
    world.failing.add("checksums")

    result = world.step()

    assert "the realm was applied but not observed" in (result.failure or "")
    assert SNAPSHOT in (result.failure or "")
    world.failing.clear()
    assert world.plan().action is RealmAction.RUN


def test_a_marker_set_since_the_plan_keeps_config_cli_from_running(world: World) -> None:
    """Cell 16: Keycloak is switched off between the plan and the step."""
    plan = world.plan()
    world.marks.record(reconcile.REALM_UNIT, datetime(2026, 10, 21, tzinfo=UTC))

    result = world.run(plan, mark=None)

    assert "config-cli" not in world.events
    assert result.failure == (
        f"realm: {reconcile.REALM_UNIT} was switched off meanwhile; config-cli is not run; "
        f"nothing applied; pre-apply snapshot {SNAPSHOT} is proven"
    )


def test_an_interrupt_during_the_backup_applies_nothing(world: World) -> None:
    """Cell 19."""
    world.interrupted.add("backup")

    result = world.step()

    assert result.failure == "realm: interrupted; nothing applied"
    assert "config-cli" not in world.events


def test_an_interrupt_during_config_cli_names_the_snapshot_and_the_realm_may_be_partial(
    world: World,
) -> None:
    """Cell 20."""
    world.interrupted.add("config-cli")

    result = world.step()

    assert "interrupted; the realm may be partly applied" in (result.failure or "")
    assert SNAPSHOT in (result.failure or "")
    assert world.plan().action is RealmAction.RUN


def test_a_config_cli_container_still_running_refuses_the_step_before_the_backup(
    world: World,
) -> None:
    """Cell 20: the one-off container of an interrupted run."""
    world.config_cli_running = True

    result = world.step()

    assert world.events == []
    assert result.failure == "realm: a config-cli container is still running; nothing applied"


def test_the_record_is_emptied_before_config_cli_and_filled_after_the_observe(
    world: World,
) -> None:
    def while_config_cli_runs() -> None:
        assert world.record.read().entries == {reconcile.CONFIG_CLI: {}}

    world.meanwhile = while_config_cli_runs

    world.step()

    entry = world.record.read().entries[reconcile.CONFIG_CLI]
    assert entry["keycloak/realm.yaml"]
    assert entry["definition"]
    assert entry[f"hallucinai {ATTRIBUTE}"] == "checksum one"


# --- The observer and edge-backup as the step calls them ---------------------------------------


@dataclass
class RecordedCompose:
    answer: str
    calls: list[tuple[str, list[str], str]] = field(default_factory=list)
    error: compose.ComposeError | None = None

    def run_once(self, service: str, arguments: list[str], stdin: str = "") -> str:
        self.calls.append((service, arguments, stdin))
        if self.error:
            raise self.error
        return self.answer


def test_the_observer_reads_checksums_with_no_key_and_inventories_under_one_key() -> None:
    """The two inventories a step compares are digested under the same key; the checksums need
    none."""
    engine = RecordedCompose('{"master": {}}')
    observer = reconcile.Observer(engine)

    observer.checksums()
    observer.inventory()
    observer.inventory()

    (_, checksum_arguments, checksum_input), (_, arguments, first_key), (_, _, second_key) = (
        engine.calls
    )
    assert (checksum_arguments, checksum_input) == (["--import-checksums"], "")
    assert arguments == []
    assert len(first_key.strip()) >= 64
    assert first_key == second_key


@pytest.mark.parametrize(
    ("engine", "message"),
    [
        (
            RecordedCompose("", error=compose.ComposeError("exited 1: realm-observer: a/b")),
            "the realm observer failed (exited 1: realm-observer: a/b)",
        ),
        (RecordedCompose("<html>"), "the realm observer answered no JSON"),
    ],
    ids=["compose failure", "not JSON"],
)
def test_an_observer_that_fails_or_answers_nonsense_is_one_line(
    engine: RecordedCompose, message: str
) -> None:
    observer = reconcile.Observer(engine)

    with pytest.raises(reconcile.RealmError) as raised:
        observer.checksums()

    assert str(raised.value) == message


@dataclass
class Commands:
    """edge-backup as a fake process: the commands asked, and what it answers."""

    stdout: str = f"edge-backup: pre-apply-Snapshot {SNAPSHOT}\n"
    stderr: str = ""
    code: int = 0
    asked: list[list[str]] = field(default_factory=list)

    def __call__(
        self, command: list[str], _timeout: int, _stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.asked.append(command)
        return subprocess.CompletedProcess(command, self.code, self.stdout, self.stderr)


def test_the_backup_asks_for_keycloak_alone_and_reads_the_snapshot_id() -> None:
    commands = Commands()
    backup = reconcile.Backup(("edge-backup", "--apps", "apps.toml"), commands)

    snapshot = backup.pre_apply()
    backup.restore_test(snapshot)

    assert snapshot == SNAPSHOT
    assert commands.asked == [
        ["edge-backup", "--apps", "apps.toml", "backup", "keycloak", "--pre-apply"],
        ["edge-backup", "--apps", "apps.toml", "restore-test", "keycloak", "--snapshot", SNAPSHOT],
    ]


def test_a_failing_backup_says_the_last_line_edge_backup_wrote() -> None:
    commands = Commands(stdout="", stderr="edge-backup: fehlgeschlagen: kein Passwort\n", code=1)

    backup = reconcile.Backup(("edge-backup",), commands)

    with pytest.raises(reconcile.RealmError) as raised:
        backup.pre_apply()

    assert str(raised.value) == (
        "the Keycloak backup failed (edge-backup: fehlgeschlagen: kein Passwort)"
    )


def test_a_backup_that_names_no_snapshot_is_a_failure() -> None:
    backup = reconcile.Backup(("edge-backup",), Commands(stdout="edge-backup: fertig\n"))

    with pytest.raises(reconcile.RealmError, match="named no pre-apply snapshot"):
        backup.pre_apply()


def test_the_installed_backup_is_the_checkouts_own_edge_backup(tmp_path: Path) -> None:
    backup = reconcile.Backup.installed(tmp_path)

    assert backup.command == (str(tmp_path / ".venv" / "bin" / "edge-backup"),)


def test_the_entry_of_a_finished_step_survives_a_json_round_trip(world: World) -> None:
    world.step()

    on_disk = json.loads(world.record.path.read_text())

    assert set(on_disk[reconcile.CONFIG_CLI]) == {
        "keycloak/entrypoint.sh",
        "keycloak/master.yaml",
        "keycloak/realm.yaml",
        "definition",
        f"hallucinai {ATTRIBUTE}",
    }


# --- What the step leaves alone, and what it says when a part breaks ---------------------------


def test_a_settled_plan_makes_no_backup_no_config_cli_and_leaves_the_record_bytes_unchanged(
    world: World,
) -> None:
    world.applied_once()
    recorded = world.record.path.read_bytes()
    plan = world.plan()
    world.events.clear()

    result = world.run(plan)

    assert (result, world.events) == (reconcile.StepResult(), [])
    assert world.record.path.read_bytes() == recorded


def test_a_bind_resolution_that_fails_is_a_partial_line_not_a_traceback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = world.plan()
    world.events.clear()

    def refuse(*_: object) -> frozenset[str]:
        raise core.CoreError("cannot read keycloak (Permission denied)")

    monkeypatch.setattr(core, "followed_services", refuse)

    result = world.run(plan)

    assert result.failure == ("realm: cannot read keycloak (Permission denied); nothing applied")
    assert world.events == []


def test_a_record_that_cannot_be_written_keeps_the_change_lines_and_says_it_repeats(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.after = inventory(group("apps"), group("operators", members=2))

    def refuse(*_: object) -> None:
        raise core.CoreError("cannot write applied.json (Permission denied)")

    monkeypatch.setattr(core.Record, "write", refuse)

    result = world.step()

    assert "hallucinai: addition group operators (2 members)" in result.lines
    assert result.failure == (
        "realm: applied and observed, but the record could not be written "
        "(cannot write applied.json (Permission denied)); the next run repeats the step"
    )


# --- edge-backup is stopped, not killed --------------------------------------------------------

WAITS = "while :; do sleep 0.1; done"
# A Python child, as the CI shell may have SIGTERM ignored on entry, which a shell cannot trap.
# It says it is ready; on SIGTERM it says it is cleaning up, holds until `argv[2]` exists (at most
# 30 seconds, so it cannot outlive a failed test), then writes its marker.
CLEANS_UP_ON_SIGTERM = """
import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
release = pathlib.Path(sys.argv[2])
def clean_up(*_):
    pathlib.Path(f"{marker}.cleaning").touch()
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    marker.write_text("cleaned\\n")
    sys.exit(143)
signal.signal(signal.SIGTERM, clean_up)
pathlib.Path(f"{marker}.pid").write_text(str(__import__("os").getpid()))
pathlib.Path(f"{marker}.ready").touch()
while True:
    time.sleep(0.1)
"""
IGNORES_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.1)
"""
DRIVES_THE_STEP = """
import pathlib, signal, sys
seen = pathlib.Path(sys.argv[1])
def interrupted(*_):
    with seen.open("a") as file:
        file.write("interrupted\\n")
    raise KeyboardInterrupt
signal.signal(signal.SIGINT, interrupted)
from edge.realm import reconcile
try:
    reconcile.run_edge_backup(sys.argv[2:], 600)
except KeyboardInterrupt:
    print("KeyboardInterrupt reached the step")
"""


def test_a_backup_that_runs_past_its_timeout_gets_sigterm_and_time_to_clean_up(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cleaned"
    release = tmp_path / "release"
    release.touch()

    with pytest.raises(compose.ComposeError, match="took longer than 3s"):
        reconcile.run_edge_backup(
            [sys.executable, "-c", CLEANS_UP_ON_SIGTERM, str(marker), str(release)], 3
        )

    assert marker.read_text() == "cleaned\n"


def test_a_backup_that_ignores_sigterm_still_ends_in_the_timeout_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile, "STOP_GRACE_SECONDS", 0.3)

    with pytest.raises(compose.ComposeError, match="took longer than 3s"):
        reconcile.run_edge_backup([sys.executable, "-c", IGNORES_SIGTERM], 3)


def test_a_grandchild_that_holds_the_output_open_cannot_hang_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reconcile, "STOP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(reconcile, "COLLECT_SECONDS", 0.5)
    escaped = tmp_path / "escaped.pid"
    leaves_a_grandchild = f"setsid sh -c 'echo $$ > {escaped}; exec sleep 60' & {WAITS}"

    try:
        started = time.monotonic()
        with pytest.raises(compose.ComposeError, match="took longer than 1s"):
            reconcile.run_edge_backup(["sh", "-c", leaves_a_grandchild], 1)
        assert time.monotonic() - started < 10
    finally:
        os.kill(int(escaped.read_text()), signal.SIGKILL)


def wait_for(condition: Callable[[], bool], what: str, step: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        if step.poll() is not None:
            pytest.fail(
                f"the step ended before {what}: {step.stdout.read() if step.stdout else ''}"
            )
        if time.monotonic() > deadline:
            pytest.fail(f"no {what} within 30 seconds")
        time.sleep(0.02)


def kill_group(process_group: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)


def test_ctrl_c_during_the_grace_does_not_cut_the_clean_up_short(tmp_path: Path) -> None:
    """The terminal's Ctrl-C goes to the step's group alone: edge-backup runs in its own."""
    marker = tmp_path / "cleaned"
    release = tmp_path / "release"
    interrupts = tmp_path / "interrupts"
    command = [
        sys.executable,
        "-c",
        DRIVES_THE_STEP,
        str(interrupts),
        *(sys.executable, "-c", CLEANS_UP_ON_SIGTERM, str(marker), str(release)),
    ]
    step = subprocess.Popen(
        command,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY / "src")},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_for(Path(f"{marker}.ready").exists, "the backup was ready", step)
        os.killpg(step.pid, signal.SIGINT)  # stops edge-backup
        wait_for(Path(f"{marker}.cleaning").exists, "the backup began its clean-up", step)
        os.killpg(step.pid, signal.SIGINT)  # comes while the clean-up is held
        wait_for(
            lambda: interrupts.exists() and interrupts.read_text().count("\n") == 2,
            "the second interrupt",
            step,
        )
        release.touch()
        output, _ = step.communicate(timeout=30)
    finally:
        kill_group(step.pid)
        if Path(f"{marker}.pid").exists():
            kill_group(int(Path(f"{marker}.pid").read_text()))
        step.wait(timeout=30)

    assert marker.read_text() == "cleaned\n"
    assert "KeyboardInterrupt reached the step" in output


def test_a_finished_backup_returns_its_exit_code_and_output() -> None:
    result = reconcile.run_edge_backup(["sh", "-c", "echo out; echo err >&2; exit 3"], 30)

    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_config_cli_of_the_real_compose_yaml_is_followed_and_its_bound_files_are_listed() -> None:
    """compose.yaml's own binds of keycloak-config-cli: edge follows it, and its bound files list
    the entrypoint beside the two realm files its directory bind carries."""
    document = yaml.safe_load((REPOSITORY / "compose.yaml").read_text())
    mounts = document["services"][reconcile.CONFIG_CLI]["volumes"]
    binds = [{"type": "bind", "source": str(REPOSITORY / mount.split(":")[0])} for mount in mounts]
    config = {"services": {reconcile.CONFIG_CLI: {"volumes": binds}}}

    files = reconcile.bound_files(config, REPOSITORY)

    assert core.followed_services(config, REPOSITORY) == {reconcile.CONFIG_CLI}
    assert [path.relative_to(REPOSITORY).as_posix() for path in files] == [
        "keycloak/master.yaml",
        "keycloak/realm.yaml",
        "keycloak/entrypoint.sh",
    ]
