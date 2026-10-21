"""The realm step of `edge apply --closed` (#242): when it runs (the plan) and what it does (the
step), with Compose, the observer and edge-backup faked. Each test names its cell of the item's
scenario matrix."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from edge import compose, core, switches
from edge.realm import reconcile
from edge.realm.reconcile import RealmAction, RealmPlan

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
        self.record = core.Record(self.root / "state")
        self.marks = switches.Switches(self.root / "state")

    def write_file(self, name: str, text: str) -> None:
        (self.root / "keycloak" / name).write_text(text)

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

    def restore_test(self) -> None:
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
        return reconcile.plan_realm(self, self, self.record, self.root, gate, starts_now)

    def run(self, plan: RealmPlan, mark: switches.Mark | None = None) -> reconcile.StepResult:
        realm = reconcile.Realm(
            self,
            self,
            self,
            self.record,
            frozenset({reconcile.CONFIG_CLI}),
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
            lambda world: setattr(world, "definition", "definition two"),
            "config-cli's definition changed",
        ),
        (
            lambda world: setattr(world, "checksum", "restored from an older snapshot"),
            "Keycloak's import checksum is not the recorded one",
        ),
    ],
    ids=["realm file", "master file", "config-cli definition", "database restored"],
)
def test_each_input_that_changed_plans_the_step_and_names_it(
    world: World, change: Callable[[World], None], why: str
) -> None:
    """Cells 3, 5, 6 and 7."""
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


def test_a_failed_config_cli_is_partial_names_the_way_back_and_runs_again(world: World) -> None:
    """Cell 11."""
    world.failing.add("config-cli")

    result = world.step()

    assert result.failure == (
        "realm: config-cli broke; the realm may be partly applied; "
        f'pre-apply snapshot {SNAPSHOT}, the way back is docs/backup.md "Restore Keycloak"; '
        "the next run repeats the step"
    )
    assert world.plan().action is RealmAction.RUN


def test_an_observer_that_fails_after_is_partial_and_the_step_is_not_recorded(
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
    backup.restore_test()

    assert snapshot == SNAPSHOT
    assert commands.asked == [
        ["edge-backup", "--apps", "apps.toml", "backup", "keycloak", "--pre-apply"],
        ["edge-backup", "--apps", "apps.toml", "restore-test", "keycloak"],
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
        "keycloak/master.yaml",
        "keycloak/realm.yaml",
        "definition",
        f"hallucinai {ATTRIBUTE}",
    }
