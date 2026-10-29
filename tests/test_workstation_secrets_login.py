"""The guided tool login of #666: the wizard has the CLI of a `create` entry make the secret and judges only its path and mode."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from workstation_secrets_support import (
    FakeCli,
    FakeSync,
    FakeTerminal,
    private_directory,
    run_wizard,
    squeezed,
)
from workstation_support import FakeProbe, run_hal_secrets

from hal.workstation import secrets_setup, secrets_view
from hal.workstation.secrets_setup import Boundaries, Outcome, Verdict, WalkEnd
from hal.workstation.secrets_view import MANIFEST_PATH as PACKAGED_MANIFEST
from hal.workstation.secrets_view import SecretKind, load_manifest

CODEX_CREATE_LINE = 'create = { run = ["codex", "login"], home = ["CODEX_HOME"], label = "Codex" }'
CODEX_LOGIN_MANIFEST = f"""\
[[secret]]
name = "agent-service/codex"
kind = "tool-login"
paths = ["~/.secrets/agent-service/codex/auth.json"]
read_by = ["agent-service"]
{CODEX_CREATE_LINE}
purpose = "the agent service runs Codex for the apps"
"""
CODEX_FOLDER = ".secrets/agent-service/codex"
CODEX_FILE = f"{CODEX_FOLDER}/auth.json"


@pytest.fixture
def terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
def disk() -> FakeSync:
    return FakeSync()


@pytest.fixture
def cli() -> FakeCli:
    return FakeCli()


def refusing_verifier(_source: object, _value: str) -> Verdict:
    return Verdict(Outcome.REFUSED, "no token is verified in a tool login")


@pytest.fixture
def boundaries(terminal: FakeTerminal, disk: FakeSync, cli: FakeCli) -> Boundaries:
    """The operator at the keyboard; the walk sees only these fakes, never a browser, a prompt or a CLI."""
    return Boundaries(terminal.boundary(), refusing_verifier, disk.sync, cli)


@pytest.fixture
def login_home(tmp_path: Path, use_manifest) -> Path:
    use_manifest(CODEX_LOGIN_MANIFEST)
    home = tmp_path / "home"
    home.mkdir()
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
    disk: FakeSync,
    capsys,
    cli_answer: FakeCli,
    refusal: str,
) -> None:
    """#666 D2, E1: a refused sign-in names itself in one row and stores no auth.json, even when the CLI wrote one or left only a sibling."""
    end = run_wizard(
        login_home, Boundaries(terminal.boundary(), refusing_verifier, disk.sync, cli_answer)
    )

    assert end is WalkEnd.STILL_OPEN
    assert f"✗ agent-service/codex not signed in {refusal}" in squeezed(capsys.readouterr().out)
    assert not (login_home / CODEX_FILE).exists()


SIBLINGS_OF_CODEX_LOGIN = ("log/codex-login.log", "tmp/arg0-linux/lock")


def test_what_the_cli_leaves_beside_the_login_file_is_no_unknown_file_for_hal_secrets(
    login_home: Path, terminal: FakeTerminal, disk: FakeSync, capsys
) -> None:
    """#666 D3: the CLI's folder is part of the entry; its logs and temporary folders are neither unknown nor loose, only the login file is judged."""
    cli_with_siblings = FakeCli(siblings=SIBLINGS_OF_CODEX_LOGIN)
    assert (
        run_wizard(
            login_home,
            Boundaries(terminal.boundary(), refusing_verifier, disk.sync, cli_with_siblings),
        )
        is WalkEnd.DONE
    )
    capsys.readouterr()

    code = run_hal_secrets(login_home, FakeProbe())

    output = capsys.readouterr().out
    assert (code, "unknown-file" in output, "loose-mode" in output) == (0, False, False)


def test_a_failed_login_removes_what_that_run_left_and_keeps_what_was_there_before(
    login_home: Path, terminal: FakeTerminal, disk: FakeSync
) -> None:
    """#666 D3: after a refused run only its own leftovers go; a sibling that existed before the run survives."""
    folder = private_directory(login_home, CODEX_FOLDER)
    (folder / "notes.txt").write_text("mine")
    failing = FakeCli(exit_code=1, siblings=SIBLINGS_OF_CODEX_LOGIN)

    end = run_wizard(
        login_home, Boundaries(terminal.boundary(), refusing_verifier, disk.sync, failing)
    )

    assert end is WalkEnd.STILL_OPEN
    assert sorted(path.name for path in folder.rglob("*")) == ["notes.txt"]


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
        entry for entry in load_manifest(PACKAGED_MANIFEST) if entry.name == "agent-service/codex"
    )

    assert (entry.kind, entry.optional, entry.create.run, entry.create.home) == (
        SecretKind.TOOL_LOGIN,
        False,
        ("codex", "login"),
        ("CODEX_HOME",),
    )
