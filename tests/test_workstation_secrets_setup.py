from __future__ import annotations

import getpass
import logging
import os
import signal
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from workstation_support import FAKE_SECRET_VALUE, symlinked_folder_fix
from workstation_secrets_support import (
    FakeCli,
    FakeSync,
    FakeTerminal,
    guided_boundaries,
    run_wizard,
)

from hal.workstation import secrets_setup
from hal.workstation.secrets_setup import (
    CLOUDFLARE_TOKEN_PAGE,
    NO_BROWSER,
    OPEN_PROMPT,
    PASTE_PROMPT,
    Boundaries,
    Outcome,
    Verdict,
    WalkEnd,
    github_page,
)
from hal.workstation.secrets_view import MANIFEST_PATH as PACKAGED_MANIFEST
from hal.workstation import secrets_view
from hal.workstation.secrets_view import SecretEntry, SecretKind, TokenSource, load_manifest

# Two guided tokens, one generated passphrase, in the order the walk must keep.
SETUP_MANIFEST = """\
[[secret]]
name = "cloudflare/read"
kind = "token"
paths = ["~/.secrets/cloudflare/read"]
read_by = ["hal apply"]
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
read_by = ["hal apply"]
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
read_by = ["hal apply"]
recreate = "hal setup"
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
recreate = "hal setup"

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
class FakeVerifier:
    """Accepts PASTED_VALUE and refuses anything else, unless told otherwise; runs `side_effect` on each call."""

    refusal: Verdict = Verdict(Outcome.REFUSED, "Provider answers 401")
    side_effect: Callable[[], None] = lambda: None
    checked: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, source: TokenSource, value: str) -> Verdict:
        self.checked.append((source.resource, len(value)))
        self.side_effect()
        return Verdict(Outcome.ACCEPTED) if value == PASTED_VALUE else self.refusal


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
def disk() -> FakeSync:
    return FakeSync()


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def cli() -> FakeCli:
    return FakeCli()


@pytest.fixture
def boundaries(
    terminal: FakeTerminal, verifier: FakeVerifier, disk: FakeSync, cli: FakeCli
) -> Boundaries:
    """The operator at the keyboard; the walk sees only these fakes, never a browser, a prompt, a provider or a CLI."""
    return Boundaries(terminal.boundary(), verifier, disk.sync, cli)


def squeezed(output: str) -> list[str]:
    """The output's lines with each run of spaces squeezed to one, so a row reads as its columns in order."""
    return [" ".join(line.split()) for line in output.splitlines()]


def private_directory(home: Path, relative: str) -> Path:
    """A directory below ~/.secrets the way a tidy one is kept: every level 0700."""
    path = home
    for part in Path(relative).parts:
        path /= part
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def files_under(home: Path) -> set[str]:
    return {str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()}


def test_every_secret_is_written_0600_in_directories_kept_0700(home: Path, terminal: FakeTerminal, boundaries: Boundaries) -> None:
    """#36 rule 2.5: every secret is written 0600 in directories kept 0700."""
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    run_wizard(home, boundaries)

    assert {name: stat.S_IMODE((home / name).stat().st_mode) for name in EVERY_FILE} == dict.fromkeys(EVERY_FILE, 0o600)
    directories = {".secrets", ".secrets/cloudflare", ".secrets/github", ".secrets/opentofu"}
    assert {name: stat.S_IMODE((home / name).stat().st_mode) for name in directories} == dict.fromkeys(directories, 0o700)
    assert {name: (home / name).read_text() for name in TOKEN_FILES} == dict.fromkeys(TOKEN_FILES, PASTED_VALUE)


@pytest.mark.parametrize("browser_opens", [True, False], ids=["the browser opens", "no browser opens"])
def test_no_value_reaches_stdout_stderr_or_a_log(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys, caplog, browser_opens: bool
) -> None:
    """#36 rule 3 / #10 line 3: no pasted or generated value reaches the screen, a prompt, a page or a log."""
    caplog.set_level(logging.DEBUG)
    terminal.browser_opens = browser_opens
    terminal.pastes = [WRONG_VALUE, PASTED_VALUE, PASTED_VALUE]

    run_wizard(home, boundaries)

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

    end = run_wizard(home, boundaries)

    captured = capsys.readouterr()
    assert end is WalkEnd.ENTRY_FAILED
    assert captured.err.endswith("error: stopped at cloudflare/read · unexpected RuntimeError · cloudflare/read is not stored\nnext: hal setup\n")
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

    end = run_wizard(home, boundaries)

    captured = capsys.readouterr()
    assert end is WalkEnd.ENTRY_FAILED
    assert captured.err.endswith("error: stopped at cloudflare/read · unexpected PermissionError · cloudflare/read is stored\nnext: hal setup\n")
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

    end = run_wizard(home, boundaries)

    captured = capsys.readouterr()
    assert end is WalkEnd.ENTRY_FAILED
    assert captured.err.endswith("error: stopped at cloudflare/read · unexpected RuntimeError · cannot tell whether cloudflare/read is stored\nnext: hal setup\n")
    assert PASTED_VALUE not in captured.out + captured.err


