from __future__ import annotations

import getpass
import http.server
import json
import logging
import os
import signal
import stat
import threading
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from conftest import FAKE_SECRET_VALUE, FakeProbe, symlinked_folder_fix

from home_tools import cli, secrets_setup
from home_tools.secrets_setup import (
    BY_HAND_STEP,
    CLOUDFLARE_TOKEN_PAGE,
    EXPOSED_INPUT_REFUSAL,
    NEXT_STEP,
    NO_BROWSER,
    RETRY_STEP,
    STILL_OPEN,
    TERMINAL_BOUNDARIES,
    UNSENDABLE_PASTE,
    Boundaries,
    Outcome,
    Response,
    Terminal,
    Verdict,
    github_page,
    http_fetch,
    verify_token,
)
from home_tools.secrets_view import Provider, SecretEntry, TokenSource, load_manifest

NOW = datetime(2026, 10, 17, 9, 0)

# Two guided tokens, one generated passphrase, in the order the walk must keep.
SETUP_MANIFEST = """\
[[secret]]
name = "cloudflare/read"
kind = "token"
paths = ["~/.secrets/cloudflare/read"]
read_by = ["edge external plan"]
recreate = "https://dash.cloudflare.com/profile/api-tokens"
purpose = "liest Cloudflare"
provider = "cloudflare"
resource = "example.invalid"
scope = ["Zone › DNS › Read"]
expires_in_days = 30

[[secret]]
name = "github/read"
kind = "token"
paths = ["~/.secrets/github/read"]
read_by = ["edge external plan"]
recreate = "https://github.com/settings/personal-access-tokens/new"
purpose = "liest GitHub"
provider = "github"
resource = "example-org"
scope = ["metadata:read", "organization_secrets:read"]
expires_in_days = 366

[[secret]]
name = "opentofu/passphrase"
kind = "service-password"
paths = ["~/.secrets/opentofu/passphrase"]
read_by = ["edge external plan"]
recreate = "home secrets --setup"
purpose = "verschlüsselt den State"
"""
TOKEN_FILES = (".secrets/cloudflare/read", ".secrets/github/read")
PASSPHRASE_FILE = ".secrets/opentofu/passphrase"
EVERY_FILE = (*TOKEN_FILES, PASSPHRASE_FILE)

# An entry the wizard has no way for, one whose presence cannot be told, an optional one and a retired one it never touches.
OTHER_ENTRIES = """
[[secret]]
name = "ssh/key.ppk"
kind = "derived-mirror"
paths = ["~/.ssh/key.ppk"]
read_by = []
recreate = "puttygen"
mirror_of = "gitlab"
optional = true

[[secret]]
name = "gitlab"
kind = "token"
paths = ["~/.secrets/gitlab"]
read_by = []
recreate = "https://gitlab.example/-/user_settings/personal_access_tokens"

[[secret]]
name = "locked/token"
kind = "service-password"
paths = ["~/.secrets/locked/token"]
read_by = []
recreate = "home secrets --setup"

[[secret]]
name = "github/old"
kind = "token"
paths = ["~/.secrets/github/old"]
read_by = []
recreate = "nicht neu anlegen"
retired_on = 2026-10-14
revoke_url = "https://github.com/settings/tokens"
"""

# A pasted token carries no whitespace, so the canary's parts are joined; each part alone must stay unseen too.
PASTED_VALUE_PARTS = tuple(FAKE_SECRET_VALUE.split())
PASTED_VALUE = ".".join(PASTED_VALUE_PARTS)
WRONG_VALUE = "FAKE-WRONG-VALUE-0000"


@dataclass
class FakeTerminal:
    """A terminal whose operator pastes the queued answers; an exception class in the queue is raised instead."""

    pastes: list[str | type[BaseException]] = field(default_factory=list)
    enter: type[BaseException] | None = None
    browser_opens: bool = True
    opened: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.enter:
            raise self.enter
        return ""

    def ask_hidden(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = self.pastes.pop(0) if self.pastes else ""
        if isinstance(answer, type):
            raise answer
        return answer

    def open_page(self, page: str) -> bool:
        self.opened.append(page)
        return self.browser_opens

    def boundary(self) -> Terminal:
        return Terminal(lambda: True, self.ask, self.ask_hidden, self.open_page)


@dataclass
class FakeVerifier:
    """Accepts PASTED_VALUE and refuses anything else, unless told otherwise; runs `side_effect` on each call."""

    refusal: Verdict = Verdict(Outcome.REFUSED, "Anbieter antwortet 401")
    side_effect: Callable[[], None] = lambda: None
    checked: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, source: TokenSource, value: str) -> Verdict:
        self.checked.append((source.resource, len(value)))
        self.side_effect()
        return Verdict(Outcome.ACCEPTED) if value == PASTED_VALUE else self.refusal


