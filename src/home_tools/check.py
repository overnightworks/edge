"""`home check` runs every read-only check of `home` and nudges only on news.

It writes one report and sends at most one desktop notification, and only when
the set of findings differs from the last run's: findings that merely persist
never nag. Exit code: 0 when every check is clean, 1 when any finding exists,
2 when the configuration file is invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from home_tools import tidy_home
from home_tools.tidy_home import CommandRunner, Config, Layout


@dataclass(frozen=True)
class CheckResult:
    name: str
    report: str
    finding_identities: frozenset[str]


def _tidy_check(layout: Layout, now: datetime, config: Config, run: CommandRunner) -> CheckResult:
    findings, report = tidy_home.dry_run(layout, now, config.tidy, run)
    return CheckResult("tidy", report, frozenset(finding.identity for finding in findings))


CHECKS: tuple[Callable[[Layout, datetime, Config, CommandRunner], CheckResult], ...] = (_tidy_check,)


def fingerprint(results: tuple[CheckResult, ...]) -> str:
    """Stable across runs for the same findings, whatever order the checks report them in."""
    identities = sorted(f"{result.name}\t{identity}" for result in results for identity in result.finding_identities)
    return hashlib.sha256("\n".join(identities).encode()).hexdigest()


def render_report(results: tuple[CheckResult, ...], now: datetime) -> str:
    total = sum(len(result.finding_identities) for result in results)
    header = f"# home check {now:%d.%m.%Y %H:%M} | {total} Funde"
    return "\n".join([header, *("\n" + result.report.rstrip() for result in results)]) + "\n"


def _previous_fingerprint(layout: Layout) -> str | None:
    path = layout.check_fingerprint_file
    return path.read_text().strip() if path.is_file() else None


NOTIFY_FAILED = "home: notify-send fehlgeschlagen; der nächste Lauf meldet die geänderten Funde erneut.\n"


def _notify(run: CommandRunner, summary: str) -> bool:
    """True once the desktop notification was delivered."""
    return run(["notify-send", summary]) is not None


def check(layout: Layout, config: Config, run: CommandRunner) -> int:
    now = datetime.now()
    results = tuple(run_check(layout, now, config, run) for run_check in CHECKS)
    report = render_report(results, now)
    current = fingerprint(results)
    has_findings = any(result.finding_identities for result in results)

    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.check_report_file.write_text(report)
    sys.stdout.write(report)
    news = config.notify_enabled and has_findings and current != _previous_fingerprint(layout)
    if news and not _notify(run, report.splitlines()[0].removeprefix("# ")):
        # The fingerprint stays at the last delivered set, so the next run sends this news again.
        sys.stderr.write(NOTIFY_FAILED)
    else:
        layout.check_fingerprint_file.write_text(current + "\n")
    return 1 if has_findings else 0


CHECK_EXAMPLES = """\
Beispiele:
  home check    alle Prüfungen, ein Bericht, höchstens eine Benachrichtigung

Benachrichtigt nur, wenn sich die Menge der Funde seit dem letzten Lauf
geändert hat. Bericht: ~/.local/state/home/check-report.md
Exit-Code: 0 sauber, 1 Funde, 2 Konfiguration ungültig.
"""


def _check_from_args(_args: argparse.Namespace, layout: Layout, config: Config, run: CommandRunner) -> int:
    return check(layout, config, run)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "check",
        help="alle Prüfungen ohne Änderung; meldet nur neue Funde",
        description="Führt jede Prüfung von home lesend aus und schreibt einen Bericht.",
        epilog=CHECK_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=_check_from_args)
