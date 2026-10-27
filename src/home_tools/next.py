"""`home next`: what only the operator can do right now, one numbered step each, with its exact command.

It only reads, through the owners that decide each fact: the plan of `hal setup`, the view of `home secrets`
(presence by `stat`, never a value) and the hook check. Each rule's owner says who
closes its findings: a step for the operator, or bookkeeping an agent sorts in, which one closing line counts.
It changes nothing, asks for no password and needs no terminal, so agents may run it. A source it cannot read
becomes one line, and the other sources' steps still show. Exit code: always 0, because it is a view, not a check.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hal_output import Row
from home_tools import hook_check, secrets_setup, secrets_view, setup
from home_tools.secrets_view import FindingRule, SecretEntry, SecretFinding
from home_tools.layout import Layout
from home_tools.report import ClosedBy, Tally

if TYPE_CHECKING:
    from home_tools.cli import Ports

ANY_TERMINAL = "beliebiges Terminal"
# Claude Code's `!` is no terminal either, so a command that refuses without one names the operator's own window.
OWN_TERMINAL = "eigenes Terminalfenster"
BY_HAND = "von Hand"
BOOKKEEPING_LINE = "Außerdem zum Einsortieren: {counts}. Das macht ein Agent: sag „räum home auf“."
UNREADABLE = "konnte {source} nicht lesen; den Fehler zeigt: {shows_error}"
# What a source may fail on: a file it cannot read, an invalid manifest or configuration, a tool that will not run.
SOURCE_FAILURES = (OSError, ValueError, hook_check.HookCheckUnavailable)


@dataclass(frozen=True)
class OperatorStep:
    command: str
    where: str
    expected: str
    why: str = ""


@dataclass(frozen=True)
class Bookkeeping:
    """How many findings of one kind an agent sorts in; it names no path."""

    tally: Tally
    count: int


@dataclass(frozen=True)
class Source:
    name: str
    steps: Callable[[Layout, Ports], Iterator[OperatorStep | Bookkeeping]]
    shows_error: str


def _as_command(text: str) -> str:
    """An owner quotes a whole command in backticks; the step shows it bare."""
    inner = text.removeprefix("`").removesuffix("`")
    return inner if text != inner and "`" not in inner else text


def _setup_apply_step(ports: Ports, expected: str, why: str = "") -> OperatorStep:
    """`hal setup --apply` runs every step, so whichever is open, it asks the operator for sudo in the own window; not the agent."""
    asks_for_sudo = setup.apply_asks_for_sudo(ports)
    return OperatorStep(
        _as_command(hook_check.SETUP_APPLY),
        OWN_TERMINAL if asks_for_sudo else ANY_TERMINAL,
        f"{expected}; it asks once for sudo" if asks_for_sudo else expected,
        why,
    )


def _setup_steps(layout: Layout, ports: Ports) -> Iterator[OperatorStep]:
    """One `hal setup --apply` for every open step."""
    if pending := setup.pending(layout, ports):
        changed = f"{', '.join(step.name for step in pending)}: changed"
        yield _setup_apply_step(ports, changed, "; ".join(dict.fromkeys(step.why for step in pending)))


def _row_text(row: Row) -> str:
    return f"{row.subject} {row.event} {row.fact}"


def _missing_secret_steps(missing: list[SecretEntry]) -> Iterator[OperatorStep]:
    guided = [entry for entry in missing if entry.written_by_setup]
    by_hand = [entry for entry in missing if not entry.written_by_setup]
    if guided:
        said = [*(_row_text(secrets_setup.stored_row(entry)) for entry in guided), f"then {setup.APPLY_PLAN}"]
        where = OWN_TERMINAL if secrets_setup.SETUP_NEEDS_A_TERMINAL else ANY_TERMINAL
        yield OperatorStep("home secrets --setup", where, "; ".join(said))
    for entry in by_hand:
        yield OperatorStep(_as_command(entry.recreate), f"von Hand, für {entry.name}", f"home secrets zeigt {entry.name} vorhanden")


def _cleared(finding: SecretFinding) -> str:
    return f"home secrets meldet {finding.rule.value} für {finding.subject} nicht mehr"


def _fix_steps(fixable: list[SecretFinding]) -> Iterator[OperatorStep]:
    """Every finding that names its fix is a step; what blocks the wizard first, since it stores nothing below such a folder."""
    for finding in sorted(fixable, key=lambda finding: finding.rule not in secrets_view.SETUP_BLOCKERS):
        command = _as_command(finding.fix)
        where = ANY_TERMINAL if command != finding.fix else BY_HAND
        yield OperatorStep(command, where, _cleared(finding))


def _by_hand_step(finding: SecretFinding, entries: dict[str, SecretEntry]) -> OperatorStep:
    """A finding no command clears is still a step: its place and its recommendation, by hand."""
    return OperatorStep(f"{secrets_view.places(entries[finding.subject])}: {finding.recommendation}", BY_HAND, _cleared(finding))


def _bookkeeping(kinds_in_order: Iterable[Tally | None], found: Iterable[tuple[Tally | None, int]]) -> Iterator[Bookkeeping]:
    """One count per kind that has findings, in the owner's fixed order of its rules; each finding adds what it stands for."""
    counts: Counter[Tally | None] = Counter()
    for tally, count in found:
        counts[tally] += count
    for tally in kinds_in_order:
        if tally is not None and counts[tally]:
            yield Bookkeeping(tally, counts[tally])


