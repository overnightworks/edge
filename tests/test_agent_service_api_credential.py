"""The API credential of agent-service/setup: made, checked and replaced in a throwaway home. The
verdict on a credential's content is always the reader of agent-service/litellm/gate.py, the one
hal-side copy of agent-runtime's rule."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import support.agent_service_setup as base
from support.agent_service_setup import (
    DUMMY_TOKEN,
    api_directory,
    credential_path,
    mode_of,
    write_token,
)
from support.script_module import loaded_script

setup_module, docker, terminal, home, make_setup, openssl = (
    base.setup_module, base.docker, base.terminal, base.home, base.make_setup, base.openssl,
)  # fmt: skip

GATE = base.SETUP.parent / "litellm" / "gate.py"
REPLACE = "agent-service/setup --replace-api-credential"


@pytest.fixture(scope="module")
def gate() -> Iterator[ModuleType]:
    yield from loaded_script("api_credential_test_gate", GATE)


def accepted_key(gate, home: Path) -> str | None:
    return gate.shared_key_of(credential_path(home).read_bytes())


def bytes_of_folder(home: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(api_directory(home).iterdir())}


@pytest.fixture
def host_without_credential(home) -> Path:
    write_token(home, DUMMY_TOKEN)
    credential_path(home).unlink()
    return home


def test_a_plain_run_makes_the_credential_that_the_gate_reader_accepts_at_0600(
    make_setup, host_without_credential, terminal, gate
):
    home = host_without_credential

    assert make_setup().run(renew=False) == 0

    key = accepted_key(gate, home)
    assert key is not None
    assert mode_of(credential_path(home)) == 0o600
    assert set(json.loads(credential_path(home).read_bytes())) == {"shared_key"}
    assert key not in "\n".join(terminal.said)
    assert list(api_directory(home).iterdir()) == [credential_path(home)]


def test_a_second_plain_run_leaves_the_credential_as_it_is(
    make_setup, host_without_credential, terminal
):
    home = host_without_credential
    assert make_setup().run(renew=False) == 0
    before = bytes_of_folder(home)

    assert make_setup().run(renew=False) == 0
    assert bytes_of_folder(home) == before


def test_check_names_the_missing_credential_in_one_line_and_creates_nothing(
    make_setup, host_without_credential, terminal
):
    home = host_without_credential

    assert make_setup().check() == 1

    assert terminal.said == [f"{credential_path(home)} does not exist"]
    assert not credential_path(home).exists()


def test_check_passes_on_a_credential_that_a_plain_run_made(
    make_setup, host_without_credential, terminal
):
    assert make_setup().run(renew=False) == 0

    assert make_setup().check() == 0
    assert terminal.said[-1] == "agent-service/setup --check: set up"


REFUSED_CONTENTS = {
    "not json": b"not json",
    "an unknown field": b'{"shared_key": "abc", "other": "x"}',
    "a duplicate field": b'{"shared_key": "abc", "shared_key": "abd"}',
    "app_keys not an object": b'{"shared_key": "abc", "app_keys": ["abc"]}',
    "a value outside the syntax": b'{"shared_key": "a b"}',
    "not utf-8": b'{"shared_key": "\xff"}',
}


@pytest.mark.parametrize("contents", REFUSED_CONTENTS.values(), ids=REFUSED_CONTENTS.keys())
def test_a_credential_the_gate_reader_refuses_is_refused_by_check_and_run_and_kept(
    make_setup, home, terminal, gate, contents
):
    write_token(home, DUMMY_TOKEN)
    credential_path(home).write_bytes(contents)
    assert gate.shared_key_of(contents) is None

    for outcome in (make_setup().check(), make_setup().run(renew=False)):
        assert outcome == 1
        assert str(credential_path(home)) in terminal.said[-1]
        assert REPLACE in terminal.said[-1]
        assert credential_path(home).read_bytes() == contents


def test_a_refused_credential_stops_the_run_before_the_token_is_touched(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    base.token_path(home).unlink()
    credential_path(home).write_bytes(b"not json")

    assert make_setup().run(renew=False) == 1
    assert not base.token_path(home).exists()
    assert terminal.prompts == []


def test_a_link_at_the_credential_asks_for_the_operators_fix_and_leaves_its_target(
    make_setup, home, terminal
):
    write_token(home, DUMMY_TOKEN)
    target = home / "elsewhere.json"
    target.write_bytes(credential_path(home).read_bytes())
    credential_path(home).unlink()
    credential_path(home).symlink_to(target)
    before = target.read_bytes()

    assert make_setup().check() == 1
    assert make_setup().run(renew=False) == 1
    assert "symbolic link" in terminal.said[-1]
    assert "fix it yourself" in terminal.said[-1]
    assert credential_path(home).is_symlink()
    assert target.read_bytes() == before


def test_a_credential_with_mode_0640_asks_for_the_operators_fix_and_keeps_its_mode(
    make_setup, home, terminal
):
    write_token(home, DUMMY_TOKEN)
    credential_path(home).chmod(0o640)

    assert make_setup().check() == 1
    assert make_setup().run(renew=False) == 1
    assert "not 0600" in terminal.said[-1]
    assert mode_of(credential_path(home)) == 0o640


def test_a_credential_owned_by_another_user_asks_for_the_operators_fix(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    setup = make_setup(service_uid=os.getuid() + 1)
    setup.judge_directory = lambda *_: None  # the folders are mine; only the file's owner differs

    assert setup.attempt(setup.api_credential.check, read_only=True) == 1
    assert "owned by uid" in terminal.said[-1]


def test_a_leftover_candidate_beside_an_accepted_credential_is_cleared_and_next_is_kept(
    make_setup, home, terminal
):
    write_token(home, DUMMY_TOKEN)
    leftover = api_directory(home) / "credential.json.new"
    kept = api_directory(home) / "credential.next.json"
    leftover.write_text("partial")
    kept.write_text("kept")
    before = credential_path(home).read_bytes()

    assert make_setup().check() == 1
    assert "credential.json.new" in terminal.said[-1]
    assert make_setup().run(renew=False) == 0

    assert not leftover.exists()
    assert kept.read_text() == "kept"
    assert credential_path(home).read_bytes() == before
    assert make_setup().check() == 0


def test_a_leftover_candidate_does_not_stop_the_credential_from_being_made(
    make_setup, host_without_credential, terminal, gate
):
    home = host_without_credential
    (api_directory(home) / "credential.json.new").write_text("partial")

    assert make_setup().run(renew=False) == 0
    assert accepted_key(gate, home) is not None
    assert not (api_directory(home) / "credential.json.new").exists()


def test_a_missing_api_folder_is_made_by_a_plain_run_at_0700(make_setup, home, terminal, gate):
    write_token(home, DUMMY_TOKEN)
    credential_path(home).unlink()
    api_directory(home).rmdir()

    assert make_setup().check() == 1
    assert make_setup().run(renew=False) == 0
    assert mode_of(api_directory(home)) == 0o700
    assert accepted_key(gate, home) is not None


def test_replacement_without_a_terminal_changes_nothing(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    terminal.interactive = False
    before = credential_path(home).read_bytes()

    assert make_setup().replace_api_credential() == 1
    assert "needs a terminal; nothing was changed" in terminal.said[-1]
    assert credential_path(home).read_bytes() == before


def test_replacement_without_a_typed_yes_changes_nothing(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    terminal.answers = ["no"]
    before = credential_path(home).read_bytes()

    assert make_setup().replace_api_credential() == 1
    assert "not confirmed; nothing was changed" in terminal.said[-1]
    assert credential_path(home).read_bytes() == before


def test_replacement_with_yes_swaps_the_key_and_names_the_recreates(
    make_setup, home, terminal, gate
):
    write_token(home, DUMMY_TOKEN)
    old_key = accepted_key(gate, home)
    (api_directory(home) / "credential.next.json").write_text("stale")

    assert make_setup().replace_api_credential() == 0

    new_key = accepted_key(gate, home)
    assert new_key is not None and new_key != old_key
    assert mode_of(credential_path(home)) == 0o600
    assert [path.name for path in api_directory(home).iterdir()] == ["credential.json"]
    said = "\n".join(terminal.said)
    assert "recreate litellm" in terminal.said[-1]
    assert old_key not in said and new_key not in said


def test_replacement_replaces_a_refused_credential(make_setup, home, terminal, gate):
    write_token(home, DUMMY_TOKEN)
    credential_path(home).write_bytes(b"not json")

    assert make_setup().replace_api_credential() == 0
    assert accepted_key(gate, home) is not None


def test_a_failure_before_the_rename_leaves_the_old_credential(
    make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN)
    before = credential_path(home).read_bytes()

    def refuse(*_arguments, **_keywords):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)

    assert make_setup().replace_api_credential() == 1
    assert credential_path(home).read_bytes() == before


def test_replacement_before_setup_has_made_the_folder_asks_for_setup(make_setup, home, terminal):
    base.make_login_directory(home)

    assert make_setup().replace_api_credential() == 1
    assert "run agent-service/setup first" in terminal.said[-1]


def test_replacement_refuses_a_link_at_the_credential(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    target = home / "elsewhere.json"
    target.write_bytes(credential_path(home).read_bytes())
    credential_path(home).unlink()
    credential_path(home).symlink_to(target)

    assert make_setup().replace_api_credential() == 1
    assert credential_path(home).is_symlink()