@pytest.mark.parametrize(
    "refusal",
    [Verdict(Outcome.REFUSED, "GitHub answers 401"), Verdict(Outcome.UNREACHABLE, "GitHub unreachable; try again later")],
    ids=["the provider refuses it", "the provider cannot be reached"],
)
def test_a_value_the_provider_does_not_confirm_is_never_written(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys, refusal: Verdict
) -> None:
    """#36 rule 2.4 / #10 line 4: a value the provider does not confirm is never written."""
    verifier.refusal = refusal
    terminal.pastes = [WRONG_VALUE, "", PASTED_VALUE]

    end = run_wizard(home, boundaries)

    lines = squeezed(capsys.readouterr().out)
    assert end is WalkEnd.STILL_OPEN
    assert f"✗ cloudflare/read rejected {refusal.detail} · paste again or leave empty" in lines
    assert "· cloudflare/read skipped nothing stored" in lines
    assert files_under(home) == {".secrets/github/read", PASSPHRASE_FILE}
    assert not [line for line in lines if line.startswith("next:") or "offen" in line]


def interrupt_at_the_open_prompt(terminal: FakeTerminal, _disk: FakeSync) -> None:
    terminal.enter = KeyboardInterrupt


def interrupt_at_the_paste(terminal: FakeTerminal, _disk: FakeSync) -> None:
    terminal.pastes = [KeyboardInterrupt]


def end_the_input_at_the_paste(terminal: FakeTerminal, _disk: FakeSync) -> None:
    terminal.pastes = [EOFError]


def interrupt_while_the_file_is_written(terminal: FakeTerminal, disk: FakeSync) -> None:
    terminal.pastes = [PASTED_VALUE]
    disk.interrupted = True


@pytest.mark.parametrize(
    "interrupt, expected_error",
    [
        (interrupt_at_the_open_prompt, "error: interrupted at cloudflare/read · no file half written"),
        (interrupt_at_the_paste, "error: interrupted at cloudflare/read · no file half written"),
        (end_the_input_at_the_paste, "error: input ended at cloudflare/read · no file half written"),
        (interrupt_while_the_file_is_written, "error: interrupted at cloudflare/read · no file half written"),
    ],
    ids=["open prompt", "paste", "input ended", "file write"],
)
def test_an_interrupted_walk_leaves_no_file_and_says_how_to_go_on(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, disk: FakeSync, capsys, interrupt: Callable[[FakeTerminal, FakeSync], None], expected_error: str
) -> None:
    """#36 rule 2.3 / #10 line 7: Ctrl-C or the end of the input leaves no file, and the next run goes on."""
    interrupt(terminal, disk)

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.ABORTED
    assert files_under(home) == set()
    assert capsys.readouterr().err == f"\n{expected_error}\nnext: hal setup\n"


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
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, disk: FakeSync, capsys
) -> None:
    """#36 rule 2.3 / #10 line 7: a Ctrl-C while a secret is stored waits until the file is whole, never half-made."""
    terminal.pastes = [PASTED_VALUE]
    disk.ctrl_c = True

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.ABORTED
    assert capsys.readouterr().err.endswith("error: interrupted at cloudflare/read · no file half written\nnext: hal setup\n")
    assert {path: (home / path).read_text() for path in files_under(home)} == {TOKEN_FILES[0]: PASTED_VALUE}