def _secrets_steps(layout: Layout, ports: Ports) -> Iterator[OperatorStep | Bookkeeping]:
    """Every operator finding of the view is a step: its fix, the command its rule names, or else by hand; the rest is counted.

    The agent holds no `~/.secrets`, so none of it is its step.
    """
    if setup.runs_as_agent(ports):
        return
    view, _report = secrets_view.inspect(layout, ports.probe, ports.now)
    entries = {status.entry.name: status.entry for status in view.statuses}
    fixable: list[SecretFinding] = []
    without_fix: dict[FindingRule, list[SecretFinding]] = {}
    bookkeeping: list[SecretFinding] = []
    for finding in view.findings:
        if finding.rule.closed_by is ClosedBy.BOOKKEEPING:
            bookkeeping.append(finding)
        elif finding.fix:
            fixable.append(finding)
        else:
            without_fix.setdefault(finding.rule, []).append(finding)

    def entries_found_by(rule: FindingRule) -> list[SecretEntry]:
        return [entries[finding.subject] for finding in without_fix.pop(rule, [])]

    yield from _fix_steps(fixable)
    yield from _missing_secret_steps(entries_found_by(FindingRule.MISSING))
    for entry in entries_found_by(FindingRule.VERIFICATION_FAILED):
        yield OperatorStep(_as_command(entry.recreate), ANY_TERMINAL, f"home secrets zeigt {entry.name} geprüft: gültig")
    for entry in entries_found_by(FindingRule.REVOCATION_UNCONFIRMED):
        yield OperatorStep(entry.revoke_url, f"Browser, {entry.name} widerrufen", "das Token steht dort nicht mehr")
    for findings in without_fix.values():
        yield from (_by_hand_step(finding, entries) for finding in findings)
    yield from _bookkeeping((rule.tally for rule in FindingRule), ((finding.rule.tally, 1) for finding in bookkeeping))


def _hook_steps(layout: Layout, ports: Ports) -> Iterator[OperatorStep]:
    for finding in hook_check.collect_findings(layout, ports.run):
        cleared = f"home check meldet {finding.rule} für {finding.subject} nicht mehr"
        if finding.fix == hook_check.SETUP_APPLY:
            yield _setup_apply_step(ports, cleared)
        else:
            yield OperatorStep(_as_command(finding.fix), ANY_TERMINAL, cleared)


# Most blocking first: a folder state blocks the wizard, the secrets block edge, the hook guards every commit.
SOURCES = (
    Source("die Geheimnisse", _secrets_steps, "home secrets"),
    Source("den Stand von hal setup", _setup_steps, "hal setup"),
    Source("die Hook-Prüfung", _hook_steps, "home check"),
)


@dataclass(frozen=True)
class Plan:
    steps: tuple[OperatorStep, ...]
    unreadable: tuple[Source, ...]
    bookkeeping: tuple[Bookkeeping, ...] = ()


def collect(layout: Layout, ports: Ports) -> Plan:
    """Every source's steps in order, each command once, and its bookkeeping counts; a source that fails is named instead."""
    steps: dict[str, OperatorStep] = {}
    unreadable: list[Source] = []
    bookkeeping: list[Bookkeeping] = []
    for source in SOURCES:
        try:
            found = list(source.steps(layout, ports))
        except SOURCE_FAILURES:
            unreadable.append(source)
            continue
        for item in found:
            if isinstance(item, Bookkeeping):
                bookkeeping.append(item)
            else:
                steps.setdefault(item.command, item)
    return Plan(tuple(steps.values()), tuple(unreadable), tuple(bookkeeping))


def _numbered_lines(plan: Plan) -> Iterator[str]:
    """A source it cannot read comes first, since none of that source's steps can show until its error is cleared."""
    unreadable = [(UNREADABLE.format(source=source.name, shows_error=source.shows_error), "") for source in plan.unreadable]
    steps = [(f"{step.command} ({step.where}) — erwartet: {step.expected}", step.why) for step in plan.steps]
    for number, (line, why) in enumerate(unreadable + steps, start=1):
        yield f"{number}. {line}"
        if why:
            yield f"   warum: {why}"


def _nothing_left(plan: Plan) -> str:
    """Says "Nichts zu tun." only when nothing at all is open; with bookkeeping left, nothing is for the operator."""
    nothing = "Sonst nichts" if plan.unreadable else "Nichts"
    return f"{nothing} für dich zu tun." if plan.bookkeeping else f"{nothing} zu tun."


def render(plan: Plan) -> str:
    lines = list(_numbered_lines(plan))
    if not plan.steps:
        lines.append(_nothing_left(plan))
    if plan.bookkeeping:
        counts = ", ".join(kind.tally.of(kind.count) for kind in plan.bookkeeping)
        lines.append(BOOKKEEPING_LINE.format(counts=counts))
    return "\n".join(lines) + "\n"


def _next_from_args(_args: argparse.Namespace, layout: Layout, ports: Ports) -> int:
    sys.stdout.write(render(collect(layout, ports)))
    return 0


NEXT_EXAMPLES = """\
Beispiele:
  home next    nummerierte Schritte nur für dich, der dringendste zuerst, je mit Befehl, Ort und Erwartung;
               zuletzt eine Zeile, die zählt, was ein Agent einsortiert

Liest nur und ändert nichts; braucht kein Terminal, Agenten dürfen es fahren.
Exit-Code: immer 0, auch mit Schritten; 3 bei einem unerwarteten Fehler.
"""


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "next",
        help="was jetzt von dir gebraucht wird, je Schritt mit dem genauen Befehl",
        description=(
            "Zeigt die Schritte, die nur du tun kannst, aus hal setup, home secrets und der Hook-Prüfung,"
            " und zählt in einer Zeile, was ein Agent einsortiert; ändert nichts."
        ),
        epilog=NEXT_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # It reads the secrets view, so an unexpected error is shown without its message, as `home secrets` does.
    parser.set_defaults(handler=_next_from_args, error_may_quote_a_secret=True)
