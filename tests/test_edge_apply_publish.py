"""Plain `edge apply` (#171): the local part (`--closed`) and then the publish (`edge external
apply`), each with its own `yes`, driven through `cli.main` with both real phases.

The local part runs on the scripted boundaries of `test_edge_apply`, the publish on the fake
tofu, token files and live checkout of `test_edge_external`; one terminal holds the answers in
order. The catalog declares no `ready` host, so the publish's ready check asks nothing."""

import functools
import io
import signal
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from app_support import KEY, ScriptedDocker, origin_at, runtime_of
from test_edge_apply import (
    NOT_SET_UP_LINE,
    apply_lock_held,
    at_terminal,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    switch_off,
    trusted_environment,
)
from test_edge_external import (
    CHANGING_PLAN,
    CLEAN_SNAPSHOT,
    EMPTY_PLAN,
    FAILING_APPLY,
    STRAY_SNAPSHOT,
    FakeProber,
    FakeReader,
    commit_catalog,
    fake_tofu,  # noqa: F401 the fixture, requested by name below
    home,  # noqa: F401 the fixture, requested by name below
    live_checkout,  # noqa: F401 the fixture, requested by name below
    plain_git,
)

from edge import apply, cli, compose
from edge.external import cli as external_cli
from edge.external import cloudflare_read, tofu

REAL_APPLY_CLOSED = apply.apply_closed
REAL_PUBLISH = external_cli.apply_command
REAL_CHECK = external_cli.check_command
CATALOG = """\
cloudflare: {zone: example.test}
services:
  step-ca:
    run: {enabled: true, setup: step-ca/init}
  site:
    hosts:
      - {name: "@", exposure: public, reason: the site}
"""
SETUP_QUESTION = "Type 'yes' to run `step-ca/init` now"
PLAN_QUESTION = "Type 'yes' to apply exactly this plan"
NOTHING_PUBLISHED = "edge apply: nothing published"
INTERRUPTED_IN_PUBLISH = "interrupted during the publish; run `edge external check --hosts`"


@dataclass
class Result:
    exit_code: int
    out: str
    err: str


@dataclass
class World:
    """Plain `edge apply` over the scripted local part and the fake publish."""

    run: "functools.partial[Result]"
    settle_local: "functools.partial[None]"
    scripted: object
    scripted_git: object
    setup_check: object
    setup_run: object
    state: Path
    stack: ExitStack
    reader: FakeReader
    fake_tofu: object
    publish_entries: list[tuple] = field(default_factory=list)
    check_entries: list[None] = field(default_factory=list)

    def make_setup_missing(self) -> None:
        self.setup_check.exit_code, self.setup_check.line = 1, NOT_SET_UP_LINE

    def publish_never_began(self) -> bool:
        return not (self.publish_entries or self.check_entries or self.fake_tofu.calls())


@pytest.fixture
def repository(live_checkout, monkeypatch) -> Path:  # noqa: F811
    """`~/git/edge`: one real checkout that both parts read, the catalog committed."""
    step_ca_setup = live_checkout / "step-ca" / "init"
    step_ca_setup.parent.mkdir()
    step_ca_setup.write_text("#!/bin/sh\n")
    step_ca_setup.chmod(0o755)
    (live_checkout / "dynamic").mkdir()
    (live_checkout / "dynamic" / ".gitkeep").write_text("")
    (live_checkout / "traefik.yml").write_text(
        'entryPoints:\n  edge:\n    address: "127.0.0.1:8081"\n'
    )
    plain_git(live_checkout, "add", "--all")
    commit_catalog(live_checkout, CATALOG)
    monkeypatch.setattr(tofu, "PROJECT_ROOT", live_checkout)
    return live_checkout


