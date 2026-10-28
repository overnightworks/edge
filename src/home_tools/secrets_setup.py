"""`home secrets --setup` walks every missing secret of the manifest, one at a time, and never shows a value.

A person stays in the loop on purpose: only someone with more rights than a token can create it, GitHub has no API
for fine-grained tokens, and on Cloudflare the wizard would need an admin key on disk. So the browser login stays the
operator's. The wizard does everything else: it says what comes and why, opens the provider's page, takes the pasted
value without echo, has the provider confirm it and only then writes the file, 0600 in a 0700 directory. A service
password is generated and written unseen. A file already present stays untouched. It runs only at a terminal.

This module also wires the `home secrets` command; its read-only view is `secrets_view`.

Exit code of `--setup`: 0 when nothing is missing any more, 1 when something still is or the store refuses a place, 2 without a terminal, at one that
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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urlsplit

from hal_output import DETAIL_INDENT, Glyph, Row, Tone, write_detail, write_error, write_row
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
from home_tools.layout import Layout

if TYPE_CHECKING:
    from home_tools.cli import Ports

OPEN_PROMPT = f"{DETAIL_INDENT}Enter opens the page in your browser: "
PASTE_PROMPT = f"{DETAIL_INDENT}paste the token (hidden, empty skips): "
OPENED = "opened"
NO_BROWSER = "no browser opened · open it yourself:"
UNSENDABLE_PASTE = "a token is visible ASCII only, without spaces; nothing checked"

PASTE_AGAIN = "paste again or leave empty"
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
        return Verdict(Outcome.REFUSED, f"{label} answers {response.status}")
    if provider is Provider.CLOUDFLARE:
        status = _cloudflare_token_status(response.body)
        if status != CLOUDFLARE_ACTIVE:
            return Verdict(Outcome.REFUSED, f"{label} reports the token as {status or 'unknown'}, not {CLOUDFLARE_ACTIVE}")
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
        return Verdict(Outcome.REFUSED, f"the value cannot be sent to {PROVIDER_LABELS[source.provider]} as a token")
    if response is None:
        return Verdict(Outcome.UNREACHABLE, f"{PROVIDER_LABELS[source.provider]} unreachable; try again later")
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


def as_command(text: str) -> str:
    """An owner quotes a whole command in backticks; the operator is shown it bare."""
    inner = text.removeprefix("`").removesuffix("`")
    return inner if text != inner and "`" not in inner else text


class StoreRefused(Exception):
    """A secret could not be written where the manifest puts it.

    `reason` names the place, never a value; `fix` is the operator's one bare command, empty when there is none.
    """

    def __init__(self, reason: str, fix: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


def _levels(home: Path, directory: Path) -> tuple[Path, ...]:
    """The directories from the home down to `directory`, outermost first, relative to the home."""
    relative = directory.relative_to(home)
    return (*reversed(relative.parents[:-1]), relative)


def _refused_below(unfit: SecretFinding) -> StoreRefused:
    return StoreRefused(f"{unfit.subject} {unfit.detail}", as_command(unfit.fix))


def _private_directories(home: Path, directory: Path) -> None:
    """Every directory from the home down to `directory` exists as a real directory open to its owner only."""
    for relative in _levels(home, directory):
        with contextlib.suppress(FileExistsError):
            (home / relative).mkdir(mode=LOOSEST_DIRECTORY_MODE)
        if unfit := unfit_folder(PurePosixPath(relative), (home / relative).lstat().st_mode):
            raise _refused_below(unfit)


_OWNER_SEARCH_BIT = 0o100


def _store_refusal(layout: Layout, place: PurePosixPath, *, unless_unsearchable: bool = False) -> StoreRefused | None:
    """Read-only, before a provider page is opened: what an existing directory on the way would refuse the store for.

    With `unless_unsearchable`, a directory its owner cannot search (no owner x bit) ends the look with None: the walk
    cannot read below it, so it reports the entry as not checked instead of naming a refusal it cannot see through.
    """
    for relative in _levels(layout.home, (layout.home / place).parent):
        try:
            mode = (layout.home / relative).lstat().st_mode
        except FileNotFoundError:
            return None  # the store creates this level and every one below it, private
        if unfit := unfit_folder(PurePosixPath(relative), mode):
            if unless_unsearchable and stat.S_ISDIR(mode) and not mode & _OWNER_SEARCH_BIT:
                return None
            return _refused_below(unfit)
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
            raise StoreRefused(f"{HOME_PREFIX}{place} appeared meanwhile and stays unchanged") from None
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


def github_rights_prose(scope: Iterable[str]) -> str:
    """`name:level` rights as prose, one entry per permission with its levels folded, in first-appearance order."""
    levels_by_permission: dict[str, list[str]] = {}
    for right in scope:
        name, level = right.split(":", 1)
        levels_by_permission.setdefault(name, []).append(level)
    entries = []
    for name, levels in levels_by_permission.items():
        words = name.replace("_", " ")
        entries.append(f"{words[:1].upper()}{words[1:]} {' and '.join(levels)}")
    return ", ".join(entries)


def _github_briefing(_entry: SecretEntry, source: TokenSource, _now: datetime) -> list[str]:
    return [
        f"prefilled: {github_rights_prose(source.scope)}",
        "[ ] Repository access: All repositories",
        "[ ] compare the rights in the form with the line above",
        "[ ] Generate token, copy the value",
    ]


def cloudflare_page(_entry: SecretEntry, _source: TokenSource) -> str:
    """Cloudflare's token page takes no pre-filled fields; the briefing lists them."""
    return CLOUDFLARE_TOKEN_PAGE