def test_a_secret_already_present_stays_untouched_and_its_page_is_not_opened(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 4: a secret already present stays untouched and its page is not opened."""
    existing = private_directory(home, ".secrets/cloudflare") / "read"
    existing.write_text("FAKE-OLD-VALUE")
    before = existing.stat()
    terminal.pastes = [PASTED_VALUE]

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.DONE
    assert not [line for line in squeezed(capsys.readouterr().out) if "cloudflare/read" in line and line[0] in "✓✗!·"]
    assert existing.read_text() == "FAKE-OLD-VALUE"
    assert (existing.stat().st_mtime_ns, existing.stat().st_mode) == (before.st_mtime_ns, before.st_mode)
    assert [urlsplit(page).netloc for page in terminal.opened] == ["github.com"]


@pytest.mark.parametrize("present", [False, True], ids=["absent", "present at 0644"])
def test_setup_never_writes_the_passwords_step_ca_init_and_keycloak_go_live_generate(
    home: Path, use_manifest, boundaries: Boundaries, present: bool
) -> None:
    """#195: those units own their four generated passwords; the packaged entries are optional, so `--setup` leaves them as they are."""
    generated = ("step-ca/password", "keycloak/db-password", "keycloak/config-cli-secret", "keycloak/bootstrap-admin-password")
    blocks = PACKAGED_MANIFEST.read_text().split("[[secret]]")[1:]
    use_manifest("".join(f"[[secret]]{block}" for block in blocks if any(f'name = "{name}"' in block for name in generated)))
    for name in generated if present else ():
        file = private_directory(home, f".secrets/{Path(name).parent}") / Path(name).name
        file.write_text("FAKE-OLD-VALUE")
        file.chmod(0o644)

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.DONE
    assert {path: (home / path).read_text() for path in files_under(home)} == {f".secrets/{name}": "FAKE-OLD-VALUE" for name in generated if present}


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

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.STORE_REFUSED
    captured = capsys.readouterr()
    assert "✗ cloudflare/read not stored ~/.secrets/cloudflare/read appeared meanwhile and stays unchanged" in squeezed(captured.out)
    assert captured.err == "error: setup stopped at secrets · nothing stored for cloudflare/read\nnext: hal setup\n"
    assert PASTED_VALUE not in captured.out + captured.err
    assert rival.read_text() == "FAKE-RIVAL-VALUE"
    assert files_under(home) == {".secrets/cloudflare/read"}


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

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.STORE_REFUSED
    captured = capsys.readouterr()
    assert "✗ github/read not stored ~/.secrets/github is not a folder" in squeezed(captured.out)
    assert captured.err.splitlines() == [
        "error: setup stopped at secrets · nothing stored for github/read",
        "next: mv -n ~/.secrets/github ~/.secrets/github.alt",
    ]
    assert PASTED_VALUE not in captured.out + captured.err
    assert files_under(home) == {".secrets/github"}


def test_a_group_that_is_a_file_when_the_walk_starts_is_refused_before_any_page_opens(home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys) -> None:
    private_directory(home, ".secrets").joinpath("cloudflare").write_text("not a directory")

    end = run_wizard(home, boundaries)

    assert end is WalkEnd.STORE_REFUSED
    captured = capsys.readouterr()
    assert "✗ cloudflare/read not stored ~/.secrets/cloudflare is not a folder" in squeezed(captured.out)
    assert "not checked" not in captured.out
    assert captured.err.splitlines()[-1] == "next: mv -n ~/.secrets/cloudflare ~/.secrets/cloudflare.alt"
    assert (terminal.opened, terminal.prompts) == ([], [])


def loosen_the_secrets_directory(home: Path) -> tuple[str, str]:
    (home / ".secrets").mkdir(mode=0o700)
    (home / ".secrets").chmod(0o755)
    return "~/.secrets has mode 755; at most 700 allowed", "chmod 700 ~/.secrets"


def loosen_the_secrets_directory_above_a_group_that_is_a_file(home: Path) -> tuple[str, str]:
    reason, fix = loosen_the_secrets_directory(home)
    (home / ".secrets" / "cloudflare").write_text("not a directory")
    return reason, fix


def make_the_secrets_directory_a_symlink(home: Path) -> tuple[str, str]:
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (home / ".secrets").symlink_to(elsewhere)
    return "~/.secrets is a symlink", symlinked_folder_fix("~/.secrets")


@pytest.mark.parametrize(
    "damage",
    [loosen_the_secrets_directory, loosen_the_secrets_directory_above_a_group_that_is_a_file, make_the_secrets_directory_a_symlink],
)
def test_a_secrets_directory_that_is_not_private_is_refused_before_any_page_opens_and_nothing_is_written(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys, damage: Callable[[Path], tuple[str, str]]
) -> None:
    """#36 rule 2.5 / #17 finding 21 (c, d): an unfit secrets directory is named before a page opens, so no fresh token is lost;
    the walk stops at the first entry with one row and the pair that names the fix."""
    reason, fix = damage(home)
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]

    end = run_wizard(home, boundaries)

    captured = capsys.readouterr()
    assert end is WalkEnd.STORE_REFUSED
    assert [line for line in squeezed(captured.out) if "not stored" in line] == [f"✗ cloudflare/read not stored {reason}"]
    assert captured.err.splitlines() == ["error: setup stopped at secrets · nothing stored for cloudflare/read", f"next: {fix}"]
    assert PASTED_VALUE not in captured.out + captured.err
    assert (terminal.opened, terminal.prompts) == ([], [])
    assert files_under(home.parent) <= {"secrets_manifest.toml", "home/.secrets", "home/.secrets/cloudflare"}