@pytest.fixture
def world(
    repository, scripted, scripted_git, setup_check, setup_run, ingress, state, fake_tofu,  # noqa: F811
    at_terminal, capsys, monkeypatch,  # noqa: F811
):  # fmt: skip
    monkeypatch.setattr(
        apply,
        "apply_closed",
        functools.partial(
            REAL_APPLY_CLOSED,
            engine=compose.Compose(scripted.run, repository),
            repository=repository,
            check=setup_check,
            run_setup=setup_run,
            verifier=ingress.verifier,
            watch=ingress.watch,
            git=scripted_git,
            ensure_environment=trusted_environment,
        ),
    )
    reader = FakeReader(CLEAN_SNAPSHOT)
    made = World(
        None, None, scripted, scripted_git, setup_check, setup_run, state, ExitStack(),
        reader, fake_tofu,
    )  # fmt: skip

    def spy_on_publish(*arguments, **keywords):
        made.publish_entries.append((arguments, keywords))
        return REAL_PUBLISH(*arguments, **keywords)

    def spy_on_check(*arguments, **keywords):
        made.check_entries.append(None)
        return REAL_CHECK(*arguments, cloudflare=reader, prober=FakeProber(), **keywords)

    monkeypatch.setattr(external_cli, "apply_command", spy_on_publish)
    monkeypatch.setattr(external_cli, "check_command", spy_on_check)

    def run(*answers: str, arguments=("apply",), while_prompt_waits=None) -> Result:
        at_terminal("\n".join(answers), while_prompt_waits)
        exit_code = cli.main(list(arguments))
        return Result(exit_code, sys.stdout.getvalue(), capsys.readouterr().err)

    def settle_local() -> None:
        """The local part has applied before; the publish has not run."""
        at_terminal("yes")
        assert apply.apply_closed(state) == 0
        capsys.readouterr()

    made.run, made.settle_local = run, settle_local
    with made.stack:
        yield made


def lock_held(world: World) -> None:
    world.stack.enter_context(apply_lock_held(world.state))


def not_on_main(world: World) -> None:
    world.scripted_git.reference = b"refs/heads/feature\n"


def setup_fails(world: World) -> None:
    world.make_setup_missing()
    world.setup_run.exit_code = 3


def setup_is_interrupted(world: World) -> None:
    world.make_setup_missing()
    world.setup_run.interrupted = True


def recheck_cannot_tell(world: World) -> None:
    world.make_setup_missing()
    world.setup_run.check_afterwards = (2, "docker is down")


def start_fails(world: World) -> None:
    world.scripted.failing_verb = "up"


@pytest.fixture
def failing_app(world, repository, home, tmp_path, monkeypatch):  # noqa: F811
    """The catalog also declares the app `hopin`, whose newest release never becomes healthy."""
    commit_catalog(
        repository,
        CATALOG.replace(
            "services:\n",
            "github: {org: overnightworks}\nservices:\n  hopin:\n"
            "    repo: {default_branch: master}\n    run: {enabled: true}\n",
            1,
        ),
    )
    origin_at(tmp_path, home).release("v2026.10.22.1")
    docker = ScriptedDocker(world.state, home / "srv" / KEY, health_after_start="unhealthy")
    monkeypatch.setattr(
        apply,
        "apply_closed",
        functools.partial(apply.apply_closed, app_runtime=runtime_of(home, world.state, docker)),
    )


def nothing(_world: World) -> None:
    """The local part is happy; only the answers decide."""