def _cloudflare_briefing(entry: SecretEntry, source: TokenSource, now: datetime) -> list[str]:
    ends = (now + timedelta(days=source.expires_in_days)).date()
    return [
        "[ ] Create Token › Custom token › Get started",
        f"[ ] Token name: {entry.name}",
        *(f"[ ] Permissions: {right}" for right in source.scope),
        "[ ] Account Resources: Include › your account",
        f"[ ] Zone Resources: Include › Specific zone › {source.resource}",
        f"[ ] TTL: leave the start date empty, set only the End Date {ends:%d.%m.%Y}",
        "[ ] Continue to summary › Create Token, copy the value",
    ]


@dataclass(frozen=True)
class ProviderGuide:
    how: str
    page: Callable[[SecretEntry, TokenSource], str]
    briefing: Callable[[SecretEntry, TokenSource, datetime], list[str]]


GUIDES = {
    Provider.GITHUB: ProviderGuide("the link fills name, owner, expiry and rights", github_page, _github_briefing),
    Provider.CLOUDFLARE: ProviderGuide(f"{PROVIDER_LABELS[Provider.CLOUDFLARE]} fills in nothing", cloudflare_page, _cloudflare_briefing),
}


# --- the walk ---------------------------------------------------------------


class WizardEvent(StrEnum):
    """What the walk says about one entry, as the event of its row."""

    VERIFIED = "verified"
    GENERATED = "generated"
    STORED = "stored"
    NEW_TOKEN = "new token"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    NOT_CHECKED = "not checked"
    BY_HAND = "by hand"
    NOT_STORED = "not stored"


def _write_entry_row(glyph: Glyph, entry_name: str, event: WizardEvent, fact: str) -> None:
    write_row(Row(glyph, entry_name, event, fact))


def stored_row(entry: SecretEntry) -> Row:
    """The row of a stored entry: a token after its provider confirmed it, an unverified one as such, a service password unseen.

    The path and mode only; the value is never part of a row."""
    where = f"{HOME_PREFIX}{entry.place} ({entry.loosest_mode:o})"
    if entry.kind is SecretKind.UNVERIFIED_TOKEN:
        return Row(Glyph.DONE, entry.name, WizardEvent.STORED, f"not verified · {where}")
    if entry.source:
        return Row(Glyph.DONE, entry.name, WizardEvent.VERIFIED, where)
    return Row(Glyph.DONE, entry.name, WizardEvent.GENERATED, f"{where} · never shown")


