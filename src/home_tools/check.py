"""`home check` runs every read-only check of `home` and nudges only on news.

It writes one report and sends at most one desktop notification, and only when
the set of findings differs from the last run's: findings that merely persist
never nag. A check that breaks or recovers is news too, but its findings count
as neither new nor gone; the title names the broken check. Exit code: 0 when
every check is clean, 1 when any finding exists, 2 when the configuration file
or the secrets manifest is invalid, 3 when a check broke on an unexpected error. A broken check leaves the other checks' findings
in the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from home_tools import hook_check, secrets_view, tidy_home
from home_tools.report import findings_count, read_state_file, write_state_file
from home_tools.secrets_view import ExitCodeProbe
from home_tools.tidy_home import CommandRunner, Config, Layout

if TYPE_CHECKING:
    from home_tools.cli import Ports


class CheckFailed(Exception):
    """Raised after the report is written and the news sent, so a broken check still ends the run as an error."""


@dataclass(frozen=True)
class CheckContext:
    layout: Layout
    config: Config
    run: CommandRunner
    probe: ExitCodeProbe
    now: datetime


@dataclass(frozen=True)
class CheckResult:
    name: str
    report: str
    finding_identities: frozenset[str]
    error: Exception | None = None

    @property
    def failure(self) -> str:
        return _failure_line(self.name, self.error) if self.error else ""


def _failure_line(name: str, error: Exception) -> str:
    return f"Prüfung {name} fehlgeschlagen ({type(error).__name__}): {error}"


CheckFindings = tuple[str, frozenset[str]]
"""What one check returns: its report section and the identities of its findings."""


def _tidy_check(ctx: CheckContext) -> CheckFindings:
    findings, report = tidy_home.dry_run(ctx.layout, ctx.now, ctx.config.tidy, ctx.run)
    return report, frozenset(finding.identity for finding in findings)


def _secrets_check(ctx: CheckContext) -> CheckFindings:
    view, report = secrets_view.inspect(ctx.layout, ctx.probe, ctx.now)
    return report, frozenset(finding.identity for finding in view.findings)


def _hook_check(ctx: CheckContext) -> CheckFindings:
    findings, report = hook_check.inspect(ctx.layout, ctx.run, ctx.now)
    return report, frozenset(finding.identity for finding in findings)


# The one place a check gets its name; the name prefixes every remembered finding identity.
CHECKS: tuple[tuple[str, Callable[[CheckContext], CheckFindings]], ...] = (
    ("tidy", _tidy_check),
    ("secrets", _secrets_check),
    ("hook", _hook_check),
)
CHECK_FAILED_IDENTITY = "check-failed"


def _run_isolated(name: str, run_check: Callable[[CheckContext], CheckFindings], ctx: CheckContext) -> CheckResult:
    """A check that breaks becomes one line in the report, so the other checks' findings stay visible."""
    try:
        report, identities = run_check(ctx)
    except Exception as error:
        return CheckResult(name, f"# home {name}\n\n{_failure_line(name, error)}\n", frozenset(), error)
    return CheckResult(name, report, identities)


def _failure_marker(name: str) -> str:
    return f"{name}\t{CHECK_FAILED_IDENTITY}"


CHECK_NAMES = frozenset(name for name, _ in CHECKS)
FAILURE_MARKERS = frozenset(_failure_marker(name) for name in CHECK_NAMES)


def remembered_identities(results: tuple[CheckResult, ...], previous: frozenset[str] | None) -> frozenset[str]:
    """What the run found, whatever order the checks report it in.

    A broken check is remembered as one failure marker, so its breaking is news; the findings it could not
    observe are carried over from the previous run, because unobserved is neither new nor gone.
    """
    before = previous or frozenset()
    identities: set[str] = set()
    for result in results:
        if result.error:
            identities.add(_failure_marker(result.name))
            identities.update(identity for identity in before if identity.startswith(f"{result.name}\t"))
        else:
            identities.update(f"{result.name}\t{identity}" for identity in result.finding_identities)
    return frozenset(identities)


def render_report(results: tuple[CheckResult, ...], now: datetime) -> str:
    total = sum(len(result.finding_identities) for result in results)
    failed = [result.name for result in results if result.error]
    failed_clause = f" | fehlgeschlagen: {', '.join(failed)}" if failed else ""
    header = f"# home check {now:%d.%m.%Y %H:%M} | {findings_count(total)}{failed_clause}"
    return "\n".join([header, *("\n" + result.report.rstrip() for result in results)]) + "\n"


def _previous_identities(layout: Layout) -> frozenset[str] | None:
    text = read_state_file(layout.home, layout.check_findings_file)
    return None if text is None else _parse_identities(text)


def _parse_identities(text: str) -> frozenset[str]:
    """Only identities `home check` itself remembers pass, so nothing else is ever carried into the state file."""
    identities = json.loads(text)
    if not isinstance(identities, list) or not all(isinstance(identity, str) and _is_remembered(identity) for identity in identities):
        raise ValueError("gemerkte Funde ungültig: erwartet eine Liste aus Prüfung, Regel und Gegenstand")
    return frozenset(identities)


