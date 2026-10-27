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

import contextlib
import fnmatch
import os
import shlex
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
from functools import reduce
from operator import and_
from pathlib import Path, PurePosixPath

from home_tools.layout import Layout
from home_tools.report import ClosedBy, Tally, findings_count, markdown_table, write_state_file
from home_tools.tunnel_config import as_wildcard, with_tunnel_id

MANIFEST_PATH = Path(__file__).with_name("secrets_manifest.toml")

ExitCodeProbe = Callable[[Sequence[str]], int | None]
PROBE_TIMEOUT_SECONDS = 20
PROBE_KILL_GRACE_SECONDS = 2
PROBE_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

HOME_PREFIX = "~/"
WILDCARD_CHARACTERS = frozenset("*?[")
LOOSEST_FILE_MODE = 0o600
LOOSEST_DECLARABLE_FILE_MODE = 0o644  # a container user reads the file; the 0700 folder keeps everyone else out
LOOSEST_DIRECTORY_MODE = 0o700
EXPIRY_UNKNOWN = "Ablauf unbekannt"
REVOCATION_UNCONFIRMED = "Widerruf unbestätigt"
REVOCATION_CONFIRMED = "Widerruf bestätigt {date:%d.%m.%Y}"
RETIRED = "zurückgezogen"
OPTIONAL = "{presence} (optional)"


class SecretKind(Enum):
    """What an entry is and how it is recreated; this decides what a later wizard does with it.

    Documentation and configuration are the known files under ~/.secrets that hold no secret.
    """

    HUMAN_PASSWORD = "human-password"
    TOOL_LOGIN = "tool-login"
    TOKEN = "token"
    SERVICE_PASSWORD = "service-password"
    UNVERIFIED_TOKEN = "unverified-token"
    DERIVED_MIRROR = "derived-mirror"
    CONFIGURATION = "configuration"
    DOCUMENTATION = "documentation"

    @property
    def label(self) -> str:
        return KIND_LABELS[self]