@pytest.mark.parametrize(
    ("arrange", "answers", "expected_exit"),
    [
        pytest.param(lock_held, [], 1, id="C1 refused: another apply runs"),
        pytest.param(not_on_main, [], 2, id="C2 cannot plan: off main"),
        pytest.param(World.make_setup_missing, ["no"], 1, id="C3 the setup is declined"),
        pytest.param(setup_fails, ["yes"], 1, id="C4 the setup fails"),
        pytest.param(setup_is_interrupted, ["yes"], 1, id="C4 the setup is interrupted"),
        pytest.param(nothing, ["no"], 1, id="C5 the plan is declined"),
        pytest.param(start_fails, ["yes"], 1, id="C6 the local part ends partial"),
        pytest.param(recheck_cannot_tell, ["yes", "yes"], 2, id="C7 a setup ran, then no plan"),
    ],
)
def test_a_local_part_that_does_not_end_clean_publishes_nothing(
    world, arrange, answers, expected_exit
):
    """C1-C7 (D2, D3): the local exit stands, the publish is never entered."""
    arrange(world)

    result = world.run(*answers)

    assert result.exit_code == expected_exit
    assert NOTHING_PUBLISHED in result.err
    assert world.publish_never_began()


@pytest.mark.usefixtures("failing_app")
def test_an_app_that_fails_its_health_ends_the_local_part_partial_and_publishes_nothing(world):
    """#255 A21: the app step is part of the local part, whose exit gates the publish."""
    result = world.run("yes")

    assert result.exit_code == 1
    assert "partial, hopin is not healthy at v2026.10.22.1" in result.err
    assert NOTHING_PUBLISHED in result.err
    assert world.publish_never_began()


def test_a_setup_that_ran_before_a_local_plan_that_failed_is_recorded(world):
    """C7: the setup's run stays on record although the exit is 2."""
    recheck_cannot_tell(world)

    world.run("yes", "yes")

    assert len(world.setup_run.commands) == 1


def test_nothing_to_change_on_either_side_asks_nothing(world):
    """C8: with nothing to change, `edge apply` exits 0 and asks no question at all."""
    world.settle_local()
    world.fake_tofu.configure(**EMPTY_PLAN)

    result = world.run()

    assert result.exit_code == 0
    assert "Type 'yes'" not in result.out
    assert "nothing to change" in result.out
    assert "no findings" in result.out


def test_changes_on_both_sides_ask_the_local_plan_then_the_publish(world):
    """C9: two questions, the local plan's first; the publish's follows the local summary."""
    world.fake_tofu.configure(**CHANGING_PLAN)

    result = world.run("yes", "yes")

    assert result.exit_code == 0
    assert result.out.count(PLAN_QUESTION) == 2
    assert result.out.index(PLAN_QUESTION) < result.out.index("step-ca is on")
    assert result.out.index("step-ca is on") < result.out.rindex(PLAN_QUESTION)
    assert world.fake_tofu.call("apply")


def test_a_missing_setup_is_asked_before_the_two_plans(world):
    """C10: the setup's `yes`, the local plan's, the publish's, in this order."""
    world.make_setup_missing()
    world.fake_tofu.configure(**CHANGING_PLAN)

    result = world.run("yes", "yes", "yes")

    assert result.exit_code == 0
    assert result.out.count("Type 'yes'") == 3
    assert result.out.count(PLAN_QUESTION) == 2
    assert result.out.index(SETUP_QUESTION) < result.out.index(PLAN_QUESTION)
    assert result.out.index("step-ca is on") < result.out.rindex(PLAN_QUESTION)


def test_an_interrupted_setup_whose_containers_run_is_asked_again_and_publishes_nothing_before(
    world,
):
    """#118: go-live aborted after Keycloak started; the next apply asks the setup question."""
    world.settle_local()
    world.make_setup_missing()
    world.fake_tofu.configure(**CHANGING_PLAN)

    declined = world.run("no")

    assert declined.exit_code == 1
    assert SETUP_QUESTION in declined.out
    assert world.publish_never_began()

    finished = world.run("yes", "yes")

    assert finished.exit_code == 0
    assert finished.out.index(SETUP_QUESTION) < finished.out.index(PLAN_QUESTION)
    assert world.fake_tofu.call("apply")


