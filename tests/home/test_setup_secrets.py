"""The secrets row of `home setup` and `hal setup`: the rules are those of #22 and #296."""

from __future__ import annotations

import stat
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from conftest import Sandbox
from test_setup import SECRETS, line, run_hal_setup

from hal.grammar import ExitCode
from home_tools import cli as home_cli
from home_tools import secrets_setup
from home_tools.cli import main
from home_tools.secrets_setup import Boundaries, Terminal
from home_tools.setup import RERUN


SECRETS_MANIFEST = """\
[[secret]]
name = "opentofu/passphrase"
kind = "service-password"
paths = ["~/.secrets/opentofu/passphrase"]
read_by = []
recreate = "hal setup --apply"

[[secret]]
name = "hopin/session-secret"
kind = "service-password"
paths = ["~/.secrets/hopin/session-secret"]
read_by = []
recreate = "hal setup --apply"
loosest_mode = 0o644
loosest_mode_reason = "der Container liest es als eigener Nutzer"
"""
SECRET_FILES = (".secrets/opentofu/passphrase", ".secrets/hopin/session-secret")


def secrets_terminal(at_a_terminal: bool) -> Boundaries:
    """A terminal that must never be asked for a value: the manifest above holds only generated passwords."""

    def never_asked(_prompt: str) -> str:
        raise AssertionError("the wizard asked a person for a value it generates itself")

    terminal = Terminal(lambda: at_a_terminal, never_asked, never_asked, lambda _page: False)
    return Boundaries(terminal, lambda _source, _value: pytest.fail("a generated password is never verified"), lambda _descriptor: None)


