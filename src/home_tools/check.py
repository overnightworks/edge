"""`home check` runs every read-only check of `home` and says in plain words whether all is well and what to do.

It writes one report and sends at most one desktop notification and one e-mail, and only when an action appears or
goes: every finding has one level (in Ordnung, Hinweis, Handlungsbedarf), notes alone and findings that merely persist
never nag. A check that breaks or recovers is an action that appears or goes, because its failure is remembered as one; the mail and the popup
name it. The desktop counts as a channel only while a graphical session
exists, the mail only while `~/.secrets/smtp/account.toml` exists; the news is remembered once every such channel delivered it and at least one exists. Exit code: 0 when
every check is clean, 1 when any finding exists, 2 when the configuration file
or the secrets manifest is invalid, 3 when a check broke on an unexpected error. A broken check leaves the other checks' findings
in the report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from home_tools import backup_check, hook_check, logs_check, mail, platform_check, secrets_view, tidy_home
from home_tools.logs_check import LOG_DIRECTORY
from home_tools.platform_check import REBOOT_REQUIRED_FILE
from home_tools.report import Level, findings_count, read_state_file, write_state_file
from home_tools.secrets_view import ExitCodeProbe, FindingRule, SecretKind
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


MISSING_PASSWORD_RULE = "missing-password"


def _secret_rule(finding: secrets_view.SecretFinding) -> str:
    """A missing human password is its own rule: the one missing secret that needs action."""
    if (finding.rule, finding.kind) == (FindingRule.MISSING, SecretKind.HUMAN_PASSWORD):
        return MISSING_PASSWORD_RULE
    return finding.rule.value


def _secrets_check(ctx: CheckContext) -> CheckFindings:
    view, report = secrets_view.inspect(ctx.layout, ctx.probe, ctx.now)
    return report, frozenset(f"{_secret_rule(finding)}\t{finding.subject}" for finding in view.findings)


def _hook_check(ctx: CheckContext) -> CheckFindings:
    findings, report = hook_check.inspect(ctx.layout, ctx.run, ctx.now)
    return report, frozenset(finding.identity for finding in findings)


def _logs_check(ctx: CheckContext) -> CheckFindings:
    findings, report = logs_check.inspect(ctx.run, ctx.now, LOG_DIRECTORY)
    return report, frozenset(finding.identity for finding in findings)


def _platform_check(ctx: CheckContext) -> CheckFindings:
    findings, report = platform_check.inspect(ctx.run, ctx.now, REBOOT_REQUIRED_FILE)
    return report, frozenset(finding.identity for finding in findings)


def _backup_check(ctx: CheckContext) -> CheckFindings:
    findings, report = backup_check.inspect(ctx.layout.home, ctx.now)
    return report, frozenset(finding.identity for finding in findings)


# The one place a check gets its name; the name prefixes every remembered finding identity.
CHECKS: tuple[tuple[str, Callable[[CheckContext], CheckFindings]], ...] = (
    ("tidy", _tidy_check),
    ("secrets", _secrets_check),
    ("hook", _hook_check),
    ("logs", _logs_check),
    ("backup", _backup_check),
    ("platform", _platform_check),
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
    """Check name, then either the failure marker or the rule and subject of a finding with a level, each separated by a tab."""
    name, _, finding = identity.partition("\t")
    rule, _, subject = finding.partition("\t")
    return name in CHECK_NAMES and (finding == CHECK_FAILED_IDENTITY or bool(subject and rule != CHECK_FAILED_IDENTITY and _rule_report(name, rule)))


def _remember(layout: Layout, identities: frozenset[str]) -> None:
    write_state_file(layout.home, layout.check_findings_file, json.dumps(sorted(identities), indent=0) + "\n")


NOTIFY_FAILED = "home: notify-send fehlgeschlagen; der nächste Lauf meldet die geänderten Funde erneut.\n"


@dataclass(frozen=True)
class RuleReport:
    """What the mail says of one rule: its level, the plain sentence, and the one step that clears it (for an action)."""

    level: Level
    sentence: str
    next_step: str = ""
    area_note: str = ""
    """What the area line says after the level when the rule is a note; the sentence without its final period when empty."""
    resolved: str = ""
    """What the mail says once an action is gone, as a sentence that reads true on its own."""


AGENT_STEP = "Sag einem Agenten „{area}: bitte schau nach“."


def _container_report(sentence: str, problem: str, resolved: str) -> RuleReport:
    return RuleReport(Level.ACTION, sentence, f"Sag einem Agenten „{{subject}} {problem}, schau nach“.", resolved=resolved)


ACTION, NOTE = Level.ACTION, Level.NOTE
HOME_NEXT = "Führe `home next` aus."
BACKUP_NOW = "Führe `hal backup` aus."
SETUP_APPLY = "Führe `hal setup --apply` aus."
HOOK_BROKEN = RuleReport(
    ACTION, "Der Schutz, der jeden Commit auf Geheimnisse prüft, greift nicht.", SETUP_APPLY, resolved="Der Schutz, der jeden Commit auf Geheimnisse prüft, greift wieder."
)
LOGS_UNSEEN = RuleReport(ACTION, "Der Check kann die Logs nicht vollständig sehen.", AGENT_STEP, resolved="Der Check kann die Logs wieder vollständig sehen.")
LOGS_NOT_ROTATED = RuleReport(NOTE, "Logs werden noch nicht überall nach 30 Tagen gelöscht.", area_note="noch nicht überall nach 30 Tagen gelöscht")
SECRET_UNFIT_FOLDER = RuleReport(NOTE, "Ein Ordner bei den Geheimnissen ist nicht in Form.", area_note="ein Ordner bei den Geheimnissen ist nicht in Form")
# The sentence of a rule names only what the mail may carry: a Compose project or an app by name, never a secret, a path or a home file (#415 Z6).
RULE_REPORTS: dict[tuple[str, str], RuleReport] = {
    ("platform", "container-restarting"): _container_report("{subject} startet ständig neu und ist darum nicht erreichbar.", "startet ständig neu", "{subject} läuft wieder."),
    ("platform", "container-dead"): _container_report("{subject} ist abgestürzt.", "ist abgestürzt", "{subject} läuft wieder."),
    ("platform", "container-unhealthy"): _container_report("{subject} läuft, meldet sich aber krank.", "meldet sich krank", "{subject} meldet sich wieder gesund."),
    ("platform", "container-exited"): _container_report("{subject} hat sich mit einem Fehler beendet.", "hat sich mit einem Fehler beendet", "{subject} läuft wieder."),
    ("platform", "docker-unreadable"): RuleReport(ACTION, "Der Check kann Docker nicht lesen; ob die Dienste laufen, ist unbekannt.", AGENT_STEP, resolved="Der Check kann Docker wieder lesen."),
    ("platform", "reboot-pending"): RuleReport(NOTE, "Ein Neustart des PCs steht aus.", area_note="ein Neustart des PCs steht aus"),
    ("backup", "backup-stale"): RuleReport(ACTION, "{subject} wurde zu lange nicht gesichert.", BACKUP_NOW, resolved="{subject} wurde wieder gesichert."),
    ("backup", "backup-failed"): RuleReport(ACTION, "Die letzte Sicherung ist fehlgeschlagen.", BACKUP_NOW, resolved="Die letzte Sicherung hat geklappt."),
    ("backup", "backup-status-unreadable"): RuleReport(ACTION, "Ob die Sicherung läuft, ist unbekannt.", BACKUP_NOW, resolved="Ob die Sicherung läuft, ist wieder bekannt."),
    ("backup", "backup-crashed"): RuleReport(ACTION, "Die Sicherung hängt.", AGENT_STEP, resolved="Die Sicherung hängt nicht mehr."),
    ("hook", "hooks-path"): HOOK_BROKEN,
    ("hook", "hook-file"): HOOK_BROKEN,
    ("hook", "quality"): HOOK_BROKEN,
    ("hook", "engines"): HOOK_BROKEN,
    ("hook", "hidden-hook"): RuleReport(NOTE, "Ein Repo-eigener Commit-Schritt wird verdeckt.", area_note="ein Repo-eigener Commit-Schritt wird verdeckt"),
    ("secrets", FindingRule.LOOSE_MODE.value): RuleReport(ACTION, "Ein Geheimnis ist für andere lesbar.", HOME_NEXT, resolved="Ein Geheimnis ist nicht mehr für andere lesbar."),
    ("secrets", FindingRule.SYMLINKED_FOLDER.value): RuleReport(ACTION, "Ein Geheimnis liegt hinter einem Verweis.", HOME_NEXT, resolved="Ein Geheimnis liegt nicht mehr hinter einem Verweis."),
    ("secrets", FindingRule.REVOCATION_UNCONFIRMED.value): RuleReport(
        ACTION, "Ein ausgemusterter Schlüssel ist noch nicht als widerrufen bestätigt.", HOME_NEXT, resolved="Ein ausgemusterter Schlüssel ist als widerrufen bestätigt."
    ),
    ("secrets", FindingRule.VERIFICATION_FAILED.value): RuleReport(ACTION, "Eine Anmeldung gilt nicht mehr; was sie nutzt, scheitert.", HOME_NEXT, resolved="Eine Anmeldung gilt wieder."),
    ("secrets", MISSING_PASSWORD_RULE): RuleReport(ACTION, "Das Passwort der Sicherung fehlt; ohne es lässt sich nichts wiederherstellen.", HOME_NEXT, resolved="Das Passwort der Sicherung ist wieder da."),
    ("secrets", FindingRule.MISSING.value): RuleReport(NOTE, "Zugangsdaten{readers} fehlen noch."),
    ("secrets", FindingRule.NOT_A_FOLDER.value): SECRET_UNFIT_FOLDER,
    ("secrets", FindingRule.CLOSED_FOLDER.value): SECRET_UNFIT_FOLDER,
    ("secrets", FindingRule.PRESENCE_UNKNOWN.value): RuleReport(NOTE, "Ob ein Geheimnis da ist, lässt sich nicht sehen.", area_note="ob ein Geheimnis da ist, lässt sich nicht sehen"),
    ("secrets", FindingRule.VERIFICATION_UNREACHABLE.value): RuleReport(NOTE, "Eine Anmeldung ließ sich gerade nicht prüfen.", area_note="eine Anmeldung ließ sich gerade nicht prüfen"),
    ("secrets", FindingRule.UNKNOWN_FILE.value): RuleReport(NOTE, "Es gibt unbekannte Dateien bei den Geheimnissen.", area_note="es gibt unbekannte Dateien bei den Geheimnissen"),
    ("logs", "container-json-file"): LOGS_NOT_ROTATED,
    ("logs", "docker-default-driver"): LOGS_NOT_ROTATED,
    ("logs", "rsyslog-file"): LOGS_NOT_ROTATED,
    ("logs", "journal-age"): RuleReport(ACTION, "Es gibt Logs, die älter als 30 Tage sind.", SETUP_APPLY, resolved="Es gibt keine Logs mehr, die älter als 30 Tage sind."),
    ("logs", "journal-unreadable"): LOGS_UNSEEN,
    ("logs", "docker-unreadable"): LOGS_UNSEEN,
}
# Nothing `home tidy` finds is broken: its rules clean or list, so the whole area is a note.
TIDY_REPORT = RuleReport(NOTE, "Es gibt etwas aufzuräumen.", area_note="es gibt etwas aufzuräumen")
BROKEN_CHECK_REPORT = RuleReport(ACTION, "Die Prüfung {area} lief nicht.", "Führe `home check` aus.", resolved="Die Prüfung {area} läuft wieder.")
# The areas in the order the mail names them, by the check that owns each.
AREAS = (
    ("platform", "Dienste"),
    ("backup", "Sicherung"),
    ("hook", "Commit-Schutz"),
    ("secrets", "Geheimnisse"),
    ("logs", "Server-Logs"),
    ("tidy", "Aufräumen"),
)
AREA_LABELS = dict(AREAS)


@dataclass(frozen=True)
class Item:
    """One remembered identity, told what it says and how much it matters."""

    identity: str
    check: str
    report: RuleReport
    sentence: str
    next_step: str
    area_note: str
    resolved: str

    @property
    def level(self) -> Level:
        return self.report.level


def _rule_report(check_name: str, rule: str) -> RuleReport | None:
    if rule == CHECK_FAILED_IDENTITY:
        return BROKEN_CHECK_REPORT
    return TIDY_REPORT if check_name == "tidy" else RULE_REPORTS.get((check_name, rule))


APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
NO_READERS: Mapping[str, str] = MappingProxyType({})


def _item(identity: str, readers: Mapping[str, str]) -> Item:
    check_name, rule, subject = [*identity.split("\t", 2), ""][:3]
    report = _rule_report(check_name, rule)
    assert report is not None, identity
    words = {"subject": subject, "area": AREA_LABELS[check_name], "readers": f" für {readers[subject]}" if readers.get(subject) else ""}
    note = (report.area_note or report.sentence).removesuffix(".")
    return Item(identity, check_name, report, report.sentence.format(**words), report.next_step.format(**words), note.format(**words), report.resolved.format(**words))


def _items(identities: frozenset[str], readers: Mapping[str, str] = NO_READERS) -> list[Item]:
    """In the order of the mail: area, then the rule and subject the identity starts with."""
    area_order = {name: position for position, (name, _) in enumerate(AREAS)}
    return sorted((_item(identity, readers) for identity in identities), key=lambda item: (area_order[item.check], item.identity))


def _actions(identities: frozenset[str] | None) -> frozenset[str]:
    return frozenset(item.identity for item in _items(identities or frozenset()) if item.level is Level.ACTION)


def _is_news(current: frozenset[str], previous: frozenset[str] | None) -> bool:
    """An action that appeared or went is news, a broken or recovered check included; notes alone never are."""
    return _actions(current) != _actions(previous)


def _area_level(items: list[Item], check_name: str) -> Level:
    return max((item.level for item in items if item.check == check_name), default=Level.OK)


def title(current: frozenset[str]) -> str:
    """The state of the PC in one phrase; at action it names the areas."""
    items = _items(current)
    levels = {name: _area_level(items, name) for name, _ in AREAS}
    action_areas = [label for name, label in AREAS if levels[name] is Level.ACTION]
    if action_areas:
        return f"PC-Check: {Level.ACTION.label} – {', '.join(action_areas)}"
    return "PC-Check: in Ordnung – nur Hinweise" if Level.NOTE in levels.values() else "PC-Check: alles in Ordnung"


MAX_ITEM_LINES = 20
MORE_ITEMS = "… und {count} weitere, siehe Bericht"


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_ITEM_LINES:
        return lines
    return [*lines[:MAX_ITEM_LINES], MORE_ITEMS.format(count=len(lines) - MAX_ITEM_LINES)]


def _action_lines(current: frozenset[str], previous: frozenset[str] | None) -> list[str]:
    before = _actions(previous)
    lines = [
        f"{number}. {item.sentence}{'' if item.identity in before else ' (neu)'}\n   Nächster Schritt: {item.next_step}"
        for number, item in enumerate((item for item in _items(current) if item.level is Level.ACTION), start=1)
    ]
    return _capped(lines)


def _check_and_subject(identity: str) -> tuple[str, str]:
    check_name, _rule, subject = [*identity.split("\t", 2), ""][:3]
    return check_name, subject


def _gone_lines(current: frozenset[str], previous: frozenset[str] | None) -> list[str]:
    """A subject that still has another action (a restarting container that now exited) is not reported as done."""
    still_named = {_check_and_subject(identity) for identity in current}
    gone = frozenset(identity for identity in _actions(previous) - current if _check_and_subject(identity) not in still_named)
    return _capped([f"- {item.resolved}" for item in _items(gone)])


@dataclass(frozen=True)
class MailFacts:
    """What the mail says beyond the findings: who reads each secret (a manifest fact) and when the last backup succeeded."""

    readers: Mapping[str, str] = NO_READERS
    last_backup: datetime | None = None


def _when(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(), now.astimezone().date()
    day = {today: "heute", today - timedelta(days=1): "gestern"}.get(local.date(), f"{local:%d.%m.%Y}")
    return f"{day} {local:%H:%M}"


def _area_line(items: list[Item], check_name: str, facts: MailFacts, now: datetime) -> str:
    level = _area_level(items, check_name)
    notes = [item.area_note for item in items if item.check == check_name and item.level is Level.NOTE]
    detail = f" – {notes[0]}" if level is Level.NOTE else ""
    if check_name == "backup" and level is Level.OK and facts.last_backup:
        detail = f", zuletzt {_when(facts.last_backup, now)}"
    return f"{AREA_LABELS[check_name]}: {level.label}{detail}"


def notification_body(current: frozenset[str], report_file: str) -> str:
    """The sentences of the actions without their steps, and where the report is."""
    sentences = _capped([item.sentence for item in _items(current) if item.level is Level.ACTION])
    return "\n".join([*sentences, f"Alles im Einzelnen: {report_file}"])


def mail_body(current: frozenset[str], previous: frozenset[str] | None, report_file: str, now: datetime, facts: MailFacts) -> str:
    """The actions with one step each, what is no longer an action, and one line per area; no number, no secret, no home file."""
    items = _items(current, facts.readers)
    sections = []
    if actions := _action_lines(current, previous):
        sections.append("Handlungsbedarf\n\n" + "\n\n".join(actions))
    if gone := _gone_lines(current, previous):
        sections.append("Erledigt\n\n" + "\n".join(gone))
    area_lines = "\n".join(_area_line(items, name, facts, now) for name, _ in AREAS)
    sections.append(f"So steht der PC ({now:%d.%m.%Y %H:%M})\n\n{area_lines}")
    hint = "Hinweise eilen nicht; home next zeigt, was davon du tun kannst.\n" if any(item.level is Level.NOTE for item in items) else ""
    sections.append(f"{hint}Alles im Einzelnen: {report_file}")
    return "\n\n".join(sections) + "\n"


MAIL_FAILED = "home: msmtp fehlgeschlagen ({host}:{port}); der nächste Lauf meldet die geänderten Funde erneut.\n"
MAIL_ACCOUNT_INVALID = "home: keine Mail, {problem}; der nächste Lauf meldet die geänderten Funde erneut.\n"
GRAPHICAL_SESSION_PROBE = ["systemctl", "--user", "is-active", "--quiet", "graphical-session.target"]


def _mail_delivered(ctx: CheckContext, subject: str, body: str) -> bool | None:
    """None while mail is not set up; False (after one stderr line) when the account is invalid or msmtp did not deliver."""
    try:
        account = mail.load_account(ctx.layout)
    except mail.AccountInvalid as error:
        sys.stderr.write(MAIL_ACCOUNT_INVALID.format(problem=error))
        return False
    if account is None:
        return None
    delivered = ctx.run(mail.sendmail_command(account, ctx.layout.secrets_dir, mail.compose(account, subject, body))) is not None
    if not delivered:
        sys.stderr.write(MAIL_FAILED.format(host=account.host, port=account.port))
    return delivered


def _desktop_delivered(ctx: CheckContext, summary: str, body: str) -> bool | None:
    """None without a graphical session; False (after one stderr line) when the notification was not delivered."""
    if ctx.run(GRAPHICAL_SESSION_PROBE) is None:
        return None
    delivered = _notify(ctx.run, summary, body, ctx.layout.check_report_file)
    if not delivered:
        sys.stderr.write(NOTIFY_FAILED)
    return delivered


def _mail_facts(ctx: CheckContext) -> MailFacts:
    """A manifest that cannot be read leaves the app names out; the secrets check reports that failure itself.

    `read_by` is free text, so only a reader that is a bare app or service name reaches the mail (no file, path or secret name).
    """
    try:
        manifest = secrets_view.load_manifest(secrets_view.MANIFEST_PATH)
    except secrets_view.ManifestError:
        manifest = ()
    readers = {entry.name: ", ".join(reader for reader in entry.read_by if APP_NAME.fullmatch(reader)) for entry in manifest}
    return MailFacts(readers, backup_check.last_successful_backup(ctx.layout.home))


def _news_delivered(ctx: CheckContext, current: frozenset[str], previous: frozenset[str] | None) -> bool:
    """Every set-up channel is attempted; the news counts as delivered when at least one is set up and all of those delivered."""
    home = ctx.layout.home
    report_file = f"~/{ctx.layout.check_report_file.relative_to(home)}"
    summary = title(current)
    outcomes = [
        _desktop_delivered(ctx, summary, notification_body(current, report_file)),
        _mail_delivered(ctx, summary, mail_body(current, previous, report_file, ctx.now, _mail_facts(ctx))),
    ]
    set_up = [outcome for outcome in outcomes if outcome is not None]
    return bool(set_up) and all(set_up)


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
    # The remembered set stays at the last delivered one, so the next run sends this news again.
    if not news or _news_delivered(ctx, current, previous):
        _remember(layout, current)
    failed = [result for result in results if result.error]
    if failed:
        raise _run_error(failed)
    return 1 if current else 0


CHECK_EXAMPLES = """\
Beispiele:
  home check    alle Prüfungen, ein Bericht, höchstens eine Benachrichtigung und eine Mail

Meldet (Desktop-Benachrichtigung; Mail über msmtp, wenn ~/.secrets/smtp/account.toml da ist) nur, wenn ein Handlungsbedarf neu auftaucht
oder verschwindet; Hinweise allein melden nichts. Bericht: ~/.local/state/home/check-report.md
Exit-Code: 0 sauber, 1 Funde, 2 Konfiguration oder Manifest ungültig, 3 unerwarteter Fehler.
"""


def _check_from_args(_args: argparse.Namespace, layout: Layout, config: Config, ports: Ports) -> int:
    return check(CheckContext(layout, config, ports.run, ports.probe, ports.now))


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "check",
        help="alle Prüfungen ohne Änderung; meldet nur, wenn ein Handlungsbedarf neu auftaucht oder verschwindet",
        description="Führt jede Prüfung von home lesend aus und schreibt einen Bericht.",
        epilog=CHECK_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=_check_from_args)