def _store(entry: SecretEntry, layout: Layout, boundaries: Boundaries, value: str) -> None:
    store_new_secret(layout, entry.place, value, boundaries.sync, entry.loosest_mode)
    write_row(stored_row(entry))


def _paste_unseen(terminal: Terminal) -> str:
    """getpass falls back to echoed input when it cannot hide it, and only warns; here that warning ends the walk unread."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return terminal.ask_hidden(PASTE_PROMPT).strip()


def _is_visible_ascii(value: str) -> bool:
    """Every token the providers issue is visible ASCII; anything else is a slip of the paste and is never sent."""
    return all("!" <= character <= "~" for character in value)


def _visible_paste(entry: SecretEntry, terminal: Terminal) -> str:
    """The first pasted value that is visible ASCII; empty when the operator leaves the prompt empty."""
    while value := _paste_unseen(terminal):
        if _is_visible_ascii(value):
            return value
        _write_entry_row(Glyph.FAILED, entry.name, WizardEvent.REJECTED, f"{UNSENDABLE_PASTE} · {PASTE_AGAIN}")
    return ""


def _skipped(entry: SecretEntry) -> None:
    _write_entry_row(Glyph.UNCHANGED, entry.name, WizardEvent.SKIPPED, "nothing stored")


def _accepted_paste(entry: SecretEntry, source: TokenSource, boundaries: Boundaries) -> str | None:
    """The first pasted value the provider accepts; None when the operator leaves the prompt empty."""
    while value := _visible_paste(entry, boundaries.terminal):
        verdict = boundaries.verify(source, value)
        if verdict.outcome is Outcome.ACCEPTED:
            return value
        _write_entry_row(Glyph.FAILED, entry.name, WizardEvent.REJECTED, f"{verdict.detail} · {PASTE_AGAIN}")
    _skipped(entry)
    return None


def _shown_address(page: str) -> str:
    """Host and path of the page, with `?…` when it has a query; the scheme and the prefilled parameters stay out of the walk."""
    parts = urlsplit(page)
    return f"{parts.netloc}{parts.path}{'?…' if parts.query else ''}"


def _offer_page(terminal: Terminal, page: str) -> None:
    terminal.ask(OPEN_PROMPT)
    if terminal.open_page(page):
        write_detail(f"{OPENED} {_shown_address(page)}", tone=Tone.DIM)
    else:
        write_detail(f"{NO_BROWSER} {page}", tone=Tone.WARNING)


def _announce(entry: SecretEntry, how: str) -> None:
    """The row that opens a walk for a token, then why it is needed."""
    _write_entry_row(Glyph.ANNOUNCE, entry.name, WizardEvent.NEW_TOKEN, how)
    if entry.purpose:
        write_detail(f"why: {entry.purpose}")


def _set_up_token(entry: SecretEntry, source: TokenSource, layout: Layout, boundaries: Boundaries, now: datetime) -> bool:
    guide = GUIDES[source.provider]
    _announce(entry, guide.how)
    write_detail(*guide.briefing(entry, source, now))
    _offer_page(boundaries.terminal, guide.page(entry, source))
    value = _accepted_paste(entry, source, boundaries)
    if value is None:
        return False
    _store(entry, layout, boundaries, value)
    return True


def _set_up_unverified_token(entry: SecretEntry, layout: Layout, boundaries: Boundaries) -> bool:
    """O6: the value is asked hidden and stored without provider verification; no provider is asked, because the TURN key's token is no user token."""
    _announce(entry, f"{PROVIDER_LABELS[Provider.CLOUDFLARE]} cannot check this one")
    write_detail(f"create: {entry.recreate}")
    _offer_page(boundaries.terminal, CLOUDFLARE_DASHBOARD_PAGE)
    value = _visible_paste(entry, boundaries.terminal)
    if not value:
        _skipped(entry)
        return False
    _store(entry, layout, boundaries, value)
    return True