def test_an_interrupted_stop_after_an_interrupted_setup_is_refused_and_publishes_nothing(world):
    """Go-live aborted, then `edge stop` aborted after its marker: the unit still runs."""
    world.settle_local()
    world.make_setup_missing()
    switch_off(world.state)
    world.fake_tofu.configure(**CHANGING_PLAN)
    changes_before = world.scripted.changing_verbs()

    result = world.run()

    assert result.exit_code == 1
    assert "step-ca is switched off but still runs and is not set up" in result.err
    assert "edge stop step-ca" in result.err
    assert SETUP_QUESTION not in result.out
    assert world.setup_run.commands == []
    assert world.scripted.changing_verbs() == changes_before
    assert world.publish_never_began()


def publish_is_declined(world: World) -> tuple[list[str], dict]:
    return ["yes", "no"], CHANGING_PLAN


def publish_has_findings(world: World) -> tuple[list[str], dict]:
    world.reader.outcome = STRAY_SNAPSHOT
    return ["yes", "yes"], CHANGING_PLAN


def publish_cannot_check(world: World) -> tuple[list[str], dict]:
    world.reader.outcome = cloudflare_read.CloudflareReadError("unreachable")
    return ["yes", "yes"], CHANGING_PLAN


def publish_fails(_world: World) -> tuple[list[str], dict]:
    return ["yes", "yes"], FAILING_APPLY


@pytest.mark.parametrize(
    "arrange",
    [publish_is_declined, publish_has_findings, publish_cannot_check, publish_fails],
    ids=["C11 declined", "C12 findings", "C12 cannot check", "C13 apply fails"],
)
def test_a_publish_that_does_not_end_clean_is_exit_1(world, arrange):
    """C11-C13: the publish's own non-zero exit is the exit of `edge apply`."""
    answers, plan = arrange(world)
    world.fake_tofu.configure(**plan)

    result = world.run(*answers)

    assert result.exit_code == 1
    assert "edge apply: the publish did not end clean (exit 1)" in result.err


def test_a_publish_that_cannot_plan_is_exit_1_with_the_summary_line(world):
    """C14: the publish's exit 2 becomes 1; only a local part can end `edge apply` with 2."""
    world.fake_tofu.configure(plan=1)

    result = world.run("yes")

    assert result.exit_code == 1
    assert "tofu plan failed (exit 1)" in result.err
    assert "edge apply: the publish did not end clean (exit 1)" in result.err


def test_a_missing_write_token_after_the_local_part_is_exit_1_naming_the_file(world, home):  # noqa: F811
    """C15 (D8): the local part ran; the publish names the file it misses."""
    (home / ".secrets/cloudflare/fleet-write").unlink()

    result = world.run("yes")

    assert result.exit_code == 1
    assert "step-ca is on" in result.out
    assert "fleet-write" in result.err
    assert world.fake_tofu.calls() == []


def test_ctrl_c_in_the_local_part_publishes_nothing_and_shows_no_traceback(world):
    """C16: SIGINT at the local plan's `yes`."""

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    result = world.run(while_prompt_waits=ctrl_c)

    assert result.exit_code == 1
    assert "edge apply: interrupted, nothing published" in result.err
    assert "Traceback" not in result.err
    assert world.publish_never_began()


def interrupting(phase, *, after_running: bool):
    """The real phase, then a Ctrl-C before its return reaches the composer."""

    def interrupted(*arguments, **keywords):
        if after_running:
            phase(*arguments, **keywords)
        raise KeyboardInterrupt

    return interrupted


def local_part_returns(monkeypatch, exit_code: int) -> None:
    monkeypatch.setattr(apply, "apply_closed", lambda _state: exit_code)


def publish_returns(monkeypatch, exit_code: int) -> list[tuple]:
    entries: list[tuple] = []

    def publish(*arguments):
        entries.append(arguments)
        return exit_code

    monkeypatch.setattr(external_cli, "apply_command", publish)
    return entries


