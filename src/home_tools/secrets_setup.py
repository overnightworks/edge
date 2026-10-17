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
import stat
import sys
import urllib.error
import urllib.request
import uuid
import warnings
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from home_tools import secrets_view
from home_tools.secrets_view import (
    HOME_PREFIX,
    LOOSEST_DIRECTORY_MODE,
    LOOSEST_FILE_MODE,
    PROBE_STOP_SIGNALS,
    ExitCodeProbe,
    Presence,
    Provider,
    SecretEntry,
    TokenSource,
    load_manifest,
    presence,
    show,
    show_by_service,
    stat_matches,
    too_loose,
)
from home_tools.tidy_home import Config, Layout

NO_TERMINAL_EXIT = 2
ENTRY_FAILED_EXIT = 2
ABORTED_EXIT = 130  # the shell's code for a run ended by Ctrl-C

OPEN_PROMPT = "Enter öffnet die Seite im Browser. "
PASTE_PROMPT = "Token einfügen (unsichtbar; leer überspringt): "
NO_BROWSER = "Kein Browser ließ sich öffnen; die Seite von Hand öffnen:"
NEXT_STEP = "next: `uv run edge external plan` in ~/git/edge (expected: No changes)"
RETRY_STEP = "next: `home secrets --setup` noch einmal, für: "
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
CLOUDFLARE_VERIFY_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"
CLOUDFLARE_ACTIVE = "active"


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


def _judge(provider: Provider, response: Response) -> Verdict:
    label = PROVIDER_LABELS[provider]
    if response.status != 200:
        return Verdict(Outcome.REFUSED, f"{label} antwortet {response.status}")
    if provider is Provider.CLOUDFLARE:
        status = _cloudflare_token_status(response.body)
        if status != CLOUDFLARE_ACTIVE:
            return Verdict(Outcome.REFUSED, f"{label} meldet das Token als {status or 'unbekannt'}, nicht {CLOUDFLARE_ACTIVE}")
    return Verdict(Outcome.ACCEPTED)


def verify_token(source: TokenSource, value: str, fetch: Fetch = http_fetch) -> Verdict:
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
    return _judge(source.provider, response)


# --- the terminal boundary -------------------------------------------------


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


# Tests replace this one object; the walk reads it when it starts.
BOUNDARIES = Boundaries(Terminal(_both_ends_are_a_terminal, input, getpass.getpass, webbrowser.open), verify_token, os.fsync)


# --- the filesystem boundary ------------------------------------------------


class StoreRefused(Exception):
    """A secret could not be written where the manifest puts it; the message names the place, never a value."""


def _private_directories(home: Path, directory: Path) -> None:
    """Every directory from the home down to `directory` exists as a real directory open to its owner only."""
    for path in (*reversed(directory.relative_to(home).parents[:-1]), directory.relative_to(home)):
        absolute = home / path
        with contextlib.suppress(FileExistsError):
            absolute.mkdir(mode=LOOSEST_DIRECTORY_MODE)
        mode = absolute.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise StoreRefused(f"{HOME_PREFIX}{path} ist kein Ordner")
        if (allowed := too_loose(mode)) is not None:
            place = f"{HOME_PREFIX}{path}"
            raise StoreRefused(f"{place} hat Rechte {stat.S_IMODE(mode):o}, höchstens {allowed:o}: erst `chmod {allowed:o} {place}`")


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


def store_new_secret(layout: Layout, place: PurePosixPath, value: str, sync: DiskSync) -> None:
    """Writes a secret that does not exist yet; it never replaces one, and an interruption leaves no file behind.

    The value goes to a fresh private file that is linked to its place only once it is complete: the link fails rather
    than replace a file that appeared meanwhile, and the fresh name is removed on every way out. A stop that arrives
    meanwhile waits until the place holds the whole secret or nothing.
    """
    target = layout.home / place
    _private_directories(layout.home, target.parent)
    fresh = target.with_name(f".{target.name}.{uuid.uuid4().hex}.neu")
    with _stops_held():
        try:
            with open(os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, LOOSEST_FILE_MODE), "wb") as fresh_file:
                os.fchmod(fresh_file.fileno(), LOOSEST_FILE_MODE)
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
        f"  [ ] TTL: End Date {ends:%d.%m.%Y}",
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