class Step(Enum):
    """How one missing entry left the walk."""

    STORED = "stored"
    STILL_MISSING = "still-missing"
    BY_HAND = "by-hand"


# What an unexpected error left of the one entry it stopped, as the disk shows it afterwards.
WHAT_FAILURE_LEFT = {
    Presence.PRESENT: "{name} is stored",
    Presence.MISSING: "{name} is not stored",
    Presence.UNKNOWN: "cannot tell whether {name} is stored",
}


class Interruption(StrEnum):
    """How the person ended the walk at one entry."""

    CTRL_C = "interrupted"
    INPUT_ENDED = "input ended"


class EntryInterrupted(Exception):
    """The person interrupted the walk, or its input ended, while one entry was set up."""

    def __init__(self, entry_name: str, cause: Interruption) -> None:
        super().__init__(entry_name)
        self.entry_name = entry_name
        self.cause = cause


class EntryRefused(Exception):
    """The store refused the place of one entry; the walk stops there, so one row and one pair say so."""

    def __init__(self, entry_name: str, refusal: StoreRefused) -> None:
        super().__init__(entry_name)
        self.entry_name = entry_name
        self.refusal = refusal


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
        return f"stopped at {self.entry_name} · unexpected {self.error_type} · {left}"


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
    except KeyboardInterrupt:
        raise EntryInterrupted(entry.name, Interruption.CTRL_C) from None
    except EOFError:
        raise EntryInterrupted(entry.name, Interruption.INPUT_ENDED) from None
    except getpass.GetPassWarning:
        raise
    except StoreRefused as refusal:
        raise EntryRefused(entry.name, refusal) from None
    except Exception as error:
        raise EntryFailed(entry.name, error, _presence_on_disk(entry, layout)) from None


def _set_up(entry: SecretEntry, layout: Layout, boundaries: Boundaries, now: datetime) -> Step:
    if not entry.written_by_setup:
        _write_entry_row(Glyph.NEEDS_YOU, entry.name, WizardEvent.BY_HAND, f"no wizard yet: {entry.recreate}")
        return Step.BY_HAND
    if refusal := _store_refusal(layout, entry.place):
        raise refusal
    if entry.source:
        stored = _set_up_token(entry, entry.source, layout, boundaries, now)
    elif entry.kind is SecretKind.UNVERIFIED_TOKEN:
        stored = _set_up_unverified_token(entry, layout, boundaries)
    else:
        _store(entry, layout, boundaries, secrets.token_urlsafe(PASSPHRASE_BYTES))
        stored = True
    return Step.STORED if stored else Step.STILL_MISSING


def _visit(entry: SecretEntry, layout: Layout, boundaries: Boundaries, now: datetime) -> Step | None:
    """One live entry: kept, skipped or set up; None when it is kept or optional."""
    found = _presence_on_disk(entry, layout)
    if not is_open(entry, found):
        if found is not Presence.PRESENT:
            _write_entry_row(Glyph.UNCHANGED, entry.name, WizardEvent.SKIPPED, "optional")
        return None
    if found is Presence.UNKNOWN:
        if entry.written_by_setup and (refusal := _store_refusal(layout, entry.place, unless_unsearchable=True)):
            raise EntryRefused(entry.name, refusal)
        _write_entry_row(Glyph.NEEDS_YOU, entry.name, WizardEvent.NOT_CHECKED, "its folder cannot be read")
        return Step.STILL_MISSING
    return _set_up_or_fail(entry, layout, boundaries, now)


