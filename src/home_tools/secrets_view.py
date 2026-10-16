"""`home secrets` shows every secret the manifest names, and never a value.

It reads secret files by `stat` only and never opens them. The one probe runs
`gh auth status`, keeps nothing but its exit code and kills its whole process
group when it hangs past a timeout or the run is stopped (Ctrl-C, SIGTERM, SIGHUP).
Without options it only displays: it writes nothing but its report under
~/.local/state/home/. Anything under ~/.secrets the manifest does not name is
reported as unknown and left alone; loose modes are reported, never changed.

Exit code: 0 when nothing needs attention, 1 when a finding exists, 2 when the
manifest is invalid, 3 when the run broke on an unexpected error.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import os
import signal
import stat
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePosixPath

from home_tools.report import findings_count, markdown_table, write_state_file
from home_tools.tidy_home import Config, Layout

MANIFEST_PATH = Path(__file__).with_name("secrets_manifest.toml")

ExitCodeProbe = Callable[[Sequence[str]], int | None]
PROBE_TIMEOUT_SECONDS = 20
PROBE_KILL_GRACE_SECONDS = 2
PROBE_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

HOME_PREFIX = "~/"
WILDCARD_CHARACTERS = frozenset("*?[")
LOOSEST_FILE_MODE = 0o600
LOOSEST_DIRECTORY_MODE = 0o700
EXPIRY_UNKNOWN = "Ablauf unbekannt"
REVOCATION_UNCONFIRMED = "Widerruf unbestätigt"
RETIRED = "zurückgezogen"


class SecretKind(Enum):
    """How a secret is recreated; this decides what a later wizard does with it."""

    HUMAN_PASSWORD = "human-password"
    TOOL_LOGIN = "tool-login"
    TOKEN = "token"
    SERVICE_PASSWORD = "service-password"
    DERIVED_MIRROR = "derived-mirror"

    @property
    def label(self) -> str:
        return KIND_LABELS[self]


KIND_LABELS = {
    SecretKind.HUMAN_PASSWORD: "Passwort (Mensch)",
    SecretKind.TOOL_LOGIN: "Tool-Login",
    SecretKind.TOKEN: "Token",
    SecretKind.SERVICE_PASSWORD: "Dienstpasswort",
    SecretKind.DERIVED_MIRROR: "abgeleitete Kopie",
}


class Presence(Enum):
    PRESENT = "vorhanden"
    MISSING = "fehlt"
    UNKNOWN = "unbekannt"


class Verification(Enum):
    PASSED = "geprüft: gültig"
    FAILED = "geprüft: ungültig"
    UNREACHABLE = "Prüfung nicht möglich"
    NOT_CHECKED = "nicht geprüft"


class Verifier(Enum):
    """Every probe `home secrets` may run, spelled as the command it is."""

    GH_AUTH_STATUS = "gh auth status"

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(self.value.split())


@dataclass(frozen=True)
class SecretEntry:
    """One manifest entry: names, places and links, never a value."""

    name: str
    kind: SecretKind
    paths: tuple[PurePosixPath, ...]
    read_by: tuple[str, ...]
    recreate: str
    verifier: Verifier | None = None
    retired_on: date | None = None
    revoke_url: str = ""
    mirror_of: str = ""

    @property
    def retired(self) -> bool:
        return self.retired_on is not None


class ManifestError(ValueError):
    """The manifest cannot be trusted; `home secrets` exits 2 on it."""


# --- manifest --------------------------------------------------------------

REQUIRED_KEYS = frozenset({"name", "kind", "paths", "read_by", "recreate"})
OPTIONAL_KEYS = frozenset({"verify", "retired_on", "revoke_url", "mirror_of"})


def _fail(path: Path, where: str, message: str) -> ManifestError:
    return ManifestError(f"{path}: {where}: {message}")


def _string(path: Path, where: str, raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, where, f"{key} must be a non-empty string")
    return value


def _strings(path: Path, where: str, raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _fail(path, where, f"{key} must be a list of non-empty strings")
    return tuple(value)


def _has_wildcard(name: str) -> bool:
    return bool(WILDCARD_CHARACTERS & set(name))


def _home_relative(path: Path, where: str, text: str) -> PurePosixPath:
    """'~/.secrets/x/*.json' becomes '.secrets/x/*.json'; only the last part may hold a wildcard."""
    relative = PurePosixPath(text.removeprefix(HOME_PREFIX))
    escapes = not text.startswith(HOME_PREFIX) or relative.is_absolute() or ".." in relative.parts or not relative.parts
    if escapes or any(_has_wildcard(part) for part in relative.parent.parts):
        raise _fail(path, where, "every paths entry must start with '~/', stay inside the home and keep wildcards in its last part")
    return relative


def _patterns(path: Path, where: str, raw: Mapping[str, object]) -> tuple[PurePosixPath, ...]:
    texts = _strings(path, where, raw, "paths")
    if not texts:
        raise _fail(path, where, "paths must name at least one place")
    return tuple(_home_relative(path, where, text) for text in texts)


def _enum_value[E: Enum](path: Path, where: str, raw: Mapping[str, object], key: str, enum: type[E]) -> E:
    try:
        return enum(raw[key])
    except ValueError:
        allowed = ", ".join(repr(member.value) for member in enum)
        raise _fail(path, where, f"{key} must be one of {allowed}") from None


def _retirement(path: Path, where: str, raw: Mapping[str, object]) -> tuple[date | None, str]:
    """A retired entry always carries the page where it is revoked, and only a retired one does."""
    if ("retired_on" in raw) != ("revoke_url" in raw):
        raise _fail(path, where, "retired_on and revoke_url come together or not at all")
    if "retired_on" not in raw:
        return None, ""
    retired_on = raw["retired_on"]
    if not isinstance(retired_on, date) or isinstance(retired_on, datetime):
        raise _fail(path, where, "retired_on must be a date like 2026-10-14")
    revoke_url = _string(path, where, raw, "revoke_url")
    if not revoke_url.startswith("https://"):
        raise _fail(path, where, "revoke_url must be an https:// link")
    return retired_on, revoke_url


def _entry(path: Path, index: int, raw: object) -> SecretEntry:
    where = f"[[secret]] #{index + 1}"
    if not isinstance(raw, dict):
        raise _fail(path, where, "must be a table")
    if isinstance(raw.get("name"), str):
        where = f"[[secret]] {raw['name']!r}"
    missing = sorted(REQUIRED_KEYS - set(raw))
    unknown = sorted(set(raw) - REQUIRED_KEYS - OPTIONAL_KEYS)
    if missing or unknown:
        raise _fail(path, where, f"missing key(s) {missing}, unknown key(s) {unknown}")
    kind = _enum_value(path, where, raw, "kind", SecretKind)
    if (kind is SecretKind.DERIVED_MIRROR) != ("mirror_of" in raw):
        raise _fail(path, where, "mirror_of belongs to every derived-mirror entry and to no other kind")
    retired_on, revoke_url = _retirement(path, where, raw)
    return SecretEntry(
        name=_string(path, where, raw, "name"),
        kind=kind,
        paths=_patterns(path, where, raw),
        read_by=_strings(path, where, raw, "read_by"),
        recreate=_string(path, where, raw, "recreate"),
        verifier=_enum_value(path, where, raw, "verify", Verifier) if "verify" in raw else None,
        retired_on=retired_on,
        revoke_url=revoke_url,
        mirror_of=_string(path, where, raw, "mirror_of") if "mirror_of" in raw else "",
    )


def _require_consistent(path: Path, entries: tuple[SecretEntry, ...]) -> None:
    names = Counter(entry.name for entry in entries)
    duplicates = sorted(name for name, count in names.items() if count > 1)
    if duplicates:
        raise _fail(path, "[[secret]]", f"name(s) used twice: {', '.join(duplicates)}")
    for entry in entries:
        if entry.mirror_of and entry.mirror_of not in names:
            raise _fail(path, f"[[secret]] {entry.name!r}", f"mirror_of names no entry: {entry.mirror_of!r}")


def load_manifest(path: Path) -> tuple[SecretEntry, ...]:
    """The manifest names every secret; an unknown key, a bad value or a dangling reference is an error naming the file and the key."""
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ManifestError(f"{path}: {error}") from None
    unknown = sorted(set(raw) - {"secret"})
    if unknown or not isinstance(raw.get("secret"), list):
        raise _fail(path, "top level", f"holds only a [[secret]] array, found key(s) {unknown or sorted(raw)}")
    entries = tuple(_entry(path, index, item) for index, item in enumerate(raw["secret"]))
    _require_consistent(path, entries)
    return entries


# --- the filesystem and probe boundaries ---------------------------------


@dataclass(frozen=True)
class PathFact:
    """What `stat` says about one path: where it is, relative to the home, and its st_mode."""

    relative: PurePosixPath
    mode: int


def _raise(error: OSError) -> None:
    raise error


def scan_secrets_tree(layout: Layout) -> tuple[PathFact, ...]:
    """Every path under ~/.secrets, the directory itself included, by lstat; nothing is opened."""
    root = layout.secrets_dir
    if not root.is_dir():
        return ()
    facts = [PathFact(root.relative_to(layout.home), root.lstat().st_mode)]
    for directory, subdirectories, files in os.walk(root, onerror=_raise):
        for name in sorted(subdirectories + files):
            path = Path(directory) / name
            facts.append(PathFact(path.relative_to(layout.home), path.lstat().st_mode))
    return tuple(sorted(facts, key=lambda fact: fact.relative))


def stat_matches(layout: Layout, pattern: PurePosixPath) -> tuple[PathFact, ...] | None:
    """The paths a manifest pattern names, by stat; None when the filesystem refuses to say."""
    try:
        if _has_wildcard(pattern.name):
            names = sorted(name for name in os.listdir(layout.home / pattern.parent) if fnmatch.fnmatchcase(name, pattern.name))
            matched = [pattern.parent / name for name in names]
        else:
            matched = [pattern]
        return tuple(PathFact(relative, (layout.home / relative).stat().st_mode) for relative in matched)
    except FileNotFoundError:
        return ()
    except OSError:
        return None


def probe_exit_code(argv: Sequence[str], timeout: float = PROBE_TIMEOUT_SECONDS) -> int | None:
    """Run a probe and keep only its exit code; None when the tool is missing or hangs. Its output is never read.

    However the wait ends without an exit code, by the timeout, Ctrl-C or a stop signal, the probe dies with everything it started.
    """
    with _StopsAroundTheProbe() as stops:
        try:
            probe = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
        except FileNotFoundError:
            return None
        try:
            with stops.ending_the_run():
                return probe.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_probe_session(probe)
            return None
        except BaseException:
            _kill_probe_session(probe)
            raise


class _StopsAroundTheProbe:
    """The probe leads its own session, so a stop sent to home's process group never reaches it; home has to kill it.

    While the probe runs, Ctrl-C, SIGTERM and SIGHUP therefore end the run only during the wait, once the probe is in hand.
    A stop that arrives while the probe is being started or killed is held until that step is done, so it can neither
    orphan a probe that was just spawned nor cut its kill short. A stop the caller chose to ignore stays ignored.
    """

    def __init__(self) -> None:
        self._previous: dict[int, Callable[[int, object], object] | int | None] = {}
        self._held: list[int] = []
        self._ending_the_run = False

    def __enter__(self) -> _StopsAroundTheProbe:
        for stop in PROBE_STOP_SIGNALS:
            handler = signal.getsignal(stop)
            if handler is not signal.SIG_IGN:
                self._previous[stop] = handler
                signal.signal(stop, self._on_stop)
        return self

    def __exit__(self, error_type: type[BaseException] | None, _error: object, _traceback: object) -> None:
        for stop, handler in self._previous.items():
            signal.signal(stop, handler)
        if error_type is None:
            self._end_the_run_on_a_held_stop()

    @contextlib.contextmanager
    def ending_the_run(self) -> Iterator[None]:
        self._ending_the_run = True
        try:
            self._end_the_run_on_a_held_stop()
            yield
        finally:
            self._ending_the_run = False

    def _on_stop(self, signum: int, _frame: object) -> None:
        if self._ending_the_run:
            raise _stopped(signum)
        self._held.append(signum)

    def _end_the_run_on_a_held_stop(self) -> None:
        if self._held:
            raise _stopped(self._held[0])


def _stopped(signum: int) -> KeyboardInterrupt | SystemExit:
    if signum == signal.SIGINT:
        return KeyboardInterrupt()
    return SystemExit(128 + signum)  # the shell's code for a run ended by that signal, e.g. 143 for SIGTERM


def _kill_probe_session(probe: subprocess.Popen[bytes]) -> None:
    """The probe leads its own session, so its process group also holds every process it started in the background.

    A stop can land just after the probe ended; its group is then gone, or holds only what it left behind.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(probe.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        probe.wait(timeout=PROBE_KILL_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(probe.pid, signal.SIGKILL)
    probe.wait()


@dataclass(frozen=True)
class Observation:
    """Everything the boundaries saw; the view is computed from it alone."""

    tree: tuple[PathFact, ...]
    matches: Mapping[PurePosixPath, tuple[PathFact, ...] | None]
    probe_codes: Mapping[Verifier, int | None]


def observe(manifest: tuple[SecretEntry, ...], layout: Layout, probe: ExitCodeProbe) -> Observation:
    verifiers = {entry.verifier for entry in manifest if entry.verifier and not entry.retired}
    return Observation(
        tree=scan_secrets_tree(layout),
        matches={pattern: stat_matches(layout, pattern) for entry in manifest for pattern in entry.paths},
        probe_codes={verifier: probe(verifier.argv) for verifier in sorted(verifiers, key=lambda v: v.value)},
    )


# --- the pure view --------------------------------------------------------


class FindingRule(Enum):
    MISSING = "missing"
    PRESENCE_UNKNOWN = "presence-unknown"
    UNKNOWN_FILE = "unknown-file"
    LOOSE_MODE = "loose-mode"
    REVOCATION_UNCONFIRMED = "revocation-unconfirmed"
    VERIFICATION_FAILED = "verification-failed"
    VERIFICATION_UNREACHABLE = "verification-unreachable"

    @property
    def recommendation(self) -> str:
        return RECOMMENDATIONS[self]


RECOMMENDATIONS = {
    FindingRule.MISSING: "neu anlegen, siehe Tabelle",
    FindingRule.PRESENCE_UNKNOWN: "Rechte des Elternordners prüfen",
    FindingRule.UNKNOWN_FILE: "unbekannt: ins Manifest eintragen oder von Hand löschen",
    FindingRule.LOOSE_MODE: "von Hand enger setzen; home ändert keine Rechte",
    FindingRule.REVOCATION_UNCONFIRMED: "beim Anbieter widerrufen, siehe Tabelle",
    FindingRule.VERIFICATION_FAILED: "neu anmelden, siehe Tabelle",
    FindingRule.VERIFICATION_UNREACHABLE: "Prüfbefehl hing oder fehlt; später erneut",
}


@dataclass(frozen=True)
class SecretFinding:
    rule: FindingRule
    subject: str
    detail: str = ""

    @property
    def identity(self) -> str:
        """What the finding is about; `home check` notifies when this set changes."""
        return f"{self.rule.value}\t{self.subject}"


@dataclass(frozen=True)
class SecretStatus:
    entry: SecretEntry
    presence: Presence
    verification: Verification


@dataclass(frozen=True)
class SecretsView:
    statuses: tuple[SecretStatus, ...]
    findings: tuple[SecretFinding, ...]


def _presence(entry: SecretEntry, matches: Mapping[PurePosixPath, tuple[PathFact, ...] | None]) -> Presence:
    found = [matches[pattern] for pattern in entry.paths]
    if any(facts is None for facts in found):
        return Presence.UNKNOWN
    return Presence.PRESENT if all(found) else Presence.MISSING


def _verification(entry: SecretEntry, probe_codes: Mapping[Verifier, int | None]) -> Verification:
    if entry.verifier is None or entry.retired:
        return Verification.NOT_CHECKED
    code = probe_codes[entry.verifier]
    if code is None:
        return Verification.UNREACHABLE
    return Verification.PASSED if code == 0 else Verification.FAILED


def _matches_pattern(relative: PurePosixPath, pattern: PurePosixPath) -> bool:
    return relative.parent == pattern.parent and fnmatch.fnmatchcase(relative.name, pattern.name)


def _is_named(relative: PurePosixPath, patterns: tuple[PurePosixPath, ...]) -> bool:
    """Named are the pattern's matches, everything inside a match, and the directories that lead to a pattern."""
    candidates = (relative, *relative.parents)
    return any(relative in pattern.parents or any(_matches_pattern(c, pattern) for c in candidates) for pattern in patterns)


def _loosest_allowed(mode: int) -> int | None:
    if stat.S_ISDIR(mode):
        return LOOSEST_DIRECTORY_MODE
    return LOOSEST_FILE_MODE if stat.S_ISREG(mode) else None


def _mode_findings(tree: tuple[PathFact, ...]) -> Iterator[SecretFinding]:
    for fact in tree:
        allowed = _loosest_allowed(fact.mode)
        permissions = stat.S_IMODE(fact.mode)
        if allowed is not None and permissions & ~allowed:
            yield SecretFinding(FindingRule.LOOSE_MODE, f"{HOME_PREFIX}{fact.relative}", f"{permissions:o}, höchstens {allowed:o}")


def _entry_findings(status: SecretStatus) -> Iterator[SecretFinding]:
    name = status.entry.name
    if status.entry.retired:
        yield SecretFinding(FindingRule.REVOCATION_UNCONFIRMED, name, status.entry.revoke_url)
        return
    if status.presence is Presence.MISSING:
        yield SecretFinding(FindingRule.MISSING, name)
    if status.presence is Presence.UNKNOWN:
        yield SecretFinding(FindingRule.PRESENCE_UNKNOWN, name)
    if status.verification is Verification.FAILED:
        yield SecretFinding(FindingRule.VERIFICATION_FAILED, name, status.entry.verifier.value)
    if status.verification is Verification.UNREACHABLE:
        yield SecretFinding(FindingRule.VERIFICATION_UNREACHABLE, name, status.entry.verifier.value)


def build_view(manifest: tuple[SecretEntry, ...], observation: Observation) -> SecretsView:
    """Pure: the statuses and findings follow from the manifest and what the boundaries saw, nothing else."""
    statuses = tuple(
        SecretStatus(entry, _presence(entry, observation.matches), _verification(entry, observation.probe_codes)) for entry in manifest
    )
    patterns = tuple(pattern for entry in manifest for pattern in entry.paths)
    unknown = (
        SecretFinding(FindingRule.UNKNOWN_FILE, f"{HOME_PREFIX}{fact.relative}")
        for fact in observation.tree
        if not _is_named(fact.relative, patterns)
    )
    findings = (*(f for status in statuses for f in _entry_findings(status)), *unknown, *_mode_findings(observation.tree))
    return SecretsView(statuses, findings)


# --- rendering ------------------------------------------------------------

NO_VALUES_NOTE = "Keine Werte: home secrets liest Geheimnisdateien nur per stat und öffnet sie nie."


def _status_row(status: SecretStatus) -> list[str]:
    entry = status.entry
    places = ", ".join(f"{HOME_PREFIX}{pattern}" for pattern in entry.paths)
    if entry.retired:
        return [entry.name, entry.kind.label, RETIRED, REVOCATION_UNCONFIRMED, EXPIRY_UNKNOWN, places, f"Widerruf: {entry.revoke_url}"]
    presence, verification = status.presence.value, status.verification.value
    return [entry.name, entry.kind.label, presence, verification, EXPIRY_UNKNOWN, places, entry.recreate]


def render_view(view: SecretsView, now: datetime) -> str:
    header = f"# home secrets {now:%d.%m.%Y %H:%M} | {findings_count(len(view.findings))}"
    status_headers = ["Name", "Art", "Datei", "Prüfung", "Ablauf", "Ort", "Neu anlegen"]
    lines = [header, "", *markdown_table(status_headers, [_status_row(status) for status in view.statuses]), ""]
    if view.findings:
        rows = [[f.rule.value, f.subject, f.detail, f.rule.recommendation] for f in view.findings]
        lines += ["## Funde", "", *markdown_table(["Regel", "Gegenstand", "Befund", "Empfehlung"], rows), ""]
    else:
        lines += ["Keine Funde.", ""]
    lines.append(NO_VALUES_NOTE)
    return "\n".join(lines) + "\n"


def services_view(manifest: tuple[SecretEntry, ...]) -> dict[str, tuple[str, ...]]:
    """Pure: the manifest's read_by, inverted and grouped per service."""
    readers: dict[str, list[str]] = {}
    for entry in manifest:
        for service in entry.read_by:
            readers.setdefault(service, []).append(entry.name)
    return {service: tuple(names) for service, names in sorted(readers.items())}


BY_SERVICE_NOTE = "Was das Manifest als Leser nennt; nicht bewiesen vollständig. Ein Leser wird nicht automatisch neu gestartet."


def render_by_service(manifest: tuple[SecretEntry, ...]) -> str:
    rows = [[service, ", ".join(names)] for service, names in services_view(manifest).items()]
    mirrors = [[entry.mirror_of, entry.name] for entry in manifest if entry.mirror_of]
    lines = ["# home secrets --by-service | deklarierte Abhängigkeiten", "", BY_SERVICE_NOTE, ""]
    lines += [*markdown_table(["Dienst", "liest"], rows), ""]
    if mirrors:
        lines += ["## Abgeleitete Kopien", "", *markdown_table(["Quelle", "Kopie"], mirrors), ""]
    return "\n".join(lines)


# --- entry point ------------------------------------------------------------


def inspect(layout: Layout, probe: ExitCodeProbe, now: datetime) -> tuple[SecretsView, str]:
    """What `home secrets` without options shows; `home check` reads the secrets through this."""
    manifest = load_manifest(MANIFEST_PATH)
    view = build_view(manifest, observe(manifest, layout, probe))
    return view, render_view(view, now)


def show(layout: Layout, probe: ExitCodeProbe, now: datetime) -> int:
    view, report = inspect(layout, probe, now)
    write_state_file(layout.home, layout.secrets_report_file, report)
    sys.stdout.write(report)
    return 1 if view.findings else 0


def show_by_service() -> int:
    sys.stdout.write(render_by_service(load_manifest(MANIFEST_PATH)))
    return 0


SECRETS_EXAMPLES = """\
Beispiele:
  home secrets                zeigt jedes Geheimnis des Manifests: Datei, Prüfung, Ablauf
  home secrets --by-service   welcher Dienst welches Geheimnis liest (deklariert)

Zeigt nie einen Wert und ändert nichts außer dem Bericht
~/.local/state/home/secrets-report.md. Braucht kein Terminal, Agenten dürfen es fahren.
Exit-Code: 0 sauber, 1 Funde, 2 Manifest ungültig, 3 unerwarteter Fehler.
"""


def _secrets_from_args(args: argparse.Namespace, layout: Layout, _config: Config, _run: object, probe: ExitCodeProbe, now: datetime) -> int:
    return show_by_service() if args.by_service else show(layout, probe, now)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "secrets",
        help="Geheimnisse zeigen: vorhanden, geprüft, Ablauf; nie ein Wert",
        description="Zeigt jedes Geheimnis aus dem Manifest, liest Dateien nur per stat und ändert nichts.",
        epilog=SECRETS_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--by-service", action="store_true", help="gruppiert nach Dienst: deklarierte Abhängigkeiten")
    parser.set_defaults(handler=_secrets_from_args)