def run_setup_with_secrets(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str], *flags: str, at_a_terminal: bool = True
) -> tuple[int, list[str], str]:
    use_manifest(SECRETS_MANIFEST)
    code = main(["setup", *flags], home=sandbox.home, secrets_setup_boundaries=secrets_terminal(at_a_terminal), now=datetime(2026, 10, 22, 9, 0))
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def test_the_secrets_step_is_verified_when_the_manifest_has_no_missing_secret(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    run_setup_with_secrets(sandbox, use_manifest, capsys, "--apply")
    code, lines, _ = run_setup_with_secrets(sandbox, use_manifest, capsys)

    assert (code, lines[-2]) == (0, line("·", SECRETS, "complete"))


def test_the_dry_run_names_the_missing_secrets_and_writes_nothing(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = run_setup_with_secrets(sandbox, use_manifest, capsys)

    assert (code, lines[-2]) == (
        0,
        line("!", SECRETS, "missing", "opentofu/passphrase, hopin/session-secret"),
    )
    assert not sandbox.layout.secrets_dir.exists()


def test_apply_walks_the_missing_secrets_through_the_wizard_and_a_loosest_mode_entry_lands_0644_in_a_0700_folder(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = run_setup_with_secrets(sandbox, use_manifest, capsys, "--apply")

    modes = {name: stat.S_IMODE((sandbox.home / name).stat().st_mode) for name in SECRET_FILES}
    folder_modes = {
        name: stat.S_IMODE((sandbox.home / name).parent.stat().st_mode) for name in SECRET_FILES
    }
    assert (code, lines[-2]) == (0, line("✓", SECRETS, "stored"))
    assert modes == {SECRET_FILES[0]: 0o600, SECRET_FILES[1]: 0o644}
    assert folder_modes == {name: 0o700 for name in SECRET_FILES}
    assert "erzeugt und gespeichert: ~/.secrets/hopin/session-secret (644)" in lines


def test_apply_fails_the_secrets_step_when_the_wizard_leaves_a_secret_open(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    by_hand = '[[secret]]\nname = "gitlab"\nkind = "token"\npaths = ["~/.secrets/gitlab"]\nread_by = []\nrecreate = "von Hand"\n'
    use_manifest(by_hand)
    code = main(
        ["setup", "--apply"], home=sandbox.home, secrets_setup_boundaries=secrets_terminal(True)
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out.splitlines()[-1] == line(
        "✗", SECRETS, "failed", "not every secret is set up; the lines above say which one is open"
    )
    assert captured.err.splitlines() == ["error: setup stopped at secrets", f"next: {RERUN}"]


def secret_entry(name: str, *, kind: str = "service-password", extra: str = "") -> str:
    return f'[[secret]]\nname = "{name}"\nkind = "{kind}"\npaths = ["~/.secrets/{name}"]\nread_by = []\nrecreate = "hal setup --apply"\n{extra}\n'


LIVE = secret_entry("live")
OPTIONAL = secret_entry("optional", extra="optional = true")
RETIRED = secret_entry("retired", extra='retired_on = 2026-10-14\nrevoke_url = "https://example.invalid/revoke"')
BY_HAND = secret_entry("by-hand", kind="token")


@pytest.fixture
def hal_secrets(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, list[str], str]]:
    """`hal setup` in process against a manifest written from the given entries; the wizard's terminal is the one `secrets_terminal` builds."""

    def run(entries: str, *flags: str, at_a_terminal: bool = True, boundaries: Boundaries | None = None) -> tuple[int, list[str], str]:
        use_manifest(entries)
        monkeypatch.setattr(home_cli, "TERMINAL_BOUNDARIES", boundaries or secrets_terminal(at_a_terminal))
        return run_hal_setup(capsys, *flags)

    return run


def put_secret(sandbox: Sandbox, name: str, mode: int = 0o600) -> Path:
    path = sandbox.home / ".secrets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a value no one may read")
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    ("entries", "present", "expected"),
    [
        pytest.param(
            LIVE, [], line("!", SECRETS, "missing", "live"), id="a live entry that is absent"
        ),
        pytest.param(
            LIVE, ["live"], line("·", SECRETS, "complete"), id="a live entry with its file"
        ),
        pytest.param(
            OPTIONAL, [], line("·", SECRETS, "complete"), id="an optional entry that is absent"
        ),
        pytest.param(
            RETIRED, [], line("·", SECRETS, "complete"), id="a retired entry that is absent"
        ),
        pytest.param(
            BY_HAND,
            [],
            line("!", SECRETS, "missing", "by-hand (by hand)"),
            id="an entry only a person can create",
        ),
        pytest.param(
            LIVE + OPTIONAL + RETIRED,
            ["live"],
            line("·", SECRETS, "complete"),
            id="every kind, the live one present",
        ),
        pytest.param(
            LIVE + BY_HAND,
            [],
            line("!", SECRETS, "missing", "live, by-hand (by hand)"),
            id="a generated and a by-hand entry",
        ),
    ],
)
def test_hal_setup_dry_run_names_only_the_open_secrets(
    sandbox: Sandbox,
    hal_secrets: Callable[..., tuple[int, list[str], str]],
    entries: str,
    present: list[str],
    expected: str,
) -> None:
    for name in present:
        put_secret(sandbox, name)

    code, lines, _ = hal_secrets(entries)

    assert (code, lines[-2]) == (ExitCode.OK, expected)


@pytest.fixture
def closed_folder(sandbox: Sandbox) -> Iterator[None]:
    """A folder its owner cannot open, so `stat` cannot tell whether the file inside exists."""
    put_secret(sandbox, "closed/entry")
    folder = sandbox.home / ".secrets" / "closed"
    folder.chmod(0)
    yield
    folder.chmod(0o700)


@pytest.mark.usefixtures("closed_folder")
@pytest.mark.parametrize("entries", [secret_entry("closed/entry"), secret_entry("closed/entry", extra="optional = true")], ids=["live", "optional"])
def test_hal_setup_dry_run_names_an_entry_whose_presence_cannot_be_told_and_does_not_call_it_verified(
    hal_secrets: Callable[..., tuple[int, list[str], str]], entries: str
) -> None:
    code, lines, _ = hal_secrets(entries)

    assert (code, lines[-2]) == (
        ExitCode.OK,
        line("!", SECRETS, "missing", "closed/entry (unknown)"),
    )


@pytest.mark.usefixtures("closed_folder")
def test_hal_setup_apply_fails_the_secrets_step_when_an_entry_cannot_be_checked(hal_secrets: Callable[..., tuple[int, list[str], str]]) -> None:
    code, lines, error = hal_secrets(secret_entry("closed/entry"), "--apply")

    assert (
        code,
        "closed/entry: nicht feststellbar, ob vorhanden (Ordner nicht lesbar); übersprungen"
        in lines,
    ) == (ExitCode.FAILURE, True)
    assert error.splitlines()[0] == "error: setup stopped at secrets"


def test_hal_setup_dry_run_reads_no_secret_and_changes_no_mode(sandbox: Sandbox, hal_secrets: Callable[..., tuple[int, list[str], str]]) -> None:
    unreadable = put_secret(sandbox, "live", mode=0)  # a read would fail with PermissionError, so the run proves it opened nothing
    modes_before = {path: stat.S_IMODE(path.stat().st_mode) for path in (unreadable, unreadable.parent)}

    code, lines, _ = hal_secrets(LIVE + secret_entry("absent"))

    assert (code, lines[-2]) == (ExitCode.OK, line("!", SECRETS, "missing", "absent"))
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes_before} == modes_before
    assert not (unreadable.parent / "absent").exists()
    unreadable.chmod(0o600)


def test_hal_setup_apply_sets_up_the_missing_secrets_and_a_repeated_apply_changes_nothing(
    sandbox: Sandbox, hal_secrets: Callable[..., tuple[int, list[str], str]]
) -> None:
    entries = LIVE + secret_entry("wide", extra="loosest_mode = 0o644\nloosest_mode_reason = \"the container reads it\"")
    first_code, first_lines, _ = hal_secrets(entries, "--apply")
    stored = [sandbox.home / ".secrets" / name for name in ("live", "wide")]
    after_first = [(path.stat().st_ino, path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode), path.read_bytes()) for path in stored]

    second_code, second_lines, _ = hal_secrets(entries, "--apply")

    assert (first_code, first_lines[-2]) == (ExitCode.OK, line("✓", SECRETS, "stored"))
    assert [mode for _, _, mode, _ in after_first] == [0o600, 0o644]
    assert (second_code, second_lines[-2]) == (ExitCode.OK, line("·", SECRETS, "complete"))
    assert [
        (
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            stat.S_IMODE(path.stat().st_mode),
            path.read_bytes(),
        )
        for path in stored
    ] == after_first