@dataclass
class FakeDisk:
    """The disk under the walk; `interrupted` raises Ctrl-C while a file is synced, `ctrl_c` sends the real signal then."""

    interrupted: bool = False
    ctrl_c: bool = False

    def sync(self, descriptor: int) -> None:
        if self.interrupted:
            raise KeyboardInterrupt
        if self.ctrl_c:
            os.kill(os.getpid(), signal.SIGINT)
        os.fsync(descriptor)


@pytest.fixture
def home(tmp_path: Path, use_manifest) -> Path:
    use_manifest(SETUP_MANIFEST)
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def disk() -> FakeDisk:
    return FakeDisk()


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def boundaries(terminal: FakeTerminal, verifier: FakeVerifier, disk: FakeDisk) -> Boundaries:
    """The operator at the keyboard; the walk sees only these fakes, never a browser, a prompt or a provider."""
    return Boundaries(terminal.boundary(), verifier, disk.sync)


def home_secrets_setup(home: Path, boundaries: Boundaries = TERMINAL_BOUNDARIES) -> int:
    return cli.main(["secrets", "--setup"], home=home, probe=FakeProbe(), now=NOW, secrets_setup_boundaries=boundaries)


def private_directory(home: Path, relative: str) -> Path:
    """A directory below ~/.secrets the way a tidy one is kept: every level 0700."""
    path = home
    for part in Path(relative).parts:
        path /= part
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def files_under(home: Path) -> set[str]:
    return {str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()}


def test_the_walk_takes_every_missing_entry_in_manifest_order_and_ends_with_the_next_step(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys
) -> None:
    """#36 rule 2 / rule 5: the walk takes the missing entries one at a time in manifest order and ends with the plan step."""
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    output = capsys.readouterr().out
    assert code == 0
    headers = [line for line in output.splitlines() if line.startswith("== ")]
    assert headers == [
        "== cloudflare/read (Token, fehlt)",
        "== github/read (Token, fehlt)",
        "== opentofu/passphrase (Dienstpasswort, fehlt)",
    ]
    assert [urlsplit(page).netloc for page in terminal.opened] == ["dash.cloudflare.com", "github.com"]
    assert verifier.checked == [("example.invalid", len(PASTED_VALUE)), ("example-org", len(PASTED_VALUE))]
    assert files_under(home) == set(EVERY_FILE)
    assert output.splitlines()[-1] == NEXT_STEP


def test_every_secret_is_written_0600_in_directories_kept_0700(home: Path, terminal: FakeTerminal, boundaries: Boundaries) -> None:
    """#36 rule 2.5: every secret is written 0600 in directories kept 0700."""
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    home_secrets_setup(home, boundaries)

    assert {name: stat.S_IMODE((home / name).stat().st_mode) for name in EVERY_FILE} == dict.fromkeys(EVERY_FILE, 0o600)
    directories = {".secrets", ".secrets/cloudflare", ".secrets/github", ".secrets/opentofu"}
    assert {name: stat.S_IMODE((home / name).stat().st_mode) for name in directories} == dict.fromkeys(directories, 0o700)
    assert {name: (home / name).read_text() for name in TOKEN_FILES} == dict.fromkeys(TOKEN_FILES, PASTED_VALUE)


def test_no_value_reaches_stdout_stderr_or_a_log(home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys, caplog) -> None:
    """#36 rule 3 / #10 line 3: no pasted or generated value reaches the screen, a prompt, a page or a log."""
    caplog.set_level(logging.DEBUG)
    terminal.pastes = [WRONG_VALUE, PASTED_VALUE, PASTED_VALUE]

    home_secrets_setup(home, boundaries)

    captured = capsys.readouterr()
    passphrase = (home / PASSPHRASE_FILE).read_text()
    assert len(passphrase) >= 64
    shown = captured.out + captured.err + caplog.text + "".join(terminal.prompts) + "".join(terminal.opened)
    for value in (PASTED_VALUE, WRONG_VALUE, passphrase, *PASTED_VALUE_PARTS):
        assert value not in shown


