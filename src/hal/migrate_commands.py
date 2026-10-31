"""`hal migrate`: the transitional steps between two layouts of the workstation. Its one subject
is `stack`: the stack leaves the Compose project it ran under and starts under the one
`compose.yaml` names. Shape of `hal rename`: the plan, one `yes`, `--dry-run` only shows."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from hal.grammar import Asks, Command, ExitCode, add_dry_run
from hal.output import (
    Glyph,
    Row,
    Verdict,
    confirmed,
    write_error,
    write_row,
    write_verdict,
)
from hal.output.command import Verb, command
from hal.stack import compose, project_move
from hal.stack.compose import ComposeError, Runner
from hal.stack.project_move import Plan, Stage
from hal.stack.terminal import at_terminal

STACK = "stack"


class Event(StrEnum):
    REMOVE = "remove"
    REMOVED = "removed"
    KEEP = "keep"
    START = "start"
    STARTED = "started"


@dataclass(frozen=True)
class World:
    """The boundaries to the outside: Docker and the terminal."""

    docker: Runner
    is_interactive: Callable[[], bool]


def real_world() -> World:
    return World(compose.run_docker, at_terminal)


def _write_plan(plan: Plan) -> None:
    source = f"project {plan.source}"
    if plan.containers:
        write_row(Row(Glyph.REMOVE, "containers", Event.REMOVE, source, details=plan.containers))
        write_row(Row(Glyph.REMOVE, "networks", Event.REMOVE, source, details=plan.networks))
    if plan.kept_volumes:
        kept = Row(Glyph.UNCHANGED, "volumes", Event.KEEP, "named by compose.yaml")
        write_row(replace(kept, details=plan.kept_volumes))
    if plan.removed_volumes:
        write_row(
            Row(Glyph.REMOVE, "volumes", Event.REMOVE, "not named by compose.yaml",
                details=plan.removed_volumes)
        )  # fmt: skip
    if plan.core:
        write_row(Row(Glyph.ADD, "core", Event.START, f"project {plan.target}", details=plan.core))


def _write_done(stage: Stage, names: tuple[str, ...], plan: Plan) -> None:
    subject, event, fact = {
        Stage.DOWN: ("containers", Event.REMOVED, f"project {plan.source}"),
        Stage.VOLUME: ("volumes", Event.REMOVED, ", ".join(names)),
        Stage.START: ("core", Event.STARTED, f"project {plan.target}"),
    }[stage]
    write_row(Row(Glyph.DONE, subject, event, fact))


def _perform(plan: Plan, world: World) -> ExitCode:
    try:
        for stage, names in project_move.move(plan, world.docker):
            _write_done(stage, names, plan)
    except ComposeError as error:
        write_error(f"migrate stack stopped, {error} · rerun finishes what is left", _again())
        return ExitCode.FAILURE
    write_verdict(
        Verdict(
            Glyph.DONE, f"stack moved from {plan.source} to {plan.target}", (),
            next_step=command(Verb.APPLY),
        )
    )  # fmt: skip
    return ExitCode.OK


def _again() -> str:
    return command(Verb.MIGRATE, STACK)


def migrate_stack(world: World, dry_run: bool) -> ExitCode:
    try:
        plan = project_move.make_plan(world.docker)
    except project_move.Refusal as refusal:
        write_error(f"cannot migrate stack, {refusal} · nothing changed")
        return ExitCode.REFUSED
    except ComposeError as error:
        write_error(f"cannot migrate stack, {error} · nothing changed")
        return ExitCode.FAILURE
    if not plan.has_work:
        write_verdict(Verdict(Glyph.UNCHANGED, f"nothing to move, no stack of {plan.source}", ()))
        return ExitCode.OK
    _write_plan(plan)
    if dry_run:
        hint = f"run {_again()}"
        write_verdict(Verdict(Glyph.UNCHANGED, "plan only, nothing changed", (), hint=hint))
        return ExitCode.OK
    if not world.is_interactive():
        write_error("migrate stack asks yes at a terminal · nothing changed", _again())
        return ExitCode.REFUSED
    if not confirmed(f"move the stack from {plan.source} to {plan.target}"):
        write_error("aborted, nothing changed")
        return ExitCode.FINDINGS
    return _perform(plan, world)


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("subject", choices=[STACK], help="what to move")
    add_dry_run(parser)


def _run(arguments: argparse.Namespace) -> ExitCode:
    return migrate_stack(real_world(), arguments.dry_run)


COMMANDS = (
    Command(
        Verb.MIGRATE,
        "move the stack between its Compose projects"
        " (shows the plan, asks yes, moves; --dry-run only shows)",
        _run,
        _configure,
        json=False,
        asks=Asks.YES,
    ),
)