def test_hal_setup_apply_ended_by_ctrl_c_exits_130_and_leaves_no_file(
    sandbox: Sandbox, hal_secrets: Callable[..., tuple[int, list[str], str]]
) -> None:
    def interrupted(_descriptor: int) -> None:
        raise KeyboardInterrupt

    terminal = secrets_terminal(True)
    code, _, error = hal_secrets(LIVE, "--apply", boundaries=Boundaries(terminal.terminal, terminal.verify, interrupted))

    assert code == ExitCode.INTERRUPTED
    assert "abgebrochen; keine Datei halb geschrieben. Weiter mit: hal setup --apply" in error
    assert "setup stopped" not in error
    assert not (sandbox.home / ".secrets" / "live").exists()


def test_home_setup_apply_ended_by_ctrl_c_exits_130_with_the_abort_line_and_no_traceback(
    sandbox: Sandbox, use_manifest: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_descriptor: int) -> None:
        raise KeyboardInterrupt

    use_manifest(LIVE)
    terminal = secrets_terminal(True)
    code = main(["setup", "--apply"], home=sandbox.home, secrets_setup_boundaries=Boundaries(terminal.terminal, terminal.verify, interrupted))
    error = capsys.readouterr().err

    assert code == secrets_setup.ABORTED_EXIT
    assert "abgebrochen; keine Datei halb geschrieben" in error
    assert "Traceback" not in error


def test_hal_setup_fails_the_secrets_step_by_name_when_the_manifest_cannot_be_read(
    hal_secrets: Callable[..., tuple[int, list[str], str]],
) -> None:
    code, lines, error = hal_secrets("secret = 5\n")

    assert code == ExitCode.FAILURE
    assert lines[-1].startswith(line("✗", SECRETS, "failed"))
    assert error.splitlines()[0] == "error: setup stopped at secrets"
    assert "unexpected error" not in error