def test_an_unexpected_error_that_quotes_the_value_ends_the_walk_unquoted_and_unwritten(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys
) -> None:
    """#36 rule 3 / #10 line 2: an error whose message carries the pasted value never shows it and writes nothing."""

    def quote_the_value() -> None:
        raise RuntimeError(PASTED_VALUE)

    verifier.side_effect = quote_the_value
    terminal.pastes = [PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.splitlines()[-1] == (
        "abgebrochen bei cloudflare/read: unerwarteter Fehler (RuntimeError); für cloudflare/read nichts gespeichert."
        " Weiter mit: home secrets --setup"
    )
    for value in (PASTED_VALUE, *PASTED_VALUE_PARTS):
        assert value not in captured.out + captured.err
    assert files_under(home) == set()


def test_an_unexpected_error_after_the_entry_is_stored_says_it_is_stored(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """#36 rule 3 / #10 line 2: an error that comes once the entry is stored says so, instead of claiming nothing was."""
    real_unlink = Path.unlink

    def refuse_to_remove_the_fresh_name(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(".neu"):
            raise PermissionError(PASTED_VALUE)
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_to_remove_the_fresh_name)
    terminal.pastes = [PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.splitlines()[-1] == (
        "abgebrochen bei cloudflare/read: unerwarteter Fehler (PermissionError); cloudflare/read ist gespeichert."
        " Weiter mit: home secrets --setup"
    )
    assert PASTED_VALUE not in captured.out + captured.err
    assert (home / TOKEN_FILES[0]).read_text() == PASTED_VALUE


def test_an_unexpected_error_that_leaves_the_entry_unstatable_claims_neither_stored_nor_missing(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """#36 rule 3 / #10 line 2: when the disk refuses to say what the error left, the report says it cannot tell."""
    secrets_dir = home / ".secrets"
    real_stat = Path.stat

    def refuse_to_say(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path.is_relative_to(secrets_dir):
            raise PermissionError(PASTED_VALUE)
        return real_stat(path, follow_symlinks=follow_symlinks)

    def hide_the_disk_and_fail() -> None:
        monkeypatch.setattr(Path, "stat", refuse_to_say)
        raise RuntimeError(PASTED_VALUE)

    verifier.side_effect = hide_the_disk_and_fail
    terminal.pastes = [PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.splitlines()[-1] == (
        "abgebrochen bei cloudflare/read: unerwarteter Fehler (RuntimeError);"
        " ob cloudflare/read gespeichert ist, ist nicht feststellbar. Weiter mit: home secrets --setup"
    )
    assert PASTED_VALUE not in captured.out + captured.err


@pytest.mark.parametrize(
    "refusal",
    [Verdict(Outcome.REFUSED, "GitHub antwortet 401"), Verdict(Outcome.UNREACHABLE, "GitHub nicht erreichbar; später noch einmal")],
    ids=["the provider refuses it", "the provider cannot be reached"],
)
def test_a_value_the_provider_does_not_confirm_is_never_written(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys, refusal: Verdict
) -> None:
    """#36 rule 2.4 / #10 line 4: a value the provider does not confirm is never written."""
    verifier.refusal = refusal
    terminal.pastes = [WRONG_VALUE, "", PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert f"abgelehnt: {refusal.detail}; nichts gespeichert. Noch einmal einfügen oder leer lassen." in lines
    assert files_under(home) == {".secrets/github/read", PASSPHRASE_FILE}
    assert lines[-2:] == [STILL_OPEN + "cloudflare/read", RETRY_STEP.format(first="")]


def interrupt_at_the_open_prompt(terminal: FakeTerminal, _disk: FakeDisk) -> None:
    terminal.enter = KeyboardInterrupt


def interrupt_at_the_paste(terminal: FakeTerminal, _disk: FakeDisk) -> None:
    terminal.pastes = [KeyboardInterrupt]


def end_the_input_at_the_paste(terminal: FakeTerminal, _disk: FakeDisk) -> None:
    terminal.pastes = [EOFError]


def interrupt_while_the_file_is_written(terminal: FakeTerminal, disk: FakeDisk) -> None:
    terminal.pastes = [PASTED_VALUE]
    disk.interrupted = True


@pytest.mark.parametrize(
    "interrupt",
    [interrupt_at_the_open_prompt, interrupt_at_the_paste, end_the_input_at_the_paste, interrupt_while_the_file_is_written],
)
def test_an_interrupted_walk_leaves_no_file_and_says_how_to_go_on(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, disk: FakeDisk, capsys, interrupt: Callable[[FakeTerminal, FakeDisk], None]
) -> None:
    """#36 rule 2.3 / #10 line 7: Ctrl-C or the end of the input leaves no file, and the next run goes on."""
    interrupt(terminal, disk)

    code = home_secrets_setup(home, boundaries)

    assert code == 130
    assert files_under(home) == set()
    assert capsys.readouterr().err.strip() == "abgebrochen; keine Datei halb geschrieben. Weiter mit: home secrets --setup"


@pytest.fixture
def ctrl_c_interrupts() -> Iterator[None]:
    """Ctrl-C raises KeyboardInterrupt, as at the operator's terminal, whatever the test runner set."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.usefixtures("ctrl_c_interrupts")
def test_a_ctrl_c_while_a_secret_is_stored_waits_until_the_file_is_whole(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, disk: FakeDisk, capsys
) -> None:
    """#36 rule 2.3 / #10 line 7: a Ctrl-C while a secret is stored waits until the file is whole, never half-made."""
    terminal.pastes = [PASTED_VALUE]
    disk.ctrl_c = True

    code = home_secrets_setup(home, boundaries)

    assert code == 130
    assert capsys.readouterr().err.strip() == "abgebrochen; keine Datei halb geschrieben. Weiter mit: home secrets --setup"
    assert {path: (home / path).read_text() for path in files_under(home)} == {TOKEN_FILES[0]: PASTED_VALUE}


def test_a_secret_already_present_stays_untouched_and_its_page_is_not_opened(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 4: a secret already present stays untouched and its page is not opened."""
    existing = private_directory(home, ".secrets/cloudflare") / "read"
    existing.write_text("FAKE-OLD-VALUE")
    before = existing.stat()
    terminal.pastes = [PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    assert code == 0
    assert "cloudflare/read: vorhanden, bleibt unverändert" in capsys.readouterr().out.splitlines()
    assert existing.read_text() == "FAKE-OLD-VALUE"
    assert (existing.stat().st_mtime_ns, existing.stat().st_mode) == (before.st_mtime_ns, before.st_mode)
    assert [urlsplit(page).netloc for page in terminal.opened] == ["github.com"]


def test_a_file_that_appears_while_the_operator_pastes_is_never_replaced(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys
) -> None:
    """#36 rule 4: a file that appears while the operator pastes is never replaced."""
    rival = home / ".secrets" / "cloudflare" / "read"

    def another_run_stores_it_first() -> None:
        if not rival.exists():
            private_directory(home, ".secrets/cloudflare")
            rival.write_text("FAKE-RIVAL-VALUE")

    verifier.side_effect = another_run_stores_it_first
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    assert code == 1
    assert "nicht gespeichert: ~/.secrets/cloudflare/read ist inzwischen vorhanden und bleibt unverändert" in capsys.readouterr().out
    assert rival.read_text() == "FAKE-RIVAL-VALUE"
    assert files_under(home) == set(EVERY_FILE)


def test_a_directory_that_turns_into_a_file_while_the_operator_pastes_is_refused_at_the_store(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys
) -> None:
    """#36 rule 2.5: the store judges the directories again, so one replaced after the check before the page is never used."""
    directory = private_directory(home, ".secrets/github")

    def replace_the_directory_by_a_file() -> None:
        directory.rmdir()
        directory.write_text("not a directory")

    verifier.side_effect = replace_the_directory_by_a_file
    terminal.pastes = ["", PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    assert code == 1
    refused = "nicht gespeichert: ~/.secrets/github ist kein Ordner: erst `mv -n ~/.secrets/github ~/.secrets/github.alt`"
    assert refused in capsys.readouterr().out.splitlines()
    assert files_under(home) == {".secrets/github", PASSPHRASE_FILE}


def loosen_the_secrets_directory(home: Path) -> tuple[str, str]:
    (home / ".secrets").mkdir(mode=0o700)
    (home / ".secrets").chmod(0o755)
    return "~/.secrets hat Rechte 755, höchstens 700", "`chmod 700 ~/.secrets`"


def make_the_secrets_directory_a_symlink(home: Path) -> tuple[str, str]:
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (home / ".secrets").symlink_to(elsewhere)
    return "~/.secrets ist ein Symlink", f"`{symlinked_folder_fix('~/.secrets')}`"


@pytest.mark.parametrize("damage", [loosen_the_secrets_directory, make_the_secrets_directory_a_symlink])
def test_a_secrets_directory_that_is_not_private_is_refused_before_any_page_opens_and_nothing_is_written(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys, damage: Callable[[Path], tuple[str, str]]
) -> None:
    """#36 rule 2.5 / #17 finding 21 (c, d): an unfit secrets directory is named before a page opens, so no fresh token is lost;
    the one `next:` line repeats the command that clears it before the walk again."""
    reason, fix = damage(home)
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    output = capsys.readouterr().out
    assert code == 1
    assert output.count(f"nicht gespeichert: {reason}: erst {fix}") == 3
    assert [line for line in output.splitlines() if line.startswith("next:")] == [RETRY_STEP.format(first=f"{fix}, dann ")]
    assert (terminal.opened, terminal.prompts) == ([], [])
    assert files_under(home.parent) <= {"secrets_manifest.toml", "home/.secrets"}


def test_entries_without_a_wizard_are_named_with_their_page_and_unknown_or_retired_ones_are_left_alone(
    home: Path, use_manifest, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 2 / rule 5 / #17 finding 21 (a): an entry without a wizard is named with its page and again at the end,
    an unknown one stays open, an optional or retired one is left alone."""
    use_manifest(SETUP_MANIFEST + OTHER_ENTRIES)
    private_directory(home, ".secrets/locked").chmod(0o000)
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]
    try:
        code = home_secrets_setup(home, boundaries)
    finally:
        (home / ".secrets" / "locked").chmod(0o700)

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert "Dafür gibt es noch keinen Assistenten; von Hand: https://gitlab.example/-/user_settings/personal_access_tokens" in lines
    assert "locked/token: nicht feststellbar, ob vorhanden (Ordner nicht lesbar); übersprungen" in lines
    assert not any("github/old" in line for line in lines)
    assert "ssh/key.ppk: fehlt, ist optional; übersprungen" in lines
    assert lines[-3:] == [
        STILL_OPEN + "locked/token",
        BY_HAND_STEP + "gitlab",
        RETRY_STEP.format(first="`chmod 700 ~/.secrets/locked`, dann "),
    ]


def test_without_a_terminal_setup_is_refused_and_nothing_is_written(home: Path, capsys) -> None:
    """#36 rule 2 / #10 line 10: without a terminal --setup is refused and nothing is written."""
    code = home_secrets_setup(home)

    assert code == 2
    assert capsys.readouterr().err.startswith("home secrets --setup läuft nur am eigenen Terminal")
    assert list(home.iterdir()) == []


def test_a_terminal_that_cannot_hide_the_paste_is_refused_before_anything_is_read(
    home: Path, verifier: FakeVerifier, disk: FakeDisk, capsys
) -> None:
    """#36 rule 2.3 / #10 line 3: a terminal that cannot hide the paste is refused before anything is read."""
    echoing = Terminal(lambda: True, lambda _prompt: "", getpass.fallback_getpass, lambda _page: True)

    code = home_secrets_setup(home, Boundaries(echoing, verifier, disk.sync))

    assert code == 2
    assert capsys.readouterr().err.strip() == EXPOSED_INPUT_REFUSAL
    assert verifier.checked == []
    assert files_under(home) == set()


def test_the_briefing_names_the_page_the_login_and_every_right_to_tick(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 2.1: the briefing names the page, the login and every right to tick."""
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    home_secrets_setup(home, boundaries)

    output = capsys.readouterr().out
    cloudflare, github = output.split("== github/read")[0], output.split("== github/read")[1].split("== opentofu")[0]
    assert "Wofür: liest Cloudflare" in cloudflare
    assert "Login: dein Cloudflare-Konto." in cloudflare
    assert "  [ ] Permissions: Zone › DNS › Read" in cloudflare
    assert "  [ ] Zone Resources: Include › Specific zone › example.invalid" in cloudflare
    assert "  [ ] TTL: leave the start date empty, set only the End Date 16.11.2026" in cloudflare
    assert f"Geöffnet: {CLOUDFLARE_TOKEN_PAGE}" in cloudflare
    assert "  - organization_secrets: read" in github
    assert "  [ ] Repository access: All repositories" in github
    assert "Wird zufällig erzeugt und nie gezeigt." in output


def test_a_page_no_browser_opens_is_named_for_opening_by_hand_and_the_walk_goes_on(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 2.2: a page no browser opens is named for opening by hand, never reported as opened."""
    terminal.browser_opens = False
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    code = home_secrets_setup(home, boundaries)

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert f"{NO_BROWSER} {CLOUDFLARE_TOKEN_PAGE}" in lines
    assert not any(line.startswith("Geöffnet:") for line in lines)
    assert files_under(home) == set(EVERY_FILE)


def github_entry() -> SecretEntry:
    (entry,) = (entry for entry in load_manifest(secrets_setup.secrets_view.MANIFEST_PATH) if entry.name == "github/read")
    return entry


def test_the_github_page_pre_fills_every_documented_field(home: Path) -> None:
    """#36 rule 2.2: the GitHub page pre-fills every field its URL parameters take."""
    entry = github_entry()

    page = urlsplit(github_page(entry, entry.source))

    assert (page.scheme, page.netloc, page.path) == ("https", "github.com", "/settings/personal-access-tokens/new")
    assert parse_qsl(page.query) == [
        ("name", "github/read"),
        ("description", "liest GitHub"),
        ("target_name", "example-org"),
        ("expires_in", "366"),
        ("metadata", "read"),
        ("organization_secrets", "read"),
    ]


GITHUB = TokenSource(Provider.GITHUB, "example-org", ("metadata:read",), 366)
CLOUDFLARE = TokenSource(Provider.CLOUDFLARE, "example.invalid", ("Zone › DNS › Read",), 30)


CLOUDFLARE_DISABLED = "Cloudflare meldet das Token als disabled, nicht active"
CLOUDFLARE_UNREADABLE = "Cloudflare meldet das Token als unbekannt, nicht active"


def cloudflare_answer(status: object, not_before: object = None, *, with_start: bool = False) -> bytes:
    result = {"id": "0", "status": status, **({"not_before": not_before} if with_start or not_before else {})}
    return json.dumps({"success": True, "result": result}).encode()


CLOUDFLARE_START = "2026-12-30T23:00:00Z"


def not_yet_valid(start_in_utc: str) -> str:
    return (
        f"Cloudflare reports the token as active, but it is not valid before {start_in_utc} UTC; "
        "clear the start date on the token's Edit page in Cloudflare, then paste again"
    )


CLOUDFLARE_NOT_YET_VALID = not_yet_valid("30.12.2026 23:00")


@pytest.mark.parametrize(
    ("source", "response", "expected"),
    [
        (GITHUB, Response(200, b"{}"), Verdict(Outcome.ACCEPTED)),
        (GITHUB, Response(401, b""), Verdict(Outcome.REFUSED, "GitHub antwortet 401")),
        (CLOUDFLARE, Response(200, cloudflare_answer("active")), Verdict(Outcome.ACCEPTED)),
        (CLOUDFLARE, Response(200, cloudflare_answer("disabled")), Verdict(Outcome.REFUSED, CLOUDFLARE_DISABLED)),
        (CLOUDFLARE, Response(200, b"[]"), Verdict(Outcome.REFUSED, CLOUDFLARE_UNREADABLE)),
        (CLOUDFLARE, Response(200, b"not json"), Verdict(Outcome.REFUSED, CLOUDFLARE_UNREADABLE)),
        (CLOUDFLARE, Response(403, b""), Verdict(Outcome.REFUSED, "Cloudflare antwortet 403")),
        (CLOUDFLARE, None, Verdict(Outcome.UNREACHABLE, "Cloudflare nicht erreichbar; später noch einmal")),
    ],
)
def test_a_token_counts_only_once_its_provider_confirms_it(source: TokenSource, response: Response | None, expected: Verdict) -> None:
    """#36 rule 2.4 / #10 line 4: a token counts only once its provider confirms it."""
    assert verify_token(source, PASTED_VALUE, lambda _request: response) == expected


CLOUDFLARE_UNREADABLE_START = (
    "Cloudflare's answer has a start date that cannot be read; check the token's TTL on its Edit page, then paste again"
)
CLOUDFLARE_START_INSTANT = datetime(2026, 12, 30, 23, 0, tzinfo=UTC)
REFUSED_UNTIL_START = Verdict(Outcome.REFUSED, CLOUDFLARE_NOT_YET_VALID)
ACCEPTED = Verdict(Outcome.ACCEPTED)


@pytest.mark.parametrize(
    ("start", "now", "expected"),
    [
        (CLOUDFLARE_START, datetime(2026, 10, 19, tzinfo=UTC), REFUSED_UNTIL_START),
        (CLOUDFLARE_START, CLOUDFLARE_START_INSTANT + timedelta(seconds=1), ACCEPTED),
        (CLOUDFLARE_START, CLOUDFLARE_START_INSTANT, ACCEPTED),
        ("2026-12-31T00:00:00+01:00", CLOUDFLARE_START_INSTANT, ACCEPTED),
        ("2026-12-30T22:30:00-01:00", CLOUDFLARE_START_INSTANT, Verdict(Outcome.REFUSED, not_yet_valid("30.12.2026 23:30"))),
        ("2026-12-31T00:00:01+01:00", CLOUDFLARE_START_INSTANT, REFUSED_UNTIL_START),
        ("2026-12-30T23:00:00", CLOUDFLARE_START_INSTANT - timedelta(seconds=1), REFUSED_UNTIL_START),
        ("2026-12-30T23:00:00", CLOUDFLARE_START_INSTANT, ACCEPTED),
        (None, CLOUDFLARE_START_INSTANT, ACCEPTED),
        ("invalid", CLOUDFLARE_START_INSTANT, Verdict(Outcome.REFUSED, CLOUDFLARE_UNREADABLE_START)),
        (1767222000, CLOUDFLARE_START_INSTANT, Verdict(Outcome.REFUSED, CLOUDFLARE_UNREADABLE_START)),
    ],
    ids=[
        "start in the future",
        "start in the past",
        "start exactly now",
        "positive offset, exactly now",
        "negative offset, still ahead",
        "positive offset, one second ahead",
        "no offset is read as UTC, ahead",
        "no offset is read as UTC, exactly now",
        "null start",
        "unreadable text",
        "not text",
    ],
)
def test_a_cloudflare_token_counts_only_from_its_start_date(start: object, now: datetime, expected: Verdict) -> None:
    """#54 rule 1: an active token whose start date lies ahead or cannot be read is refused; one without or past it passes."""
    response = Response(200, cloudflare_answer("active", start, with_start=True))

    assert verify_token(CLOUDFLARE, PASTED_VALUE, lambda _request: response, lambda: now) == expected


def test_the_default_clock_refuses_a_start_date_far_in_the_future() -> None:
    response = Response(200, cloudflare_answer("active", "9999-01-01T00:00:00Z"))

    assert verify_token(CLOUDFLARE, PASTED_VALUE, lambda _request: response).outcome is Outcome.REFUSED


@pytest.mark.parametrize(
    ("source", "url"),
    [
        (GITHUB, "https://api.github.com/orgs/example-org/actions/secrets?per_page=1"),
        (CLOUDFLARE, "https://api.cloudflare.com/client/v4/user/tokens/verify"),
    ],
)
def test_the_check_asks_the_provider_with_the_token_as_a_bearer(source: TokenSource, url: str) -> None:
    """#36 rule 2.4: the check asks the provider's address with the token as a bearer."""
    sent: list[urllib.request.Request] = []

    verify_token(source, PASTED_VALUE, lambda request: sent.append(request))

    (request,) = sent
    assert (request.get_method(), request.full_url) == ("GET", url)
    assert request.get_header("Authorization") == f"Bearer {PASTED_VALUE}"


class _Answers(http.server.BaseHTTPRequestHandler):
    """Answers /ok, sends /redirect on to /ok, and notes the Authorization each path received."""

    seen: list[tuple[str, str | None]]

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/not-yet-valid":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(cloudflare_answer("active", CLOUDFLARE_START))
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        status = 200 if self.path == "/ok" else 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok" if status == 200 else b"")

    def log_message(self, *_args: object) -> None:
        """Silent: a test server never writes to stderr."""


@dataclass(frozen=True)
class LocalServer:
    url: str
    seen: list[tuple[str, str | None]]

    def fetch(self, path: str) -> Callable[[urllib.request.Request], Response | None]:
        """The real fetch, sent here instead of to the provider: the request verify_token built, headers and all."""

        def fetch(request: urllib.request.Request) -> Response | None:
            request.full_url = f"{self.url}{path}"
            return http_fetch(request)

        return fetch


@pytest.fixture
def local_server() -> Iterator[LocalServer]:
    seen: list[tuple[str, str | None]] = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Answers", (_Answers,), {"seen": seen}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalServer(f"http://127.0.0.1:{server.server_address[1]}", seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_fetch_returns_the_status_and_body_or_none_when_nothing_answers(local_server: LocalServer) -> None:
    """#36 rule 2.4: an answer is told apart from no answer, so an unreachable provider is never a refusal."""
    closed_port = local_server.url.rsplit(":", 1)[0] + ":1"

    assert http_fetch(urllib.request.Request(f"{local_server.url}/ok")) == Response(200, b"ok")
    assert http_fetch(urllib.request.Request(f"{local_server.url}/missing")) == Response(404, b"")
    assert http_fetch(urllib.request.Request(f"{closed_port}/ok")) is None


def test_a_redirect_never_carries_the_token_on(local_server: LocalServer) -> None:
    """#36 rule 3: the bearer reaches the provider's address only; the place a redirect names never receives it."""
    verify_token(GITHUB, PASTED_VALUE, local_server.fetch("/redirect"))

    assert local_server.seen == [("/redirect", f"Bearer {PASTED_VALUE}"), ("/ok", None)]


UNSENDABLE_CHARACTERS = {"carriage return": "\r", "line feed": "\n", "nul": "\x00", "tab": "\t", "space": " ", "euro sign": "€"}


def canary_with(character: str) -> str:
    return f"FAKE-CANARY-HEAD{character}FAKE-CANARY-TAIL"


@pytest.mark.parametrize("character", UNSENDABLE_CHARACTERS.values(), ids=UNSENDABLE_CHARACTERS.keys())
def test_a_paste_that_is_not_visible_ascii_is_refused_unsent_unshown_and_unwritten(
    home: Path, terminal: FakeTerminal, local_server: LocalServer, capsys, character: str
) -> None:
    """#36 rule 3 / #10 line 2: a paste no token can be is refused with a fixed line; it is never sent, shown or written."""
    canary = canary_with(character)
    real_check = Boundaries(terminal.boundary(), lambda source, value: verify_token(source, value, local_server.fetch("/ok")), os.fsync)
    terminal.pastes = [canary, "", canary, ""]

    code = home_secrets_setup(home, real_check)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.splitlines().count(UNSENDABLE_PASTE) == 2
    for part in (canary, "FAKE-CANARY-HEAD", "FAKE-CANARY-TAIL"):
        assert part not in captured.out + captured.err
    assert local_server.seen == []
    assert files_under(home) == {PASSPHRASE_FILE}


def test_the_walk_refuses_a_token_with_a_future_start_date_and_writes_nothing(
    home: Path, terminal: FakeTerminal, local_server: LocalServer, capsys
) -> None:
    """#54 rule 1 / #10 line 22: a rejected value is refused with one line naming the start date and is never written."""
    now = datetime(2026, 10, 19, tzinfo=UTC)
    real_check = Boundaries(
        terminal.boundary(), lambda source, value: verify_token(source, value, local_server.fetch("/not-yet-valid"), lambda: now), os.fsync
    )
    terminal.pastes = [PASTED_VALUE, "", ""]

    code = home_secrets_setup(home, real_check)

    captured = capsys.readouterr()
    assert code == 1
    assert f"abgelehnt: {CLOUDFLARE_NOT_YET_VALID}; nichts gespeichert." in captured.out
    assert PASTED_VALUE not in captured.out + captured.err
    assert files_under(home) == {PASSPHRASE_FILE}


@pytest.mark.parametrize("character", ["\r", "\n", "€"], ids=["carriage return", "line feed", "euro sign"])
def test_a_value_http_cannot_send_is_refused_without_quoting_it(local_server: LocalServer, character: str) -> None:
    """#36 rule 3 / #10 line 2: a value the HTTP layer refuses to send is a refusal whose detail never quotes the value."""
    verdict = verify_token(GITHUB, canary_with(character), local_server.fetch("/ok"))

    assert verdict == Verdict(Outcome.REFUSED, "der Wert lässt sich GitHub nicht als Token senden")


def test_a_service_password_is_generated_fresh_for_each_home(
    tmp_path: Path, use_manifest, terminal: FakeTerminal, boundaries: Boundaries
) -> None:
    """#36 rule 2 / #10 line 12: a service password is generated fresh for each home."""
    use_manifest(SETUP_MANIFEST)
    passphrases = []
    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        terminal.pastes = ["", ""]
        home_secrets_setup(home, boundaries)
        passphrases.append((home / PASSPHRASE_FILE).read_text())

    assert passphrases[0] != passphrases[1]
    assert all(os.path.getsize(tmp_path / name / PASSPHRASE_FILE) >= 64 for name in ("first", "second"))