def _is_remembered(identity: str) -> bool:
    """Check name, then either the failure marker or a finding's rule and subject, each separated by a tab."""
    name, _, finding = identity.partition("\t")
    rule, _, subject = finding.partition("\t")
    return name in CHECK_NAMES and (finding == CHECK_FAILED_IDENTITY or bool(rule and subject))


def _remember(layout: Layout, identities: frozenset[str]) -> None:
    write_state_file(layout.home, layout.check_findings_file, json.dumps(sorted(identities), indent=0) + "\n")


NOTIFY_FAILED = "home: notify-send fehlgeschlagen; der nächste Lauf meldet die geänderten Funde erneut.\n"


def _is_news(current: frozenset[str], previous: frozenset[str] | None) -> bool:
    """A changed set is news while findings remain or a broken check recovered; a home turning clean stays silent."""
    recovered = bool(((previous or frozenset()) & FAILURE_MARKERS) - current)
    return current != previous and (bool(current) or recovered)


def notification_body(current: frozenset[str], previous: frozenset[str] | None, report_file: Path) -> str:
    """Counts findings only: a failure marker is named in the summary, never counted as new or gone."""
    found, before = current - FAILURE_MARKERS, (previous or frozenset()) - FAILURE_MARKERS
    return f"{len(found - before)} neu, {len(before - found)} weg. Bericht: {report_file}"


OPEN_REPORT_ACTION = "open=Bericht öffnen"
# Runs inside the detached unit. A printed notification ID above 0 proves delivery (0 is what a failed show prints),
# so only then is the unit ready and systemd-run succeeds; afterwards the click is awaited and opens the report, a dismissal ends quietly.
# notify-send holds its ID in a block buffer until it exits, which --action delays to the click, so it runs line-buffered.
WAIT_FOR_CLICK_SCRIPT = (
    f'stdbuf -oL notify-send --print-id --action="{OPEN_REPORT_ACTION}" "$1" "$2" | '
    '{ read -r id && [ "$id" -gt 0 ] 2>/dev/null || exit 1; systemd-notify --ready; '
    'read -r action; if [ "$action" = open ]; then exec xdg-open "$3"; fi; }'
)
DETACHED_UNIT_OPTIONS = ["systemd-run", "--user", "--collect", "-p", "Type=notify", "-p", "NotifyAccess=all"]


def notification_command(summary: str, body: str, report_file: Path) -> list[str]:
    """systemd-run returns once the unit reported the notification as delivered; the click is awaited by the unit, never by the check."""
    return [*DETACHED_UNIT_OPTIONS, "sh", "-c", WAIT_FOR_CLICK_SCRIPT, "sh", summary, body, str(report_file)]


def _notify(run: CommandRunner, summary: str, body: str, report_file: Path) -> bool:
    """True once the notification was delivered and its click-waiting unit runs detached."""
    return run(notification_command(summary, body, report_file)) is not None


def _run_error(failed: list[CheckResult]) -> secrets_view.ManifestError | CheckFailed:
    """An invalid manifest keeps its own exit code 2; any other broken check ends the run as an unexpected error."""
    manifest_errors = [result.error for result in failed if isinstance(result.error, secrets_view.ManifestError)]
    if len(manifest_errors) == len(failed):
        return manifest_errors[0]
    return CheckFailed("; ".join(result.failure for result in failed))


def check(ctx: CheckContext) -> int:
    layout = ctx.layout
    results = tuple(_run_isolated(name, run_check, ctx) for name, run_check in CHECKS)
    report = render_report(results, ctx.now)
    write_state_file(layout.home, layout.check_report_file, report)
    previous = _previous_identities(layout)
    current = remembered_identities(results, previous)
    sys.stdout.write(report)
    news = ctx.config.notify_enabled and _is_news(current, previous)
    summary = report.splitlines()[0].removeprefix("# ")
    body = notification_body(current, previous, layout.check_report_file)
    if news and not _notify(ctx.run, summary, body, layout.check_report_file):
        # The remembered set stays at the last delivered one, so the next run sends this news again.
        sys.stderr.write(NOTIFY_FAILED)
    else:
        _remember(layout, current)
    failed = [result for result in results if result.error]
    if failed:
        raise _run_error(failed)
    return 1 if current else 0


CHECK_EXAMPLES = """\
Beispiele:
  home check    alle Prüfungen, ein Bericht, höchstens eine Benachrichtigung

Benachrichtigt nur, wenn sich die Menge der Funde seit dem letzten Lauf
geändert hat. Bericht: ~/.local/state/home/check-report.md
Exit-Code: 0 sauber, 1 Funde, 2 Konfiguration oder Manifest ungültig, 3 unerwarteter Fehler.
"""


def _check_from_args(_args: argparse.Namespace, layout: Layout, config: Config, ports: Ports) -> int:
    return check(CheckContext(layout, config, ports.run, ports.probe, ports.now))


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "check",
        help="alle Prüfungen ohne Änderung; meldet nur, wenn sich die Funde ändern",
        description="Führt jede Prüfung von home lesend aus und schreibt einen Bericht.",
        epilog=CHECK_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=_check_from_args)
