"""What `hal apply` plans for an app and how it runs the plan (#255 A1-A3, A8-A12, A16-A19, A22,
A23), against a real origin and a scripted Docker: the checkout, the tags and the record are real,
the containers are a table."""

import dataclasses
import json
import stat
import subprocess
from pathlib import Path

import pytest
from app_support import (
    COMPOSE,
    FLEET,
    KEY,
    NOW,
    SENTINEL,
    Clock,
    Origin,
    ScriptedDocker,
    origin_at,
    runtime_of,
)
from git_support import head_of, plain_git

from edge import apps, compose, switches
from edge.unit_plans import Fleet, Progress

EDGE_FLEET = Fleet({"name": "edge"}, {}, ())

FIRST, SECOND, THIRD = "v2026.10.22.1", "v2026.10.22.2", "v2026.10.22.3"
TWO_SERVICES = (
    "services:\n  web:\n    build: .\n    restart: unless-stopped\n"
    "  worker:\n    build: .\n    restart: unless-stopped\n"
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LC_ALL", "C")
    return home


@pytest.fixture
def origin(tmp_path: Path, home: Path) -> Origin:
    return origin_at(tmp_path, home)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    tree = tmp_path / "edge"
    tree.mkdir()
    (tree / "fleet.yaml").write_text(FLEET)
    return tree


@pytest.fixture
def docker(home: Path, state: Path) -> ScriptedDocker:
    return ScriptedDocker(state, home / "srv" / KEY)


@pytest.fixture
def runtime(home: Path, state: Path, docker: ScriptedDocker) -> apps.AppRuntime:
    return runtime_of(home, state, docker)


@dataclasses.dataclass
class World:
    repository: Path
    state: Path
    srv: Path
    runtime: apps.AppRuntime
    docker: ScriptedDocker

    def plan(self) -> apps.AppsPlan:
        return apps.plan_apps(self.repository, EDGE_FLEET, self.runtime, self.marks)

    def run(self, plan: apps.AppsPlan | None = None) -> list[str | None]:
        """The failure line of each app's run; None where it is on and recorded."""
        planned = plan or self.plan()
        return [failure for _, failure in planned.run(self.marks, self.progress)]

    def deployed(self) -> dict[str, str] | None:
        record = self.state / "deployed" / KEY
        return json.loads(record.read_text()) if record.exists() else None

    marks: switches.Switches = dataclasses.field(init=False)
    progress: Progress = dataclasses.field(default_factory=Progress)

    def __post_init__(self) -> None:
        self.marks = switches.Switches(self.state)


@pytest.fixture
def world(repository, state, home, runtime, docker) -> World:
    return World(repository, state, home / "srv" / KEY, runtime, docker)


def deploy(world: World, origin: Origin, tag: str, files: dict[str, str] | None = None) -> str:
    """The app switched on at a new release `tag`, healthy and recorded; its sha."""
    sha = origin.release(tag, files)
    assert world.run() == [None]
    assert world.deployed()["sha"] == sha
    return sha


def test_a_first_apply_plans_tag_and_full_sha_then_runs_and_records_after_health(world, origin):
    sha = origin.release(FIRST)

    plan = world.plan()

    assert plan.describe() == [f"hopin: on at {FIRST} ({sha}) from origin master, into ~/srv/hopin"]
    assert world.deployed() is None
    assert world.run(plan) == [None]
    assert world.deployed() == {"tag": FIRST, "sha": sha, "time": NOW.isoformat()}
    assert stat.S_IMODE((world.state / "deployed" / KEY).stat().st_mode) == 0o600
    assert head_of(world.srv) == sha
    assert world.docker.verbs == [
        "ps", "build", "pull", "config", "image inspect", "create", "start", "ps",
    ]  # fmt: skip


def test_only_create_runs_under_the_switch_lock_and_nothing_else_does(world, origin):
    origin.release(FIRST)

    world.run()

    assert world.docker.locked_verbs() == {"create"}


def test_a_second_apply_changes_nothing_and_builds_creates_and_starts_nothing(world, origin):
    deploy(world, origin, FIRST)
    world.docker.calls.clear()

    plan = world.plan()

    assert plan.changes_nothing
    assert plan.describe() == []
    assert world.docker.verbs == ["ps"]


def test_a_newer_release_is_an_update_that_names_the_last_successful_deployment(world, origin):
    deploy(world, origin, FIRST)
    sha = origin.release(SECOND)

    plan = world.plan()

    assert plan.describe() == [
        f"hopin: update to {SECOND} ({sha}); last successful deployment {FIRST}; "
        "now: hopin running (healthy)"
    ]
    assert world.run(plan) == [None]
    assert world.deployed()["tag"] == SECOND


def test_a_release_on_a_side_branch_is_named_and_the_newest_stays(world, origin):
    deploy(world, origin, SECOND)
    plain_git(origin.work, "switch", "--quiet", "-c", "side")
    origin.commit({"side": "1"})
    origin.tag("v2026.10.22.9")

    plan = world.plan()

    assert plan.changes_nothing
    assert plan.notes == ("hopin: skipped tag v2026.10.22.9: not on origin master",)


def test_an_off_grammar_tag_is_named_in_the_plan(world, origin):
    origin.release(FIRST)
    origin.tag("v2027.1.5.1")

    plan = world.plan()

    assert plan.notes == ("hopin: skipped tag v2027.1.5.1: not vYYYY.MM.DD.N",)
    assert plan.plans[0].release.tag == FIRST


def test_without_a_release_the_plan_is_refused_asking_for_the_first_one_and_writes_only_git(
    world, origin
):
    with pytest.raises(apps.AppError, match=r"no vYYYY.MM.DD.N release on origin master; .*first"):
        world.plan()

    assert [path.name for path in world.srv.iterdir()] == [".git"]


def test_a_new_tag_that_fails_health_stays_partial_with_the_record_on_the_old_one(world, origin):
    deploy(world, origin, SECOND)
    sha = origin.release(THIRD)
    world.docker.health_after_start = "unhealthy"

    [failure] = world.run()

    assert failure == (
        f"hopin is not healthy at {THIRD} after 300s (hopin running (unhealthy)); "
        "the record stays, the fix is a newer tag"
    )
    assert world.deployed()["tag"] == SECOND
    assert world.plan().describe() == [
        f"hopin: update to {THIRD} ({sha}); last successful deployment {SECOND}; "
        f"the update to {THIRD} did not finish; now: hopin running (unhealthy)"
    ]


def test_a_build_that_fails_leaves_the_old_containers_and_the_next_plan_says_unfinished(
    world, origin
):
    deploy(world, origin, FIRST)
    sha = origin.release(SECOND)
    world.docker.failing_verb = "build"

    [failure] = world.run()
    world.docker.failing_verb = None

    assert failure == (
        f"hopin: could not build at {SECOND}, `docker compose build` of hopin at {SECOND} "
        "exited 3; its output is withheld, run the step in the app's checkout to read it"
    )
    assert world.deployed()["tag"] == FIRST
    assert world.docker.rows[0]["Name"] == "hopin-hopin-1"
    assert world.plan().describe() == [
        f"hopin: update to {SECOND} ({sha}); last successful deployment {FIRST}; "
        f"the update to {SECOND} did not finish; now: hopin running (healthy)"
    ]


@pytest.mark.parametrize("verb", ["build", "pull", "create", "start"])
def test_no_word_of_composes_output_reaches_a_failure_line(world, origin, verb):
    origin.release(FIRST)
    world.docker.failing_verb = verb

    [failure] = world.run()

    assert SENTINEL not in failure
    assert f"could not {verb}" in failure
    assert f"at {FIRST}" in failure
    assert "exited 3" in failure


def test_no_word_of_composes_output_reaches_the_plan_when_ps_fails(world, origin):
    origin.release(FIRST)
    world.docker.failing_verb = "ps"

    with pytest.raises(compose.ComposeError) as failed:
        world.plan()

    assert SENTINEL not in str(failed.value)
    assert "exited 3" in str(failed.value)


@pytest.mark.parametrize(
    ("verb", "step"),
    [
        ("build", f"build at {SECOND}"),
        ("create", "create the containers"),
        ("start", "start at"),
    ],
)
def test_an_interrupt_names_the_app_and_step_and_leaves_the_record_and_the_next_plan_unfinished(
    world, origin, verb, step
):
    deploy(world, origin, FIRST)
    sha = origin.release(SECOND)
    world.docker.interrupted_verb = verb

    with pytest.raises(KeyboardInterrupt):
        world.run()
    world.docker.interrupted_verb = None

    assert world.progress.unit == KEY
    assert step in world.progress.step
    assert ("partly replaced" in world.progress.step) == (verb != "build")
    assert world.deployed()["tag"] == FIRST
    assert f"the update to {SECOND} did not finish" in world.plan().describe()[0]
    assert head_of(world.srv) == sha


def test_an_interrupt_during_the_health_wait_names_it_and_records_nothing(
    world, origin, docker, home, state
):
    deploy(world, origin, FIRST)
    origin.release(SECOND)
    clock = Clock()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    docker.health_after_start = "starting"
    world.runtime = dataclasses.replace(
        world.runtime, health=compose.Watch(clock.read, interrupted, 300)
    )

    with pytest.raises(KeyboardInterrupt):
        world.run()

    assert world.progress.step == f"wait for {KEY} to be healthy"
    assert world.deployed()["tag"] == FIRST


@pytest.mark.parametrize(
    ("break_it", "now"),
    [
        (lambda docker: docker.rows[0].update(State="exited", Health=""), "exited"),
        (lambda docker: docker.rows[0].update(Health="unhealthy"), "running (unhealthy)"),
    ],
    ids=["stopped by hand", "unhealthy"],
)
def test_the_newest_release_recorded_and_checked_out_but_not_running_well_starts_again(
    world, origin, break_it, now
):
    sha = deploy(world, origin, FIRST)
    break_it(world.docker)

    assert world.plan().describe() == [
        f"hopin: start again at {FIRST} ({sha}, recorded); now: hopin {now}"
    ]


def test_a_container_name_that_compose_interpolates_never_reaches_a_plan_or_a_failure(
    world, origin, monkeypatch
):
    resolved = "hal-test-sentinel-value"
    monkeypatch.setenv("SENTINEL", resolved)
    named = COMPOSE + "    container_name: '${SENTINEL}'\n"
    create_unnamed = world.docker.verb_create

    def create_named(arguments: list[str]) -> str:
        create_unnamed(arguments)
        for row in world.docker.rows:
            row["Name"] = resolved
        return ""

    world.docker.verb_create = create_named
    sha = deploy(world, origin, FIRST, {"compose.yaml": named})
    world.docker.rows[0]["State"] = "exited"
    start_again = world.plan().describe()

    assert world.docker.rows[0]["Name"] == resolved
    assert start_again[0].startswith(f"hopin: start again at {FIRST} ({sha}, recorded)")

    origin.release(SECOND, {"compose.yaml": named})
    update = world.plan().describe()

    assert update[0].startswith(f"hopin: update to {SECOND}")

    world.docker.health_after_start = "unhealthy"
    [failure] = world.run()

    assert world.docker.rows[0]["Name"] == resolved
    assert "hopin running (unhealthy)" in failure
    assert resolved not in " ".join([*start_again, *update, failure])


@pytest.mark.parametrize(
    "arrange",
    [
        lambda docker: docker.put(KEY, "running", "healthy"),
        lambda docker: docker.put("orphan", "running", "healthy"),
    ],
    ids=["a second container of a service", "a container of a service the file lacks"],
)
def test_one_container_per_service_and_no_orphan_is_needed_for_nothing_to_change(
    world, origin, arrange
):
    sha = deploy(world, origin, FIRST)
    arrange(world.docker)

    [line] = world.plan().describe()

    assert line.startswith(f"hopin: start again at {FIRST} ({sha}, recorded)")


def test_running_containers_without_a_record_are_replaced_and_their_volumes_kept(world, origin):
    sha = origin.release(FIRST)
    world.docker.put(KEY, "running", "healthy")

    assert world.plan().describe() == [
        f"hopin: on at {FIRST} ({sha}) from origin master, into ~/srv/hopin, "
        "replacing the running containers; named volumes kept"
    ]


def test_a_dirty_checkout_cannot_be_planned_and_is_not_reset(world, origin):
    deploy(world, origin, FIRST)
    (world.srv / "stray").write_text("mine")

    with pytest.raises(
        apps.AppError, match=r"services.hopin: ~/srv/hopin has local changes.*stray"
    ):
        world.plan()

    assert (world.srv / "stray").read_text() == "mine"


def test_a_regular_file_at_the_checkout_cannot_be_planned_with_one_line(world, origin):
    origin.release(FIRST)
    world.srv.parent.mkdir(parents=True, exist_ok=True)
    world.srv.write_text("mine")

    with pytest.raises(apps.AppError, match=r"services.hopin: ~/srv/hopin cannot be made"):
        world.plan()


def test_a_tag_moved_on_origin_against_the_record_cannot_be_planned_naming_both_shas(world, origin):
    recorded = deploy(world, origin, FIRST)
    moved = origin.commit({"other": "1"})
    origin.push("master")
    origin.tag(FIRST)

    with pytest.raises(apps.AppError) as refused:
        world.plan()

    assert str(refused.value) == (
        f"services.hopin: tag {FIRST} moved on origin; deployed {recorded}, origin now {moved}"
    )


def test_an_origin_that_cannot_be_reached_cannot_be_planned_with_gits_last_line(
    world, origin, home
):
    origin.release(FIRST)
    (home / ".gitconfig").write_text(
        '[url "file:///nowhere.git"]\n\tinsteadOf = https://github.com/\n'
    )

    with pytest.raises(apps.AppError, match=r"services.hopin: `git fetch` exited 128: fatal:"):
        world.plan()


def test_a_disabled_app_is_not_fetched_and_has_nothing_to_change(world, repository):
    (repository / "fleet.yaml").write_text(FLEET.replace("enabled: true", "enabled: false"))
    asked = []
    world.runtime = dataclasses.replace(
        world.runtime, git=lambda *call: asked.append(call) or pytest.fail("git was asked")
    )

    plan = world.plan()

    assert plan.changes_nothing
    assert asked == []
    assert world.docker.verbs == ["ps"]
    assert not world.srv.exists()


class OffDocker:
    """What the off step asks of Docker: the project's `ps` from the scripted table, its volume
    listing, and the file-free `stop` and `rm -f`, which empty the table. Every other call goes to
    the scripted Docker, which fails the test on a `down`, a `-v` or a file-bound call."""

    def __init__(self, scripted: ScriptedDocker, volumes: dict[str, str]) -> None:
        self.scripted, self.volumes = scripted, volumes  # volume name -> its key in the file
        self.commands: list[list[str]] = []
        self.failing: str | None = None
        self.removal_by_identifier_works = True
        self.removal_by_identifier_exit = 0
        self.survivors: set[str] = set()  # services whose containers only `docker rm` removes

    def run(self, command, timeout, stdin=None):
        self.commands.append(command)
        if command[:3] == ["docker", "volume", "ls"]:
            shown = self.volumes.values() if "Label" in command[-1] else self.volumes
            return subprocess.CompletedProcess(command, 0, "\n".join(shown), "")
        if command[:3] == ["docker", "rm", "-f"]:
            return self.remove_by_identifier(command)
        if command[4:5] in (["stop"], ["rm"]):
            assert command[4:] in (["stop"], ["rm", "-f"]), command
            if self.failing == command[4]:
                return subprocess.CompletedProcess(command, 3, SENTINEL, SENTINEL)
            if command[4] == "rm":
                self.scripted.rows[:] = [
                    r for r in self.scripted.rows if r["Service"] in self.survivors
                ]
            return subprocess.CompletedProcess(command, 0, "", "")
        return self.scripted.run(command, timeout, stdin)

    def remove_by_identifier(self, command):
        assert "-v" not in command, command
        assert "--volumes" not in command, command
        listed = {row["ID"] for row in self.scripted.rows}
        assert command[3:], command
        assert set(command[3:]) <= listed, command
        if self.removal_by_identifier_exit:
            code = self.removal_by_identifier_exit
            return subprocess.CompletedProcess(command, code, SENTINEL, SENTINEL)
        if self.removal_by_identifier_works:
            self.scripted.rows[:] = [r for r in self.scripted.rows if r["ID"] not in command[3:]]
        return subprocess.CompletedProcess(command, 0, "", "")

    def compose_calls(self) -> list[list[str]]:
        return [command[4:] for command in self.commands if command[:2] == ["docker", "compose"]]


def switch_off(world: World) -> OffDocker:
    """The app disabled in the catalog while its project holds a healthy container, a leftover of
    a service the file does not name, and the named volume `hopin_data`."""
    (world.repository / "fleet.yaml").write_text(FLEET.replace("enabled: true", "enabled: false"))
    if not world.docker.rows:
        world.docker.put(KEY, "running", "healthy")
    world.docker.put("leftover", "exited")
    fake = OffDocker(world.docker, {f"{KEY}_data": "data"})
    world.runtime = dataclasses.replace(world.runtime, docker=fake.run)
    return fake


def test_an_app_switched_off_while_running_plans_one_line_naming_what_stays(world):
    switch_off(world)

    plan = world.plan()

    assert not plan.changes_nothing
    assert plan.describe() == [
        f"{KEY}: off (containers {KEY} running (healthy), leftover exited; "
        f"named volumes data stay; ~/srv/{KEY} stays)"
    ]


def test_off_stops_then_removes_every_container_of_the_project_and_nothing_else(world):
    off = switch_off(world)
    plan = world.plan()
    off.commands.clear()

    assert world.run(plan) == []

    assert off.commands == [
        ["docker", "compose", "-p", KEY, "stop"],
        ["docker", "compose", "-p", KEY, "rm", "-f"],
        ["docker", "compose", "-p", KEY, "ps", "--all", "--format", "json"],
    ]
    assert world.docker.rows == []
    assert world.plan().changes_nothing


def test_off_never_fetches_and_leaves_the_checkout_and_the_record(world, origin):
    sha = deploy(world, origin, FIRST)
    record = world.deployed()
    off = switch_off(world)
    world.runtime = dataclasses.replace(
        world.runtime, git=lambda *call: pytest.fail("git was asked")
    )

    assert world.run() == []

    assert [call[0] for call in off.compose_calls()[-3:]] == ["stop", "rm", "ps"]
    assert head_of(world.srv) == sha
    assert world.deployed() == record


@pytest.mark.parametrize("verb", ["stop", "rm"])
def test_a_failing_off_is_one_line_without_composes_output_and_stops_there(world, verb):
    off = switch_off(world)
    plan = world.plan()
    off.failing = verb

    [failure] = world.run(plan)

    assert f"`docker compose {verb}` of {KEY}" in failure
    assert SENTINEL not in failure
    assert ("rm" in {c[0] for c in off.compose_calls()}) == (verb == "rm")


@pytest.mark.parametrize(
    "record",
    ["not json", '{"tag": "v1", "sha": "abc", "time": "x"}', '{"tag": ' + f'"{FIRST}"' + "}"],
)
def test_a_record_that_is_no_deployment_cannot_be_planned(world, origin, record):
    origin.release(FIRST)
    (world.state / "deployed").mkdir(parents=True)
    (world.state / "deployed" / KEY).write_text(record)

    with pytest.raises(apps.AppError, match="is not a record of a deployment"):
        world.plan()


@pytest.mark.parametrize("second_web", [False, True], ids=["web only created", "web twice"])
def test_an_update_interrupted_after_part_of_the_containers_was_replaced_is_repaired(
    world, origin, docker, second_web
):
    deploy(world, origin, FIRST, {"compose.yaml": TWO_SERVICES})
    origin.release(SECOND, {"compose.yaml": TWO_SERVICES + "# second\n"})
    docker.interrupted_verb = "start"
    with pytest.raises(KeyboardInterrupt):
        world.run()
    docker.interrupted_verb = None
    docker.rows.clear()
    docker.put("web", "created")
    docker.put("worker", "running", "healthy")
    if second_web:
        docker.put("web", "exited")

    [line] = world.plan().describe()

    assert f"last successful deployment {FIRST}; the update to {SECOND} did not finish" in line
    assert all(row["Service"] in line for row in docker.rows)
    assert world.run() == [None]
    assert sorted(row["Service"] for row in docker.rows) == ["web", "worker"]
    assert world.deployed()["tag"] == SECOND


def test_a_contract_refusal_in_a_release_is_a_plan_refusal_before_anything_is_built(world, origin):
    origin.release(FIRST, {"compose.yaml": "services: {web: {image: x, profiles: [a]}}"})

    with pytest.raises(
        apps.AppError, match=r"compose\.yaml at v2026\.10\.22\.1: service web uses profiles"
    ):
        world.plan()

    assert world.docker.verbs == []


def test_a_record_that_cannot_be_read_cannot_be_planned(world, origin):
    origin.release(FIRST)
    (world.state / "deployed" / KEY).mkdir(parents=True)

    with pytest.raises(apps.AppError, match=r"services.hopin: cannot read .*\(Is a directory\)"):
        world.plan()


def test_a_record_that_cannot_be_written_ends_the_run_partial_after_health(world, origin):
    origin.release(FIRST)
    plan = world.plan()
    (world.state / "deployed").mkdir(parents=True)
    (world.state / "deployed" / KEY / "in the way").mkdir(parents=True)

    [failure] = world.run(plan)

    assert failure == f"hopin: could not record the deployment of {FIRST} (Is a directory)"


def test_the_default_runtime_records_with_the_clock_and_lives_under_home_srv(tmp_path):
    runtime = apps.AppRuntime.of(tmp_path / "home", tmp_path / "state")

    assert runtime.srv == tmp_path / "home" / "srv"
    assert runtime.now().utcoffset() is not None


PULL_ALWAYS = {"compose.yaml": COMPOSE.replace("build: .", "build: .\n    pull_policy: always")}
COVERED = {
    "compose.yaml": COMPOSE.replace("build: .", "build: .\n    volumes: ['data:/data']")
    + "volumes:\n  data: {}\n"
}


def test_an_image_volume_no_named_volume_covers_is_partial_before_create_with_the_record_kept(
    world, origin
):
    first = deploy(world, origin, FIRST)
    origin.release(SECOND)
    world.docker.image_volumes[KEY] = ["/data"]

    [failure] = world.run()

    assert failure == (
        f"services.hopin: image of service hopin at {SECOND} declares a volume that no named "
        "volume of the service covers (volumes); give it a named volume"
    )
    assert world.docker.verbs[-1] == "image inspect"
    assert world.deployed()["sha"] == first


def test_an_image_volume_a_named_volume_covers_starts(world, origin):
    world.docker.image_volumes[KEY] = ["/data"]
    origin.release(FIRST, COVERED)

    assert world.run() == [None]
    assert world.deployed()["tag"] == FIRST


def test_a_service_with_pull_policy_always_is_started_from_the_inspected_image(world, origin):
    origin.release(FIRST, PULL_ALWAYS)

    assert world.run() == [None]

    verbs = world.docker.verbs
    assert "pull" not in verbs[verbs.index("image inspect") :]
    create = next(arguments for verb, arguments, _ in world.docker.calls if verb == "create")
    assert create == ("--no-build", "--pull", "never", "--remove-orphans")


def test_off_removes_a_one_off_container_that_rm_leaves_by_its_identifier_and_never_a_volume(world):
    off = switch_off(world)
    world.docker.put("oneoff", "running")
    off.survivors.add("oneoff")
    plan = world.plan()
    off.commands.clear()

    assert world.run(plan) == []

    assert ["docker", "rm", "-f", "id3"] in off.commands
    assert world.docker.rows == []


def test_a_container_that_survives_even_docker_rm_ends_the_off_partial_naming_service_and_state(
    world,
):
    off = switch_off(world)
    world.docker.put("stuck", "running")
    off.survivors.add("stuck")
    plan = world.plan()
    off.removal_by_identifier_works = False

    [failure] = world.run(plan)

    assert failure == f"{KEY}: could not remove every container of {KEY}, left: stuck running"


def test_a_failing_docker_rm_ends_the_off_partial_naming_service_and_state_and_withholds_output(
    world,
):
    off = switch_off(world)
    world.docker.put("stuck", "running")
    off.survivors.add("stuck")
    plan = world.plan()
    off.removal_by_identifier_exit = 5

    [failure] = world.run(plan)

    assert failure.startswith(
        f"{KEY}: could not remove every container of {KEY}, left: stuck running"
    )
    assert "exited 5" in failure
    assert SENTINEL not in failure


def test_volumes_that_share_a_key_are_named_once_with_their_count(world):
    off = switch_off(world)
    off.volumes = {f"{KEY}_data": "data", SENTINEL: "data", f"{KEY}_cache": "cache"}

    [line] = world.plan().describe()

    assert "named volumes data (2), cache stay" in line
    assert SENTINEL not in line


def test_a_volume_name_that_compose_interpolates_never_reaches_the_plan(world):
    off = switch_off(world)
    off.volumes = {SENTINEL: "data"}

    [line] = world.plan().describe()

    assert SENTINEL not in line
    assert "named volumes data stay" in line