def test_entries_without_a_wizard_are_named_with_their_page_and_unknown_or_retired_ones_are_left_alone(
    home: Path, use_manifest, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 2 / rule 5 / #17 finding 21 (a): an entry without a wizard is named with its page and again at the end,
    an unknown one stays open, an optional or retired one is left alone."""
    use_manifest(SETUP_MANIFEST + OTHER_ENTRIES)
    private_directory(home, ".secrets/locked").chmod(0o000)
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]
    try:
        end = run_wizard(home, boundaries)
    finally:
        (home / ".secrets" / "locked").chmod(0o700)

    lines = squeezed(capsys.readouterr().out)
    assert end is WalkEnd.STILL_OPEN
    assert "! gitlab by hand no wizard yet: https://gitlab.example/-/user_settings/personal_access_tokens" in lines
    assert "! locked/token not checked its folder cannot be read" in lines
    assert not any("github/old" in line or line.startswith(("› gitlab", "› locked")) for line in lines)
    assert "· ssh/key.ppk skipped optional" in lines
    assert not [line for line in lines if line.startswith("next:") or "offen" in line]


def test_a_terminal_that_cannot_hide_the_paste_is_refused_before_anything_is_read(
    home: Path, verifier: FakeVerifier, capsys
) -> None:
    """#36 rule 2.3 / #10 line 3: a terminal that cannot hide the paste is refused before anything is read."""
    end = run_wizard(home, guided_boundaries(getpass.fallback_getpass, verifier))

    assert end is WalkEnd.CANNOT_HIDE_INPUT
    assert capsys.readouterr().err.endswith("error: this terminal cannot hide what you type · nothing read\nnext: hal setup in another terminal window\n")
    assert verifier.checked == []
    assert files_under(home) == set()


def test_each_token_walk_prints_its_announce_why_briefing_and_browser_line_and_a_generated_entry_only_its_row(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, verifier: FakeVerifier, capsys
) -> None:
    """#531 picture s-s2g / #36 rules 2 and 5: a token entry announces itself, says why, briefs and names the page; a generated one prints its row only."""
    terminal.pastes = [PASTED_VALUE, PASTED_VALUE]
    assert run_wizard(home, boundaries) is WalkEnd.DONE
    output = capsys.readouterr().out
    assert verifier.checked == [("example.invalid", len(PASTED_VALUE)), ("example-org", len(PASTED_VALUE))]
    assert files_under(home) == set(EVERY_FILE)
    assert squeezed(output) == [
        "› cloudflare/read new token Cloudflare fills in nothing",
        "why: liest Cloudflare",
        "[ ] Create Token › Custom token › Get started",
        "[ ] Token name: cloudflare/read",
        "[ ] Permissions: Zone › DNS › Read",
        "[ ] Account Resources: Include › your account",
        "[ ] Zone Resources: Include › Specific zone › example.invalid",
        "[ ] TTL: leave the start date empty, set only the End Date 16.11.2026",
        "[ ] Continue to summary › Create Token, copy the value",
        "opened dash.cloudflare.com/profile/api-tokens",
        "✓ cloudflare/read verified ~/.secrets/cloudflare/read (600)",
        "› github/read new token the link fills name, owner, expiry and rights",
        "why: liest GitHub",
        "prefilled: Metadata read, Organization secrets read",
        "[ ] Repository access: All repositories",
        "[ ] compare the rights in the form with the line above",
        "[ ] Generate token, copy the value",
        "opened github.com/settings/personal-access-tokens/new?…",
        "✓ github/read verified ~/.secrets/github/read (600)",
        "✓ opentofu/passphrase generated ~/.secrets/opentofu/passphrase (600) · never shown",
    ]
    assert terminal.prompts == [OPEN_PROMPT, PASTE_PROMPT, OPEN_PROMPT, PASTE_PROMPT]
    under_a_row = [line for line in output.splitlines() if line.lstrip().startswith(("why:", "[ ]", "prefilled:", "opened "))]
    assert under_a_row
    assert all(line.startswith("    ") and not line.startswith("     ") for line in under_a_row)


def test_an_unverified_token_without_a_purpose_is_announced_with_no_why_line(
    tmp_path: Path, use_manifest, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    use_manifest('[[secret]]\nname = "turn"\nkind = "unverified-token"\npaths = ["~/.secrets/turn"]\nread_by = []\nrecreate = "https://x.example"\n')
    terminal.pastes = [PASTED_VALUE]
    assert run_wizard(tmp_path, boundaries) is WalkEnd.DONE
    output = capsys.readouterr().out
    assert squeezed(output)[0].startswith("› turn new token")
    assert "why:" not in output


def test_a_page_no_browser_opens_is_named_for_opening_by_hand_and_the_walk_goes_on(
    home: Path, terminal: FakeTerminal, boundaries: Boundaries, capsys
) -> None:
    """#36 rule 2.2: a page no browser opens is named for opening by hand, never reported as opened."""
    terminal.browser_opens, terminal.pastes = False, [PASTED_VALUE, PASTED_VALUE]
    assert run_wizard(home, boundaries) is WalkEnd.DONE
    lines = capsys.readouterr().out.splitlines()
    assert files_under(home) == set(EVERY_FILE)
    assert f"    {NO_BROWSER} {CLOUDFLARE_TOKEN_PAGE}" in lines
    assert not any(line.lstrip().startswith("opened ") for line in lines)


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
        run_wizard(home, boundaries)
        passphrases.append((home / PASSPHRASE_FILE).read_text())

    assert passphrases[0] != passphrases[1]
    assert all(os.path.getsize(tmp_path / name / PASSPHRASE_FILE) >= 64 for name in ("first", "second"))


def test_the_real_terminal_fact_is_false_while_stdio_is_captured(capsys: pytest.CaptureFixture[str]) -> None:
    """A terminal needs both ends: under captured stdio the process the agents run is no terminal, which is what refuses the yes."""
    assert secrets_setup.TERMINAL_BOUNDARIES.terminal.is_interactive() is False


CODEX_CREATE_LINE = 'create = { run = ["codex", "login"], home = ["CODEX_HOME"], label = "Codex" }'
CODEX_LOGIN_MANIFEST = f"""\
[[secret]]
name = "agent-service-codex"
kind = "tool-login"
paths = ["~/.secrets/agent-service/codex/auth.json"]
read_by = ["agent-service"]
{CODEX_CREATE_LINE}
purpose = "the agent service runs Codex for the apps"
"""
CODEX_FOLDER = ".secrets/agent-service/codex"
CODEX_FILE = f"{CODEX_FOLDER}/auth.json"


@pytest.fixture
def login_home(home: Path, use_manifest) -> Path:
    use_manifest(CODEX_LOGIN_MANIFEST)
    return home


def test_a_missing_login_is_made_by_its_cli_in_the_entrys_own_folder_with_the_ruled_rights(
    login_home: Path, boundaries: Boundaries, cli: FakeCli, capsys
) -> None:
    """#666 D1, D3: the CLI runs with its home variable at the entry's folder; afterwards folder 0700 and file 0600, and the row never shows content."""
    end = run_wizard(login_home, boundaries)

    captured = capsys.readouterr()
    assert end is WalkEnd.DONE
    assert cli.calls == [(("codex", "login"), {"CODEX_HOME": str(login_home / CODEX_FOLDER)})]
    assert stat.S_IMODE((login_home / CODEX_FOLDER).stat().st_mode) == 0o700
    assert stat.S_IMODE((login_home / CODEX_FILE).stat().st_mode) == 0o600
    assert squeezed(captured.out) == [
        "› agent-service/codex new login your Codex account, a login of its own",
        "why: the agent service runs Codex for the apps",
        "✓ agent-service/codex signed in ~/.secrets/agent-service/codex/auth.json (600) · never shown",
    ]
    assert cli.content not in captured.out + captured.err


def test_the_walk_offers_the_sign_in_before_it_runs_the_cli(
    login_home: Path, boundaries: Boundaries, terminal: FakeTerminal, cli: FakeCli
) -> None:
    """#666 rows: Enter opens the Codex sign-in; nothing runs before the operator pressed it."""
    terminal.enter = KeyboardInterrupt

    end = run_wizard(login_home, boundaries)

    assert end is WalkEnd.ABORTED
    assert terminal.prompts == [
        f"{secrets_setup.DETAIL_INDENT}Enter opens the Codex sign-in in your browser: "
    ]
    assert cli.calls == []
    assert not list((login_home / CODEX_FOLDER).glob("*"))


def test_a_login_already_present_is_left_untouched_and_the_cli_does_not_run(
    login_home: Path, boundaries: Boundaries, cli: FakeCli
) -> None:
    """#666 D2: a present auth.json stays as it is, whatever its mode."""
    folder = private_directory(login_home, CODEX_FOLDER)
    (folder / "auth.json").write_text("kept")
    (folder / "auth.json").chmod(0o640)

    end = run_wizard(login_home, boundaries)

    assert end is WalkEnd.DONE
    assert cli.calls == []
    assert (
        (folder / "auth.json").read_text(),
        stat.S_IMODE((folder / "auth.json").stat().st_mode),
    ) == ("kept", 0o640)


@pytest.mark.parametrize(
    ("cli_answer", "refusal"),
    [
        (FakeCli(exit_code=None), "codex is not installed · install it, then rerun"),
        (FakeCli(exit_code=1), "codex login ended with exit 1 · nothing stored"),
        (
            FakeCli(exit_code=0, leaves=""),
            "codex login ended without a login file · nothing stored",
        ),
        (
            FakeCli(exit_code=0, leaves="auth.json.tmp"),
            "codex login ended without a login file · nothing stored",
        ),
    ],
    ids=["not installed", "exit 1", "no file left", "only a sibling left"],
)
def test_a_login_that_fails_is_refused_with_a_row_and_leaves_the_walk_open(
    login_home: Path,
    terminal: FakeTerminal,
    verifier: FakeVerifier,
    disk: FakeSync,
    capsys,
    cli_answer: FakeCli,
    refusal: str,
) -> None:
    """#666 D2, E1: a refused sign-in names itself in one row and stores no auth.json, even when the CLI wrote one or left only a sibling."""
    end = run_wizard(login_home, Boundaries(terminal.boundary(), verifier, disk.sync, cli_answer))

    assert end is WalkEnd.STILL_OPEN
    assert f"✗ agent-service/codex not signed in {refusal}" in squeezed(capsys.readouterr().out)
    assert not (login_home / CODEX_FILE).exists()


def test_the_renewal_command_is_derived_from_the_create_key(login_home: Path) -> None:
    """#666 D4: the command `hal secrets` shows is derived, so the entry carries no hand-written recreate."""
    (entry,) = load_manifest(secrets_view.MANIFEST_PATH)

    assert entry.renewal == "CODEX_HOME=~/.secrets/agent-service/codex codex login"


@pytest.mark.parametrize(
    "create_line",
    [
        'create = { run = ["codex"], label = "Codex" }\nrecreate = "by hand"',
        'create = { run = ["codex"], label = "Codex", mode = "x" }',
        'create = { run = [], label = "Codex" }',
    ],
    ids=["recreate beside create", "unknown create key", "empty run"],
)
def test_a_create_entry_is_refused_when_it_is_malformed(use_manifest, create_line: str) -> None:
    """#666 D1/D4: create replaces recreate, takes only run, home and label, and names a CLI."""
    path = use_manifest(CODEX_LOGIN_MANIFEST.replace(CODEX_CREATE_LINE, create_line))

    with pytest.raises(secrets_view.ManifestError):
        load_manifest(path)


def test_the_packaged_manifest_lists_the_codex_login_as_a_required_tool_login() -> None:
    """#666 D2: the entry is not optional, so a machine without the login is walked."""
    entry = next(
        entry for entry in load_manifest(PACKAGED_MANIFEST) if entry.name == "agent-service-codex"
    )

    assert (entry.kind, entry.optional, entry.create.run, entry.create.home) == (
        SecretKind.TOOL_LOGIN,
        False,
        ("codex", "login"),
        ("CODEX_HOME",),
    )