def test_ctrl_c_in_the_gap_after_the_local_part_publishes_nothing(world, monkeypatch):
    """C17: the local part ended clean, the publish phase is not yet recorded."""
    monkeypatch.setattr(apply, "apply_closed", interrupting(apply.apply_closed, after_running=True))

    result = world.run("yes")

    assert result.exit_code == 1
    assert "edge apply: interrupted, nothing published" in result.err
    assert "step-ca is on" in result.out
    assert world.publish_never_began()


@pytest.mark.parametrize(
    "after_running", [False, True], ids=["out of the publish", "as the publish returns"]
)
def test_ctrl_c_with_the_publish_started_names_the_check_to_run(world, monkeypatch, after_running):
    """C18: no exit recorded yet, so the publish may have changed the services."""
    world.settle_local()
    world.fake_tofu.configure(**EMPTY_PLAN)
    monkeypatch.setattr(
        external_cli,
        "apply_command",
        interrupting(external_cli.apply_command, after_running=after_running),
    )

    result = world.run()

    assert result.exit_code == 1
    assert INTERRUPTED_IN_PUBLISH in result.err
    if after_running:
        assert "no findings" in result.out


class InterruptedOnSummary(io.StringIO):
    """A stderr that takes a Ctrl-C when the composer writes its own line."""

    def write(self, text: str) -> int:
        if text.startswith("edge apply:"):
            raise KeyboardInterrupt
        return super().write(text)


@pytest.mark.parametrize(("publish_exit", "expected"), [(0, 0), (2, 1)])
def test_ctrl_c_after_the_publish_exit_is_recorded_leaves_that_exit(
    world, monkeypatch, publish_exit, expected
):
    """C19: from the store on, the exit is binding."""
    local_part_returns(monkeypatch, 0)
    publish_returns(monkeypatch, publish_exit)
    monkeypatch.setattr(sys, "stderr", InterruptedOnSummary())

    assert cli.main(["apply"]) == expected


def test_the_composer_installs_no_sigterm_handler(world, monkeypatch):
    """C20: SIGTERM keeps the default action in the local part and at the publish's entry."""
    seen: dict[str, object] = {}

    def at_the_local_prompt() -> None:
        seen.setdefault("local part", signal.getsignal(signal.SIGTERM))

    def at_the_publish_entry(*_arguments):
        seen["publish entry"] = signal.getsignal(signal.SIGTERM)
        return 0

    monkeypatch.setattr(external_cli, "apply_command", at_the_publish_entry)
    world.run("yes", while_prompt_waits=at_the_local_prompt)

    assert seen == {"local part": signal.SIG_DFL, "publish entry": signal.SIG_DFL}


def test_closed_never_publishes(world):
    """C21: `--closed` is the local part alone."""
    result = world.run("yes", arguments=("apply", "--closed"))

    assert result.exit_code == 0
    assert world.publish_never_began()


def test_the_publish_starts_only_after_the_local_part_returned(world, monkeypatch):
    """C22 and C23: the order, and the entry `external_cli.apply_command(Path.home())`."""
    events: list[str] = []
    local = apply.apply_closed

    def local_part(*arguments, **keywords):
        events.append("local entered")
        exit_code = local(*arguments, **keywords)
        events.append("local returned")
        return exit_code

    def publish(*arguments, **keywords):
        events.append("publish entered")
        return 0

    monkeypatch.setattr(apply, "apply_closed", local_part)
    monkeypatch.setattr(external_cli, "apply_command", publish)
    world.run("yes")

    assert events == ["local entered", "local returned", "publish entered"]


def test_the_publish_is_entered_with_the_home_directory(world):
    """C23."""
    world.run("yes")

    assert [arguments for arguments, _ in world.publish_entries] == [(Path.home(),)]


def test_help_names_both_parts(capsys):
    """C24."""
    with pytest.raises(SystemExit) as stopped:
        cli.main(["apply", "--help"])

    assert stopped.value.code == 0
    said = " ".join(capsys.readouterr().out.split())
    assert "--closed" in said
    assert "the publish" in said
