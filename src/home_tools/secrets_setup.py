"""`home secrets --setup` walks every missing secret of the manifest, one at a time, and never shows a value.

A person stays in the loop on purpose: only someone with more rights than a token can create it, GitHub has no API
for fine-grained tokens, and on Cloudflare the wizard would need an admin key on disk. So the browser login stays the
operator's. The wizard does everything else: it says what comes and why, opens the provider's page, takes the pasted
value without echo, has the provider confirm it and only then writes the file, 0600 in a 0700 directory. A service
password is generated and written unseen. A file already present stays untouched. It runs only at a terminal.

This module also wires the `home secrets` command; its read-only view is `secrets_view`.

Exit code of `--setup`: 0 when nothing is missing any more, 1 when something still is, 2 without a terminal, at one that
cannot hide the input, with an invalid manifest or after an unexpected error at an entry, 130 after Ctrl-C.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import secrets
import signal
import sys
import urllib.error
import urllib.request
import uuid
import warnings
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from home_tools import secrets_view
from home_tools.secrets_view import (
    HOME_PREFIX,
    LOOSEST_DIRECTORY_MODE,
    LOOSEST_FILE_MODE,
    PROBE_STOP_SIGNALS,
    Presence,
    Provider,
    SecretEntry,
    SecretFinding,
    SecretKind,
    TokenSource,
    load_manifest,
    presence,
    show,
    show_by_service,
    stat_matches,
    unfit_folder,
)
from home_tools.tidy_home import Config
from home_tools.layout import Layout

if TYPE_CHECKING:
    from home_tools.cli import Ports

NO_TERMINAL_EXIT = 2
ENTRY_FAILED_EXIT = 2
ABORTED_EXIT = 130  # the shell's code for a run ended by Ctrl-C

OPEN_PROMPT = "Enter öffnet die Seite im Browser. "
PASTE_PROMPT = "Token einfügen (unsichtbar; leer überspringt): "
SKIPPED = "übersprungen; nichts gespeichert"
NO_BROWSER = "Kein Browser ließ sich öffnen; die Seite von Hand öffnen:"
PLAN_READER = "edge external plan"
NEXT_STEP = "next: `uv run edge external plan` in ~/git/edge (expected: No changes)"
PLAN_NEEDS = "next: erst {names} anlegen; `uv run edge external plan` in ~/git/edge braucht sie"
RETRY_STEP = "next: {first}`hal setup --apply` noch einmal"
STILL_OPEN = "noch offen: "
BY_HAND_STEP = "von Hand offen, siehe oben: "
UNSENDABLE_PASTE = (
    "abgelehnt: ein Token hat nur sichtbare ASCII-Zeichen, ohne Leerzeichen; nichts geprüft, nichts gespeichert."
    " Noch einmal einfügen oder leer lassen."
)
EXPOSED_INPUT_REFUSAL = "abgebrochen: dieses Terminal kann die Eingabe nicht verbergen; nichts gelesen. Ein anderes Terminal nehmen."

PASSPHRASE_BYTES = 48
VERIFY_TIMEOUT_SECONDS = 20
RESPONSE_LIMIT_BYTES = 64 * 1024

GITHUB_TOKEN_PAGE = "https://github.com/settings/personal-access-tokens/new"
GITHUB_API = "https://api.github.com"
CLOUDFLARE_TOKEN_PAGE = "https://dash.cloudflare.com/profile/api-tokens"
CLOUDFLARE_DASHBOARD_PAGE = "https://dash.cloudflare.com/"
CLOUDFLARE_VERIFY_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"
CLOUDFLARE_ACTIVE = "active"
NOT_YET_VALID = (
    "{label} reports the token as active, but it is not valid before {start:%d.%m.%Y %H:%M} UTC; "
    "clear the start date on the token's Edit page in Cloudflare, then paste again"
)
UNREADABLE_START = "{label}'s answer has a start date that cannot be read; check the token's TTL on its Edit page, then paste again"


# --- the provider boundary -------------------------------------------------


class Outcome(Enum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Verdict:
    """What the provider said about a pasted value; the detail names a status, never the value."""

    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


Fetch = Callable[[urllib.request.Request], Response | None]
TokenVerifier = Callable[[TokenSource, str], Verdict]


def http_fetch(request: urllib.request.Request) -> Response | None:
    """One HTTP call; None when the provider cannot be reached. An error status is an answer, not a failure."""
    try:
        with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SECONDS) as response:
            return Response(response.status, response.read(RESPONSE_LIMIT_BYTES))
    except urllib.error.HTTPError as error:
        with contextlib.closing(error):
            return Response(error.code, b"")
    except OSError:  # URLError and a timeout are OSErrors too
        return None


def _verify_request(source: TokenSource) -> urllib.request.Request:
    """GitHub: listing the org's secret names needs the token's org reach and its Secrets right, and returns no value."""
    if source.provider is Provider.GITHUB:
        url = f"{GITHUB_API}/orgs/{quote(source.resource)}/actions/secrets?per_page=1"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    else:
        url, headers = CLOUDFLARE_VERIFY_URL, {"Accept": "application/json"}
    return urllib.request.Request(url, headers={**headers, "User-Agent": "home-secrets-setup"})


def _cloudflare_token_status(body: bytes) -> str:
    try:
        return str(json.loads(body)["result"]["status"])
    except (ValueError, KeyError, TypeError):
        return ""


def _cloudflare_not_before(body: bytes) -> datetime | None:
    """The start of the token's validity: None when the answer names none, ValueError when it names one that cannot be read."""
    result = json.loads(body).get("result") if body else None
    raw = result.get("not_before") if isinstance(result, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("not_before is not text")
    start = datetime.fromisoformat(raw)
    return start if start.tzinfo else start.replace(tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _judge(provider: Provider, response: Response, now: datetime) -> Verdict:
    label = PROVIDER_LABELS[provider]
    if response.status != 200:
        return Verdict(Outcome.REFUSED, f"{label} antwortet {response.status}")
    if provider is Provider.CLOUDFLARE:
        status = _cloudflare_token_status(response.body)
        if status != CLOUDFLARE_ACTIVE:
            return Verdict(Outcome.REFUSED, f"{label} meldet das Token als {status or 'unbekannt'}, nicht {CLOUDFLARE_ACTIVE}")
        try:
            not_before = _cloudflare_not_before(response.body)
        except ValueError:  # the message may quote the answer, so the refusal says nothing of it
            return Verdict(Outcome.REFUSED, UNREADABLE_START.format(label=label))
        if not_before is not None and not_before > now:
            return Verdict(Outcome.REFUSED, NOT_YET_VALID.format(label=label, start=not_before.astimezone(UTC)))
    return Verdict(Outcome.ACCEPTED)


def verify_token(source: TokenSource, value: str, fetch: Fetch = http_fetch, clock: Callable[[], datetime] = _utc_now) -> Verdict:
    """Asks the provider whether the value works before anything is stored."""
    request = _verify_request(source)
    # Unredirected: a redirect to another host never carries the token along.
    request.add_unredirected_header("Authorization", f"Bearer {value}")
    try:
        response = fetch(request)
    except ValueError:  # http.client refuses a header it cannot send with a message that quotes the value
        return Verdict(Outcome.REFUSED, f"der Wert lässt sich {PROVIDER_LABELS[source.provider]} nicht als Token senden")
    if response is None:
        return Verdict(Outcome.UNREACHABLE, f"{PROVIDER_LABELS[source.provider]} nicht erreichbar; später noch einmal")
    return _judge(source.provider, response, clock())


# --- the terminal boundary -------------------------------------------------

# The one fact both `run_setup`'s refusal and `home next`'s "eigenes Terminalfenster" read: the walk asks a person.
SETUP_NEEDS_A_TERMINAL = True


def _both_ends_are_a_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass(frozen=True)
class Terminal:
    """Everything the walk asks of the person in front of it."""

    is_interactive: Callable[[], bool]
    ask: Callable[[str], str]
    ask_hidden: Callable[[str], str]
    open_page: Callable[[str], bool]


DiskSync = Callable[[int], None]


@dataclass(frozen=True)
class Boundaries:
    terminal: Terminal
    verify: TokenVerifier
    sync: DiskSync


TERMINAL_BOUNDARIES = Boundaries(Terminal(_both_ends_are_a_terminal, input, getpass.getpass, webbrowser.open), verify_token, os.fsync)


# --- the filesystem boundary ------------------------------------------------


class StoreRefused(Exception):
    """A secret could not be written where the manifest puts it; the message names the place, never a value."""


def _levels(home: Path, directory: Path) -> tuple[Path, ...]:
    """The directories from the home down to `directory`, outermost first, relative to the home."""
    relative = directory.relative_to(home)
    return (*reversed(relative.parents[:-1]), relative)


def _refused_below(unfit: SecretFinding) -> str:
    return f"{unfit.subject} {unfit.detail}: erst {unfit.fix}"


def _private_directories(home: Path, directory: Path) -> None:
    """Every directory from the home down to `directory` exists as a real directory open to its owner only."""
    for relative in _levels(home, directory):
        with contextlib.suppress(FileExistsError):
            (home / relative).mkdir(mode=LOOSEST_DIRECTORY_MODE)
        if unfit := unfit_folder(PurePosixPath(relative), (home / relative).lstat().st_mode):
            raise StoreRefused(_refused_below(unfit))


def _store_refusal(layout: Layout, place: PurePosixPath) -> SecretFinding | None:
    """Read-only, before a provider page is opened: what an existing directory on the way would refuse the store for."""
    for relative in _levels(layout.home, (layout.home / place).parent):
        try:
            mode = (layout.home / relative).lstat().st_mode
        except FileNotFoundError:
            return None  # the store creates this level and every one below it, private
        if unfit := unfit_folder(PurePosixPath(relative), mode):
            return unfit
    return None


@contextlib.contextmanager
def _stops_held() -> Iterator[None]:
    """Ctrl-C, SIGTERM and SIGHUP wait until the block is done and arrive right after it, so none can split it.

    The mask holds for the calling thread only. That is enough because `home` stores from its one thread: a second
    thread with the signals unblocked would take them, and Python would still raise them here mid-block.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, PROBE_STOP_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def store_new_secret(layout: Layout, place: PurePosixPath, value: str, sync: DiskSync, mode: int = LOOSEST_FILE_MODE) -> None:
    """Writes a secret that does not exist yet; it never replaces one, and an interruption leaves no file behind.

    The value goes to a fresh private file that is linked to its place only once it is complete: the link fails rather
    than replace a file that appeared meanwhile, and the fresh name is removed on every way out. A stop that arrives
    meanwhile waits until the place holds the whole secret or nothing. `mode` is the entry's declared loosest mode; the
    folder stays 0700 whatever it is.
    """
    target = layout.home / place
    _private_directories(layout.home, target.parent)
    fresh = target.with_name(f".{target.name}.{uuid.uuid4().hex}.neu")
    with _stops_held():
        try:
            with open(os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), "wb") as fresh_file:
                os.fchmod(fresh_file.fileno(), mode)
                fresh_file.write(value.encode())
                fresh_file.flush()
                sync(fresh_file.fileno())
            os.link(fresh, target)
        except FileExistsError:
            raise StoreRefused(f"{HOME_PREFIX}{place} ist inzwischen vorhanden und bleibt unverändert") from None
        finally:
            fresh.unlink(missing_ok=True)


# --- what the operator reads, per provider ---------------------------------

PROVIDER_LABELS = {Provider.GITHUB: "GitHub", Provider.CLOUDFLARE: "Cloudflare"}


def github_page(entry: SecretEntry, source: TokenSource) -> str:
    """GitHub's token form, pre-filled with every field its documented URL parameters take."""
    rights = [tuple(right.split(":", 1)) for right in source.scope]
    fields = [
        ("name", entry.name),
        ("description", entry.purpose),
        ("target_name", source.resource),
        ("expires_in", str(source.expires_in_days)),
        *rights,
    ]
    return f"{GITHUB_TOKEN_PAGE}?{urlencode(fields, quote_via=quote)}"


def _github_briefing(entry: SecretEntry, source: TokenSource, _now: datetime) -> list[str]:
    return [
        f"Seite: GitHub, neues Fine-grained-Token. Login: dein GitHub-Konto, Inhaber der Org {source.resource}.",
        f"Der Link füllt vor: Name {entry.name}, Resource owner {source.resource}, Ablauf {source.expires_in_days} Tage,"
        " die Beschreibung und diese Rechte:",
        *(f"  - {right.replace(':', ': ', 1)}" for right in source.scope),
        "Von Hand, das kann kein Link vorgeben:",
        "  [ ] Repository access: All repositories",
        "  [ ] die Rechte im Formular mit der Liste oben vergleichen",
        "  [ ] Generate token, dann den Wert kopieren",
    ]


def cloudflare_page(_entry: SecretEntry, _source: TokenSource) -> str:
    """Cloudflare's token page takes no pre-filled fields; the briefing lists them."""
    return CLOUDFLARE_TOKEN_PAGE


def _cloudflare_briefing(entry: SecretEntry, source: TokenSource, now: datetime) -> list[str]:
    ends = (now + timedelta(days=source.expires_in_days)).date()
    return [
        "Seite: Cloudflare, My Profile › API Tokens. Login: dein Cloudflare-Konto.",
        "Cloudflare füllt nichts vor; von Hand:",
        "  [ ] Create Token › Custom token › Get started",
        f"  [ ] Token name: {entry.name}",
        *(f"  [ ] Permissions: {right}" for right in source.scope),
        "  [ ] Account Resources: Include › dein Konto",
        f"  [ ] Zone Resources: Include › Specific zone › {source.resource}",
        f"  [ ] TTL: leave the start date empty, set only the End Date {ends:%d.%m.%Y}",
        "  [ ] Continue to summary › Create Token, dann den Wert kopieren",
    ]


@dataclass(frozen=True)
class ProviderGuide:
    page: Callable[[SecretEntry, TokenSource], str]
    briefing: Callable[[SecretEntry, TokenSource, datetime], list[str]]


GUIDES = {
    Provider.GITHUB: ProviderGuide(github_page, _github_briefing),
    Provider.CLOUDFLARE: ProviderGuide(cloudflare_page, _cloudflare_briefing),
}


# --- the walk ---------------------------------------------------------------


def _say(*lines: str) -> None:
    for line in lines:
        print(line)


def stored_line(entry: SecretEntry) -> str:
    """What the walk says once an entry is stored: a token after its provider confirmed it, an unverified one as such, a service password unseen."""
    if entry.kind is SecretKind.UNVERIFIED_TOKEN:
        done = "gespeichert, nicht geprüft"
    else:
        done = "geprüft und gespeichert" if entry.source else "erzeugt und gespeichert"
    return f"{done}: {HOME_PREFIX}{entry.place} ({entry.loosest_mode:o})"


def _stored(entry: SecretEntry, layout: Layout, boundaries: Boundaries, value: str) -> bool:
    try:
        store_new_secret(layout, entry.place, value, boundaries.sync, entry.loosest_mode)
    except StoreRefused as refused:
        _say(f"nicht gespeichert: {refused}")
        return False
    _say(stored_line(entry))
    return True


def _paste_unseen(terminal: Terminal) -> str:
    """getpass falls back to echoed input when it cannot hide it, and only warns; here that warning ends the walk unread."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return terminal.ask_hidden(PASTE_PROMPT).strip()


def _is_visible_ascii(value: str) -> bool:
    """Every token the providers issue is visible ASCII; anything else is a slip of the paste and is never sent."""
    return all("!" <= character <= "~" for character in value)


def _visible_paste(terminal: Terminal) -> str:
    """The first pasted value that is visible ASCII; empty when the operator leaves the prompt empty."""
    while value := _paste_unseen(terminal):
        if _is_visible_ascii(value):
            return value
        _say(UNSENDABLE_PASTE)
    return ""


def _accepted_paste(source: TokenSource, boundaries: Boundaries) -> str | None:
    """The first pasted value the provider accepts; None when the operator leaves the prompt empty."""
    while value := _visible_paste(boundaries.terminal):
        verdict = boundaries.verify(source, value)
        if verdict.outcome is Outcome.ACCEPTED:
            return value
        _say(f"abgelehnt: {verdict.detail}; nichts gespeichert. Noch einmal einfügen oder leer lassen.")
    _say(SKIPPED)
    return None


def _offer_page(terminal: Terminal, page: str) -> None:
    terminal.ask(OPEN_PROMPT)
    _say(f"Geöffnet: {page}" if terminal.open_page(page) else f"{NO_BROWSER} {page}")


def _set_up_token(entry: SecretEntry, source: TokenSource, layout: Layout, boundaries: Boundaries, now: datetime) -> bool:
    guide = GUIDES[source.provider]
    _say(*guide.briefing(entry, source, now))
    _offer_page(boundaries.terminal, guide.page(entry, source))
    value = _accepted_paste(source, boundaries)
    return value is not None and _stored(entry, layout, boundaries, value)


def _set_up_unverified_token(entry: SecretEntry, layout: Layout, boundaries: Boundaries) -> bool:
    """O6: the value is asked hidden and stored without provider verification; no provider is asked, because the TURN key's token is no user token."""
    _say(f"Anlegen: {entry.recreate}")
    _offer_page(boundaries.terminal, CLOUDFLARE_DASHBOARD_PAGE)
    value = _visible_paste(boundaries.terminal)
    if not value:
        _say(SKIPPED)
    return bool(value) and _stored(entry, layout, boundaries, value)


class Step(Enum):
    """How one missing entry left the walk."""

    STORED = "stored"
    STILL_MISSING = "still-missing"
    BY_HAND = "by-hand"


# What an unexpected error left of the one entry it stopped, as the disk shows it afterwards.
WHAT_FAILURE_LEFT = {
    Presence.PRESENT: "{name} ist gespeichert",
    Presence.MISSING: "für {name} nichts gespeichert",
    Presence.UNKNOWN: "ob {name} gespeichert ist, ist nicht feststellbar",
}


class EntryFailed(Exception):
    """An unexpected error while one entry was set up; only its type is kept, because its message may quote a value.

    The error can come after the entry was stored, so what it left is read back from the disk rather than assumed.
    """

    def __init__(self, entry_name: str, error: Exception, left: Presence) -> None:
        super().__init__(entry_name)
        self.entry_name = entry_name
        self.error_type = type(error).__name__
        self.left = left

    def report(self) -> str:
        left = WHAT_FAILURE_LEFT[self.left].format(name=self.entry_name)
        return f"abgebrochen bei {self.entry_name}: unerwarteter Fehler ({self.error_type}); {left}. Weiter mit: hal setup --apply"


def _presence_on_disk(entry: SecretEntry, layout: Layout) -> Presence:
    return presence(entry, {pattern: stat_matches(layout, pattern) for pattern in entry.paths})


def live_entries() -> list[SecretEntry]:
    return [entry for entry in load_manifest(secrets_view.MANIFEST_PATH) if not entry.retired]


def is_open(entry: SecretEntry, found: Presence) -> bool:
    """Whether the walk has work for a live entry: its presence cannot be told, or it is absent and not optional."""
    return found is Presence.UNKNOWN or (found is Presence.MISSING and entry.expected_present)


def open_entries(layout: Layout) -> list[tuple[SecretEntry, Presence]]:
    """The live entries the walk would not leave alone, with their presence, by `stat` only."""
    found = ((entry, _presence_on_disk(entry, layout)) for entry in live_entries())
    return [(entry, presence_on_disk) for entry, presence_on_disk in found if is_open(entry, presence_on_disk)]


def _set_up_or_fail(entry: SecretEntry, layout: Layout, boundaries: Boundaries, now: datetime) -> Step:
    try:
        return _set_up(entry, layout, boundaries, now)
    except (EOFError, getpass.GetPassWarning):
        raise
    except Exception as error:
        raise EntryFailed(entry.name, error, _presence_on_disk(entry, layout)) from None


def _set_up(entry: SecretEntry, layout: Layout, boundaries: Boundaries, now: datetime) -> Step:
    _say("", f"== {entry.name} ({entry.kind.label}, fehlt)")
    if entry.purpose:
        _say(f"Wofür: {entry.purpose}")
    if not entry.written_by_setup:
        _say(f"Dafür gibt es noch keinen Assistenten; von Hand: {entry.recreate}")
        return Step.BY_HAND
    if refusal := _store_refusal(layout, entry.place):
        _say(f"nicht gespeichert: {_refused_below(refusal)}")
        return Step.STILL_MISSING
    if entry.source:
        stored = _set_up_token(entry, entry.source, layout, boundaries, now)
    elif entry.kind is SecretKind.UNVERIFIED_TOKEN:
        stored = _set_up_unverified_token(entry, layout, boundaries)
    else:
        _say("Wird zufällig erzeugt und nie gezeigt.")
        stored = _stored(entry, layout, boundaries, secrets.token_urlsafe(PASSPHRASE_BYTES))
    return Step.STORED if stored else Step.STILL_MISSING


def next_line(still_open: list[SecretEntry], by_hand: list[SecretEntry], unblocking: list[str]) -> str:
    """The walk's one `next:` line: what unblocks the open entries and the walk again, else the plan step or what it lacks."""
    if still_open:
        return RETRY_STEP.format(first="".join(f"{command}, dann " for command in unblocking))
    plan_lacks = [entry.name for entry in by_hand if PLAN_READER in entry.read_by]
    return PLAN_NEEDS.format(names=", ".join(plan_lacks)) if plan_lacks else NEXT_STEP


def _ending(still_open: list[SecretEntry], by_hand: list[SecretEntry], unblocking: list[str]) -> list[str]:
    """What is still open and what is left by hand, then the one `next:` line."""
    lines = [STILL_OPEN + ", ".join(entry.name for entry in still_open)] if still_open else []
    if by_hand:
        lines.append(BY_HAND_STEP + ", ".join(entry.name for entry in by_hand))
    return [*lines, next_line(still_open, by_hand, unblocking)]


def _unblocking(still_open: list[SecretEntry], layout: Layout) -> list[str]:
    """The commands that clear what the store still refuses for the open entries, each once, as the disk shows it now."""
    refusals = (_store_refusal(layout, entry.place) for entry in still_open if entry.written_by_setup)
    return list(dict.fromkeys(refusal.fix for refusal in refusals if refusal))


def _visit(entry: SecretEntry, layout: Layout, boundaries: Boundaries, now: datetime) -> Step | None:
    """One live entry: kept, skipped or set up; None when it is kept or optional."""
    found = _presence_on_disk(entry, layout)
    if not is_open(entry, found):
        _say(f"{entry.name}: vorhanden, bleibt unverändert" if found is Presence.PRESENT else f"{entry.name}: fehlt, ist optional; übersprungen")
        return None
    if found is Presence.UNKNOWN:
        _say(f"{entry.name}: nicht feststellbar, ob vorhanden (Ordner nicht lesbar); übersprungen")
        return Step.STILL_MISSING
    return _set_up_or_fail(entry, layout, boundaries, now)


def _walk(layout: Layout, boundaries: Boundaries, now: datetime) -> int:
    """Every live entry in manifest order; the closing lines name what is still open and the plan step."""
    still_open: list[SecretEntry] = []
    by_hand: list[SecretEntry] = []
    for entry in live_entries():
        step = _visit(entry, layout, boundaries, now)
        if step is Step.BY_HAND:
            by_hand.append(entry)
        elif step is Step.STILL_MISSING:
            still_open.append(entry)
    _say("", *_ending(still_open, by_hand, _unblocking(still_open, layout)))
    return 1 if still_open or by_hand else 0


def run_setup(layout: Layout, boundaries: Boundaries, now: datetime) -> int:
    if SETUP_NEEDS_A_TERMINAL and not boundaries.terminal.is_interactive():
        print("home secrets --setup läuft nur am eigenen Terminal; ohne --setup zeigt home secrets nur an.", file=sys.stderr)
        return NO_TERMINAL_EXIT
    try:
        return _walk(layout, boundaries, now)
    except (KeyboardInterrupt, EOFError):
        print("\nabgebrochen; keine Datei halb geschrieben. Weiter mit: hal setup --apply", file=sys.stderr)
        return ABORTED_EXIT
    except getpass.GetPassWarning:
        print(f"\n{EXPOSED_INPUT_REFUSAL}", file=sys.stderr)
        return NO_TERMINAL_EXIT
    except EntryFailed as failed:
        print(f"\n{failed.report()}", file=sys.stderr)
        return ENTRY_FAILED_EXIT


# --- the `home secrets` command --------------------------------------------

SECRETS_EXAMPLES = """\
Beispiele:
  home secrets                zeigt jedes Geheimnis des Manifests: Datei, Prüfung, Ablauf
  home secrets --by-service   welcher Dienst welches Geheimnis liest (deklariert)
  home secrets --setup        führt durch jedes fehlende Geheimnis: Seite, einfügen, prüfen, speichern

Ohne --setup zeigt es nie einen Wert und ändert nichts außer dem Bericht
~/.local/state/home/secrets-report.md. Braucht kein Terminal, Agenten dürfen es fahren.
--setup läuft nur am eigenen Terminal; Agenten fahren es nie.
Exit-Code: 0 sauber, 1 Funde (mit --setup: noch etwas fehlt), 2 Manifest ungültig, kein Terminal oder
unerwarteter Fehler bei einem Geheimnis, 3 sonst unerwarteter Fehler, 130 abgebrochen.
"""


def _secrets_from_args(args: argparse.Namespace, layout: Layout, _config: Config, ports: Ports) -> int:
    if args.setup:
        return run_setup(layout, ports.secrets_setup, ports.now)
    return show_by_service() if args.by_service else show(layout, ports.probe, ports.now)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "secrets",
        help="Geheimnisse zeigen und fehlende einrichten; nie ein Wert",
        description="Zeigt jedes Geheimnis aus dem Manifest, liest Dateien nur per stat und ändert nichts; --setup richtet fehlende ein.",
        epilog=SECRETS_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--by-service", action="store_true", help="gruppiert nach Dienst: deklarierte Abhängigkeiten")
    mode.add_argument("--setup", action="store_true", help="führt am Terminal durch jedes fehlende Geheimnis, eins nach dem anderen")
    parser.set_defaults(handler=_secrets_from_args, error_may_quote_a_secret=True)