def _stored(entry: SecretEntry, layout: Layout, boundaries: Boundaries, value: str, done: str) -> bool:
    try:
        store_new_secret(layout, entry.place, value, boundaries.sync)
    except StoreRefused as refused:
        _say(f"nicht gespeichert: {refused}")
        return False
    _say(f"{done} und gespeichert: {HOME_PREFIX}{entry.place} (600)")
    return True


def _paste_unseen(terminal: Terminal) -> str:
    """getpass falls back to echoed input when it cannot hide it, and only warns; here that warning ends the walk unread."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return terminal.ask_hidden(PASTE_PROMPT).strip()


def _is_visible_ascii(value: str) -> bool:
    """Every token the providers issue is visible ASCII; anything else is a slip of the paste and is never sent."""
    return all("!" <= character <= "~" for character in value)


def _accepted_paste(source: TokenSource, boundaries: Boundaries) -> str | None:
    """The first pasted value the provider accepts; None when the operator leaves the prompt empty."""
    while value := _paste_unseen(boundaries.terminal):
        if not _is_visible_ascii(value):
            _say(UNSENDABLE_PASTE)
            continue
        verdict = boundaries.verify(source, value)
        if verdict.outcome is Outcome.ACCEPTED:
            return value
        _say(f"abgelehnt: {verdict.detail}; nichts gespeichert. Noch einmal einfügen oder leer lassen.")
    _say("übersprungen; nichts gespeichert")
    return None


def _set_up_token(entry: SecretEntry, source: TokenSource, layout: Layout, boundaries: Boundaries, now: datetime) -> bool:
    guide = GUIDES[source.provider]
    _say(*guide.briefing(entry, source, now))
    boundaries.terminal.ask(OPEN_PROMPT)
    page = guide.page(entry, source)
    _say(f"Geöffnet: {page}" if boundaries.terminal.open_page(page) else f"{NO_BROWSER} {page}")
    value = _accepted_paste(source, boundaries)
    return value is not None and _stored(entry, layout, boundaries, value, "geprüft")


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
        return f"abgebrochen bei {self.entry_name}: unerwarteter Fehler ({self.error_type}); {left}. Weiter mit: home secrets --setup"


def _presence_on_disk(entry: SecretEntry, layout: Layout) -> Presence:
    return presence(entry, {pattern: stat_matches(layout, pattern) for pattern in entry.paths})


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
    if entry.source:
        stored = _set_up_token(entry, entry.source, layout, boundaries, now)
    else:
        _say("Wird zufällig erzeugt und nie gezeigt.")
        stored = _stored(entry, layout, boundaries, secrets.token_urlsafe(PASSPHRASE_BYTES), "erzeugt")
    return Step.STORED if stored else Step.STILL_MISSING


def _walk(layout: Layout, boundaries: Boundaries, now: datetime) -> int:
    """Every live entry in manifest order; the last line names the plan step, after what is still open."""
    still_open: list[str] = []
    by_hand = False
    for entry in load_manifest(secrets_view.MANIFEST_PATH):
        if entry.retired:
            continue
        found = _presence_on_disk(entry, layout)
        if found is Presence.PRESENT:
            _say(f"{entry.name}: vorhanden, bleibt unverändert")
        elif found is Presence.UNKNOWN:
            _say(f"{entry.name}: nicht feststellbar, ob vorhanden (Ordner nicht lesbar); übersprungen")
            still_open.append(entry.name)
        else:
            step = _set_up_or_fail(entry, layout, boundaries, now)
            by_hand |= step is Step.BY_HAND
            if step is Step.STILL_MISSING:
                still_open.append(entry.name)
    _say("")
    if still_open:
        _say(RETRY_STEP + ", ".join(still_open))
    _say(NEXT_STEP)
    return 1 if still_open or by_hand else 0


def run_setup(layout: Layout, boundaries: Boundaries, now: datetime) -> int:
    if not boundaries.terminal.is_interactive():
        print("home secrets --setup läuft nur am eigenen Terminal; ohne --setup zeigt home secrets nur an.", file=sys.stderr)
        return NO_TERMINAL_EXIT
    try:
        return _walk(layout, boundaries, now)
    except (KeyboardInterrupt, EOFError):
        print("\nabgebrochen; keine Datei halb geschrieben. Weiter mit: home secrets --setup", file=sys.stderr)
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


def _secrets_from_args(args: argparse.Namespace, layout: Layout, _config: Config, _run: object, probe: ExitCodeProbe, now: datetime) -> int:
    if args.setup:
        return run_setup(layout, BOUNDARIES, now)
    return show_by_service() if args.by_service else show(layout, probe, now)


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