KIND_LABELS = {
    SecretKind.HUMAN_PASSWORD: "Passwort (Mensch)",
    SecretKind.TOOL_LOGIN: "Tool-Login",
    SecretKind.TOKEN: "Token",
    SecretKind.SERVICE_PASSWORD: "Dienstpasswort",
    SecretKind.UNVERIFIED_TOKEN: "Token, ungeprüft",
    SecretKind.DERIVED_MIRROR: "abgeleitete Kopie",
    SecretKind.CONFIGURATION: "Konfiguration",
    SecretKind.DOCUMENTATION: "Dokumentation",
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


class Provider(Enum):
    """Where `home secrets --setup` sends the operator to create a token."""

    GITHUB = "github"
    CLOUDFLARE = "cloudflare"


@dataclass(frozen=True)
class TokenSource:
    """Where and with which rights a token is created; the page and the check behind it belong to `home secrets --setup`."""

    provider: Provider
    resource: str
    scope: tuple[str, ...]
    expires_in_days: int


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
    revocation_confirmed: date | None = None
    mirror_of: str = ""
    purpose: str = ""
    source: TokenSource | None = None
    optional: bool = False
    elsewhere: str = ""
    loosest_mode: int = LOOSEST_FILE_MODE
    loosest_mode_reason: str = ""

    @property
    def retired(self) -> bool:
        return self.retired_on is not None

    @property
    def expected_present(self) -> bool:
        """A live entry that is not optional; only its absence is a gap to close."""
        return not self.retired and not self.optional

    @property
    def written_by_setup(self) -> bool:
        """`--setup` writes a token it guides through and verifies, a token it asks hidden and stores unverified, and a service password it generates, unless that is optional: then the unit named in `recreate` writes it; every other entry is by hand."""
        return self.source is not None or self.kind in (SecretKind.SERVICE_PASSWORD, SecretKind.UNVERIFIED_TOKEN)

    @property
    def place(self) -> PurePosixPath:
        """The one file of an entry `--setup` writes; the manifest guarantees one plain path for it."""
        (path,) = self.paths
        return path


class ManifestError(ValueError):
    """The manifest cannot be trusted; `home secrets` exits 2 on it."""


# --- manifest --------------------------------------------------------------

REQUIRED_KEYS = frozenset({"name", "kind", "paths", "read_by", "recreate"})
SOURCE_KEYS = frozenset({"provider", "resource", "scope", "expires_in_days"})
RETIREMENT_KEYS = frozenset({"retired_on", "revoke_url", "revocation_confirmed"})
MODE_KEYS = frozenset({"loosest_mode", "loosest_mode_reason"})
OPTIONAL_KEYS = frozenset({"verify", "mirror_of", "purpose", "optional", "elsewhere"}) | RETIREMENT_KEYS | SOURCE_KEYS | MODE_KEYS
GITHUB_ACCESS_LEVELS = frozenset({"read", "write", "admin"})
LONGEST_EXPIRY_DAYS = 366  # GitHub's ceiling for a fine-grained token's expires_in


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


def _date(path: Path, where: str, raw: Mapping[str, object], key: str) -> date:
    value = raw[key]
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _fail(path, where, f"{key} must be a date like 2026-10-14")
    return value


@dataclass(frozen=True)
class Retirement:
    retired_on: date | None = None
    revoke_url: str = ""
    revocation_confirmed: date | None = None


def _retirement(path: Path, where: str, raw: Mapping[str, object]) -> Retirement:
    """A retired entry always carries the page where it is revoked, and only a retired one does or records a confirmed revocation."""
    if ("retired_on" in raw) != ("revoke_url" in raw):
        raise _fail(path, where, "retired_on and revoke_url come together or not at all")
    if "retired_on" not in raw:
        if "revocation_confirmed" in raw:
            raise _fail(path, where, "revocation_confirmed belongs to a retired entry only")
        return Retirement()
    revoke_url = _string(path, where, raw, "revoke_url")
    if not revoke_url.startswith("https://"):
        raise _fail(path, where, "revoke_url must be an https:// link")
    confirmed = _date(path, where, raw, "revocation_confirmed") if "revocation_confirmed" in raw else None
    return Retirement(_date(path, where, raw, "retired_on"), revoke_url, confirmed)


def _flag(path: Path, where: str, raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise _fail(path, where, f"{key} must be true or false")
    return value


def _declared_mode(path: Path, where: str, raw: Mapping[str, object]) -> tuple[int, str]:
    """The loosest mode a file of the entry may have and why; both keys together, never looser than a container user needs."""
    if set(raw) & MODE_KEYS and not set(raw) >= MODE_KEYS:
        raise _fail(path, where, "loosest_mode and loosest_mode_reason belong together")
    if "loosest_mode" not in raw:
        return LOOSEST_FILE_MODE, ""
    mode = raw["loosest_mode"]
    if not isinstance(mode, int) or isinstance(mode, bool) or mode <= 0 or mode & ~LOOSEST_DECLARABLE_FILE_MODE:
        raise _fail(path, where, f"loosest_mode must be a file mode within {LOOSEST_DECLARABLE_FILE_MODE:04o}, e.g. 0o644")
    return mode, _string(path, where, raw, "loosest_mode_reason")


def _is_github_right(right: str) -> bool:
    permission, _, level = right.partition(":")
    return bool(permission) and level in GITHUB_ACCESS_LEVELS


def _token_source(path: Path, where: str, raw: Mapping[str, object], kind: SecretKind) -> TokenSource | None:
    """A token `--setup` guides through names its provider, resource, rights and expiry together; no other kind does."""
    named = SOURCE_KEYS & set(raw)
    if not named:
        return None
    if named != SOURCE_KEYS or kind is not SecretKind.TOKEN or "purpose" not in raw:
        raise _fail(path, where, "provider, resource, scope, expires_in_days and purpose come together, on a token entry only")
    provider = _enum_value(path, where, raw, "provider", Provider)
    scope = _strings(path, where, raw, "scope")
    if not scope or (provider is Provider.GITHUB and not all(_is_github_right(right) for right in scope)):
        raise _fail(path, where, "scope must name the rights; a github right reads 'permission:read', ':write' or ':admin'")
    days = raw["expires_in_days"]
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= LONGEST_EXPIRY_DAYS:
        raise _fail(path, where, f"expires_in_days must be a whole number from 1 to {LONGEST_EXPIRY_DAYS}")
    return TokenSource(provider, _string(path, where, raw, "resource"), scope, days)


def _require_one_plain_place(path: Path, where: str, patterns: tuple[PurePosixPath, ...]) -> None:
    if len(patterns) != 1 or _has_wildcard(patterns[0].name):
        raise _fail(path, where, "paths of an entry --setup writes must be exactly one place without a wildcard")


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
    retirement = _retirement(path, where, raw)
    source = _token_source(path, where, raw, kind)
    loosest_mode, loosest_mode_reason = _declared_mode(path, where, raw)
    entry = SecretEntry(
        name=_string(path, where, raw, "name"),
        kind=kind,
        paths=_patterns(path, where, raw),
        read_by=_strings(path, where, raw, "read_by"),
        recreate=_string(path, where, raw, "recreate"),
        verifier=_enum_value(path, where, raw, "verify", Verifier) if "verify" in raw else None,
        retired_on=retirement.retired_on,
        revoke_url=retirement.revoke_url,
        revocation_confirmed=retirement.revocation_confirmed,
        mirror_of=_string(path, where, raw, "mirror_of") if "mirror_of" in raw else "",
        purpose=_string(path, where, raw, "purpose") if "purpose" in raw else "",
        source=source,
        optional=_flag(path, where, raw, "optional"),
        elsewhere=_string(path, where, raw, "elsewhere") if "elsewhere" in raw else "",
        loosest_mode=loosest_mode,
        loosest_mode_reason=loosest_mode_reason,
    )
    if entry.written_by_setup:
        _require_one_plain_place(path, where, entry.paths)
    return entry


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


def closed_to_owner(mode: int) -> bool:
    """A folder its owner cannot list, enter or write into; nothing below it can be seen or stored."""
    return stat.S_ISDIR(mode) and stat.S_IMODE(mode) & stat.S_IRWXU != stat.S_IRWXU


def _lstat_below(directory: Path, home: Path) -> Iterator[PathFact]:
    """Every path below a folder by lstat, entering only real folders open to their owner; a refused listing fails loud."""
    with os.scandir(directory) as entries:
        for entry in entries:
            fact = PathFact(Path(entry.path).relative_to(home), entry.stat(follow_symlinks=False).st_mode)
            yield fact
            if stat.S_ISDIR(fact.mode) and not closed_to_owner(fact.mode):
                yield from _lstat_below(Path(entry.path), home)


def _lstat_from(root: Path, home: Path) -> Iterator[PathFact]:
    """The place itself whatever stands there, then everything below it when it is a real folder open to its owner."""
    try:
        root_fact = PathFact(root.relative_to(home), root.lstat().st_mode)
    except FileNotFoundError:
        return
    yield root_fact
    if root.is_dir() and not closed_to_owner(root.stat().st_mode):
        yield from _lstat_below(root, home)


def scan_secrets_tree(layout: Layout) -> tuple[PathFact, ...]:
    """Every path under ~/.secrets, the place itself included, by lstat; nothing is opened, a closed folder is not entered.

    ~/.secrets itself is recorded whatever stands there, so a file or a link in its place is judged like any folder on the way.
    """
    return tuple(sorted(_lstat_from(layout.secrets_dir, layout.home), key=lambda fact: fact.relative))


def stat_matches(layout: Layout, pattern: PurePosixPath) -> tuple[PathFact, ...] | None:
    """The paths a manifest pattern names, by stat; None when the filesystem or the tunnel config refuses to say."""
    try:
        pattern = with_tunnel_id(pattern)
        wildcard = _has_wildcard(pattern.name)
        names = sorted(os.listdir(layout.home / pattern.parent)) if wildcard else []
        matched = [pattern.parent / name for name in names if fnmatch.fnmatchcase(name, pattern.name)] if wildcard else [pattern]
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
    """Each rule says who closes its findings, and how bookkeeping is counted, so a reader never decides by its name."""

    tally: Tally | None

    def __new__(cls, value: str, tally: Tally | None = None) -> FindingRule:
        rule = object.__new__(cls)
        rule._value_ = value
        rule.tally = tally
        return rule

    @property
    def closed_by(self) -> ClosedBy:
        return ClosedBy.counted_by(self.tally)

    MISSING = "missing"
    PRESENCE_UNKNOWN = "presence-unknown"
    UNKNOWN_FILE = ("unknown-file", Tally("unbekannte Datei in ~/.secrets", "unbekannte Dateien in ~/.secrets"))
    LOOSE_MODE = "loose-mode"
    SYMLINKED_FOLDER = "symlinked-folder"
    NOT_A_FOLDER = "not-a-folder"
    CLOSED_FOLDER = "closed-folder"
    REVOCATION_UNCONFIRMED = "revocation-unconfirmed"
    VERIFICATION_FAILED = "verification-failed"
    VERIFICATION_UNREACHABLE = "verification-unreachable"


# The generic remedy per rule; a rule whose remedy depends on the finding names it in SecretFinding.fix instead.
RECOMMENDATIONS = {
    FindingRule.MISSING: "neu anlegen, siehe Tabelle",
    FindingRule.PRESENCE_UNKNOWN: "Rechte des Elternordners prüfen",
    FindingRule.REVOCATION_UNCONFIRMED: "beim Anbieter widerrufen, siehe Tabelle",
    FindingRule.VERIFICATION_FAILED: "neu anmelden, siehe Tabelle",
    FindingRule.VERIFICATION_UNREACHABLE: "Prüfbefehl hing oder fehlt; später erneut",
}

# A remedy that depends on the entry's kind: a human's password is fetched back, because a new one locks out what the old one protected.
KIND_RECOMMENDATIONS = {(FindingRule.MISSING, SecretKind.HUMAN_PASSWORD): "zurückholen, siehe Tabelle"}


@dataclass(frozen=True)
class SecretFinding:
    rule: FindingRule
    subject: str
    detail: str = ""
    # The operator's one action that clears this finding, a command in backticks when it is one; `home next` shows it as a step.
    fix: str = ""
    kind: SecretKind | None = None

    @property
    def recommendation(self) -> str:
        return self.fix or KIND_RECOMMENDATIONS.get((self.rule, self.kind)) or RECOMMENDATIONS[self.rule]


@dataclass(frozen=True)
class SecretStatus:
    entry: SecretEntry
    presence: Presence
    verification: Verification


@dataclass(frozen=True)
class SecretsView:
    statuses: tuple[SecretStatus, ...]
    findings: tuple[SecretFinding, ...]


def presence(entry: SecretEntry, matches: Mapping[PurePosixPath, tuple[PathFact, ...] | None]) -> Presence:
    """Present when every pattern matches, unknown when the filesystem refuses to say for one of them."""
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
    return relative.parent == pattern.parent and fnmatch.fnmatchcase(relative.name, as_wildcard(pattern.name))


def _is_named(relative: PurePosixPath, patterns: tuple[PurePosixPath, ...]) -> bool:
    """Named are the pattern's matches, everything inside a match, and the directories that lead to a pattern."""
    candidates = (relative, *relative.parents)
    return any(relative in pattern.parents or any(_matches_pattern(c, pattern) for c in candidates) for pattern in patterns)


def _loosest_allowed(mode: int, loosest_file_mode: int) -> int | None:
    if stat.S_ISDIR(mode):
        return LOOSEST_DIRECTORY_MODE
    return loosest_file_mode if stat.S_ISREG(mode) else None


def too_loose(mode: int, loosest_file_mode: int) -> int | None:
    """The loosest mode allowed for this kind of path when `mode` grants more; None when it stays within it."""
    allowed = _loosest_allowed(mode, loosest_file_mode)
    return allowed if allowed is not None and stat.S_IMODE(mode) & ~allowed else None


def _in_shell(relative: PurePosixPath) -> str:
    return f"{HOME_PREFIX}{shlex.quote(str(relative))}"


def mode_fix(relative: PurePosixPath, allowed: int) -> str:
    return f"`chmod {allowed:o} {_in_shell(relative)}`"


def symlinked_folder_fix(relative: PurePosixPath) -> str:
    """Moves the folder the link points to into the link's place, so it becomes a real one; nothing is deleted but the link.

    A link whose target is gone leaves an empty folder in its place, so the command clears the finding either way.
    """
    place = _in_shell(relative)
    real_folder = f'if [ -e "$t" ]; then mv -T "$t" {place}; else mkdir {place}; fi'
    return f"`t=$(readlink -m {place}) && rm {place} && {real_folder} && chmod {LOOSEST_DIRECTORY_MODE:o} {place}`"


def _moved_aside_fix(relative: PurePosixPath) -> str:
    """Moves what stands in a folder's place aside, unchanged, so the folder can be created; nothing is deleted."""
    return f"`mv -n {_in_shell(relative)} {_in_shell(relative.with_name(f'{relative.name}.alt'))}`"


@dataclass(frozen=True)
class _Flaw:
    detail: str
    fix: str


def _symlinked(relative: PurePosixPath, mode: int) -> _Flaw | None:
    return _Flaw("ist ein Symlink", symlinked_folder_fix(relative)) if stat.S_ISLNK(mode) else None


def _not_a_folder(relative: PurePosixPath, mode: int) -> _Flaw | None:
    return None if stat.S_ISDIR(mode) else _Flaw("ist kein Ordner", _moved_aside_fix(relative))


def _closed(relative: PurePosixPath, mode: int) -> _Flaw | None:
    if not closed_to_owner(mode):
        return None
    needed = LOOSEST_DIRECTORY_MODE
    return _Flaw(f"hat Rechte {stat.S_IMODE(mode):o}, der Besitzer braucht {needed:o}", mode_fix(relative, needed))


def _loose(relative: PurePosixPath, mode: int, declaring: tuple[SecretEntry, ...] = ()) -> _Flaw | None:
    """Judged against the permission bits every declaring entry allows, so no entry's limit is loosened by another."""
    loosest = reduce(and_, (entry.loosest_mode for entry in declaring)) if declaring else LOOSEST_FILE_MODE
    allowed = too_loose(mode, loosest)
    if allowed is None:
        return None
    reasons = [entry.loosest_mode_reason for entry in declaring if entry.loosest_mode == allowed and entry.loosest_mode_reason]
    reason = f" ({reasons[0]})" if reasons else ""
    return _Flaw(f"hat Rechte {stat.S_IMODE(mode):o}, höchstens {allowed:o}{reason}", mode_fix(relative, allowed))


def _declaring_entries(relative: PurePosixPath, manifest: tuple[SecretEntry, ...]) -> tuple[SecretEntry, ...]:
    """The live entries whose place this file is."""
    return tuple(entry for entry in manifest if not entry.retired and any(_matches_pattern(relative, pattern) for pattern in entry.paths))


# What can be wrong with a folder on the way to a secret, in the order `unfit_folder` names the first one found.
_FOLDER_FLAWS = {
    FindingRule.SYMLINKED_FOLDER: _symlinked,
    FindingRule.NOT_A_FOLDER: _not_a_folder,
    FindingRule.CLOSED_FOLDER: _closed,
    FindingRule.LOOSE_MODE: _loose,
}

# `home secrets --setup` stores nothing below a folder `unfit_folder` refuses, so their fix comes before it.
SETUP_BLOCKERS = frozenset(_FOLDER_FLAWS)


def _flaw_finding(rule: FindingRule, relative: PurePosixPath, flaw: _Flaw | None) -> SecretFinding | None:
    return flaw and SecretFinding(rule, f"{HOME_PREFIX}{relative}", flaw.detail, flaw.fix)


def unfit_folder(relative: PurePosixPath, mode: int) -> SecretFinding | None:
    """Why nothing may be stored below this place on the way to a secret, with its fix; None for a real folder open to its owner only."""
    for rule, flaw in _FOLDER_FLAWS.items():
        if finding := _flaw_finding(rule, relative, flaw(relative, mode)):
            return finding
    return None


def _tree_findings(
    tree: tuple[PathFact, ...], manifest: tuple[SecretEntry, ...], patterns: tuple[PurePosixPath, ...]
) -> Iterator[SecretFinding]:
    """A folder or a place on the way to a manifest place is judged as a folder; any other path by its mode and the loosest one its entry declares."""
    for fact in tree:
        if stat.S_ISDIR(fact.mode) or any(fact.relative in pattern.parents for pattern in patterns):
            finding = unfit_folder(fact.relative, fact.mode)
        else:
            loose = _loose(fact.relative, fact.mode, _declaring_entries(fact.relative, manifest))
            finding = _flaw_finding(FindingRule.LOOSE_MODE, fact.relative, loose)
        if finding:
            yield finding


def _entry_findings(status: SecretStatus) -> Iterator[SecretFinding]:
    name = status.entry.name
    if status.entry.retired:
        if status.entry.revocation_confirmed is None:
            yield SecretFinding(FindingRule.REVOCATION_UNCONFIRMED, name, status.entry.revoke_url)
        return
    if status.presence is Presence.MISSING and status.entry.expected_present:
        yield SecretFinding(FindingRule.MISSING, name, kind=status.entry.kind)
    if status.presence is Presence.UNKNOWN:
        yield SecretFinding(FindingRule.PRESENCE_UNKNOWN, name)
    if status.verification is Verification.FAILED:
        yield SecretFinding(FindingRule.VERIFICATION_FAILED, name, status.entry.verifier.value)
    if status.verification is Verification.UNREACHABLE:
        yield SecretFinding(FindingRule.VERIFICATION_UNREACHABLE, name, status.entry.verifier.value)


def build_view(manifest: tuple[SecretEntry, ...], observation: Observation) -> SecretsView:
    """Pure: the statuses and findings follow from the manifest and what the boundaries saw, nothing else."""
    statuses = tuple(
        SecretStatus(entry, presence(entry, observation.matches), _verification(entry, observation.probe_codes)) for entry in manifest
    )
    patterns = tuple(pattern for entry in manifest for pattern in entry.paths)
    unknown = (
        SecretFinding(
            FindingRule.UNKNOWN_FILE,
            f"{HOME_PREFIX}{fact.relative}",
            fix=f"{HOME_PREFIX}{fact.relative} ins Manifest eintragen oder löschen",
        )
        for fact in observation.tree
        if not _is_named(fact.relative, patterns)
    )
    findings = (
        *(f for status in statuses for f in _entry_findings(status)),
        *unknown,
        *_tree_findings(observation.tree, manifest, patterns),
    )
    return SecretsView(statuses, findings)


# --- rendering ------------------------------------------------------------

NO_VALUES_NOTE = "Keine Werte: home secrets liest Geheimnisdateien nur per stat und öffnet sie nie."


def places(entry: SecretEntry) -> str:
    listed = ", ".join(f"{HOME_PREFIX}{pattern}" for pattern in entry.paths)
    return f"{listed}; {entry.elsewhere}" if entry.elsewhere else listed


def _status_row(status: SecretStatus) -> list[str]:
    entry = status.entry
    if entry.retired:
        revocation = REVOCATION_CONFIRMED.format(date=entry.revocation_confirmed) if entry.revocation_confirmed else REVOCATION_UNCONFIRMED
        return [entry.name, entry.kind.label, RETIRED, revocation, EXPIRY_UNKNOWN, places(entry), f"Widerruf: {entry.revoke_url}"]
    presence = OPTIONAL.format(presence=status.presence.value) if entry.optional else status.presence.value
    return [entry.name, entry.kind.label, presence, status.verification.value, EXPIRY_UNKNOWN, places(entry), entry.recreate]


def render_view(view: SecretsView, now: datetime) -> str:
    header = f"# home secrets {now:%d.%m.%Y %H:%M} | {findings_count(len(view.findings))}"
    status_headers = ["Name", "Art", "Datei", "Prüfung", "Ablauf", "Ort", "Neu anlegen"]
    lines = [header, "", *markdown_table(status_headers, [_status_row(status) for status in view.statuses]), ""]
    if view.findings:
        rows = [[f.rule.value, f.subject, f.detail, f.recommendation] for f in view.findings]
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
    """What `home secrets` without options shows; `hal check` reads the secrets through this."""
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