class WalkEnd(Enum):
    """How the wizard's walk ended: the exit code the command returns, and whether the walk already wrote its own error pair."""

    DONE = (0, False)
    STILL_OPEN = (1, False)
    NO_TERMINAL = (2, True)
    CANNOT_HIDE_INPUT = (2, True)
    ENTRY_FAILED = (2, True)
    STORE_REFUSED = (1, True)
    ABORTED = (130, True)  # the shell's code for a run ended by Ctrl-C

    def __new__(cls, exit_code: int, wrote_its_pair: bool) -> WalkEnd:
        member = object.__new__(cls)
        member._value_ = len(cls.__members__)  # equal pairs stay distinct members
        member.exit_code = exit_code
        member.wrote_its_pair = wrote_its_pair
        return member


def _walk(layout: Layout, boundaries: Boundaries, now: datetime) -> WalkEnd:
    """Every live entry in manifest order; the end says whether anything is still open or left by hand."""
    still_open: list[SecretEntry] = []
    by_hand: list[SecretEntry] = []
    for entry in live_entries():
        step = _visit(entry, layout, boundaries, now)
        if step is Step.BY_HAND:
            by_hand.append(entry)
        elif step is Step.STILL_MISSING:
            still_open.append(entry)
    return WalkEnd.STILL_OPEN if still_open or by_hand else WalkEnd.DONE


SETUP_NEXT_STEP = "hal setup --apply"


def run_setup(layout: Layout, boundaries: Boundaries, now: datetime) -> WalkEnd:
    if SETUP_NEEDS_A_TERMINAL and not boundaries.terminal.is_interactive():
        write_error(
            "secrets setup needs your own terminal window · nothing changed",
            "hal setup --apply in your own terminal window",
        )
        return WalkEnd.NO_TERMINAL
    try:
        return _walk(layout, boundaries, now)
    except EntryInterrupted as interrupted:
        write_error(
            f"{interrupted.cause} at {interrupted.entry_name} · no file half written",
            SETUP_NEXT_STEP,
            fresh_line=True,
        )
        return WalkEnd.ABORTED
    except getpass.GetPassWarning:
        write_error(
            "this terminal cannot hide what you type · nothing read",
            "hal setup --apply in another terminal window",
        )
        return WalkEnd.CANNOT_HIDE_INPUT
    except EntryRefused as refused:
        _write_entry_row(Glyph.FAILED, refused.entry_name, WizardEvent.NOT_STORED, refused.refusal.reason)
        write_error(f"setup stopped at secrets · nothing stored for {refused.entry_name}", refused.refusal.fix or SETUP_NEXT_STEP)
        return WalkEnd.STORE_REFUSED
    except EntryFailed as failed:
        write_error(failed.report(), SETUP_NEXT_STEP)
        return WalkEnd.ENTRY_FAILED


# --- the `home secrets` command --------------------------------------------

SECRETS_EXAMPLES = """\
Examples:
  home secrets                shows every secret of the manifest: file, check, expiry
  home secrets --by-service   which service reads which secret (declared)
  home secrets --setup        walks through every missing secret: page, paste, check, save

Without --setup it never shows a value and changes nothing except the report
~/.local/state/home/secrets-report.md. It needs no terminal; agents may run it.
--setup runs only at your own terminal; agents never run it.
Exit code: 0 clean, 1 findings (with --setup: something is still missing or the store refuses a place), 2 invalid manifest, no terminal or
an unexpected error at one secret, 3 any other unexpected error, 130 interrupted.
"""


def _secrets_from_args(args: argparse.Namespace, layout: Layout, ports: Ports) -> int:
    if args.setup:
        return run_setup(layout, ports.secrets_setup, ports.now).exit_code
    return show_by_service() if args.by_service else show(layout, ports.probe, ports.now)


def register(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    parser = add_parser(
        "secrets",
        help="show secrets and set up missing ones; never a value",
        description="Shows every secret of the manifest, reads files by stat only and changes nothing; --setup sets up missing ones.",
        epilog=SECRETS_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--by-service", action="store_true", help="grouped by service: declared dependencies")
    mode.add_argument("--setup", action="store_true", help="walks you through every missing secret at a terminal, one after the other")
    parser.set_defaults(handler=_secrets_from_args, error_may_quote_a_secret=True)
