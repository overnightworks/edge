"""The pure checks of the login command (#457): the refusals before any docker call, the
preflight of the resolved configuration, the exact-secret gate and the S00 census counts."""

from __future__ import annotations

import copy
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_support import CONFIGURATION, LOGIN, TOKEN, login_probe_modules, make_login, resolved

STARTED = ("agent-egress", "agent-issuer", "agent-service")


@pytest.fixture(scope="module")
def probe() -> Iterator[SimpleNamespace]:
    with login_probe_modules() as modules:
        yield modules


# --- the refusals before any docker call -------------------------------------------------------


def refusal(probe, text: str) -> str:
    with pytest.raises(probe.checks.LoginFailed) as raised:
        probe.checks.read_login(text)
    return str(raised.value)


def test_a_login_is_read_with_its_token_stripped(probe, tmp_path):
    """#457 row 4: the token file ends in a newline and the login holds it stripped."""
    login = probe.checks.read_login(str(make_login(tmp_path / "login")))
    assert login.token == TOKEN
    assert TOKEN not in repr(login)


def test_a_login_behind_a_symlinked_ancestor_is_resolved_and_accepted(probe, tmp_path):
    """#457 row 2: a path reached through a symlink resolves and is accepted."""
    real = tmp_path / "real"
    real.mkdir()
    make_login(real / "login")
    (tmp_path / "link").symlink_to(real)
    login = probe.checks.read_login(str(tmp_path / "link" / "login"))
    assert login.directory == (real / "login").resolve()


def test_a_missing_directory_and_a_file_are_refused(probe, tmp_path):
    """#457 row 2: no directory is refused naming the flag only."""
    (tmp_path / "file").write_text("x", encoding="utf-8")
    for text in (str(tmp_path / "missing"), str(tmp_path / "file")):
        assert refusal(probe, text) == probe.checks.Refusal.NOT_A_DIRECTORY


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (lambda login: None, "NO_TOKEN_FILE"),
        (lambda login: (login / "token").mkdir(), "NO_TOKEN_FILE"),
        (lambda login: (login / "token").symlink_to(login / "elsewhere"), "NO_TOKEN_FILE"),
        (lambda login: (login / "token").write_bytes(b""), "TOKEN_EMPTY"),
        (lambda login: (login / "token").write_bytes(b" \n\t\n"), "TOKEN_EMPTY"),
        (lambda login: (login / "token").write_bytes(b"x" * (16 * 1024 + 1)), "TOKEN_TOO_LARGE"),
        (lambda login: (login / "token").write_bytes(b"\xff\xfe"), "TOKEN_UNREADABLE"),
    ],
    ids=[
        "no token",
        "token a directory",
        "token a symlink",
        "token empty",
        "token whitespace",
        "token too large",
        "token not text",
    ],
)
def test_a_login_without_a_usable_token_is_refused_without_its_path(
    probe, tmp_path, arrange, reason
):
    """#457 row 2: an unusable token is refused without a path."""
    login = tmp_path / "login"
    login.mkdir()
    arrange(login)
    message = refusal(probe, str(login))
    assert message == probe.checks.Refusal[reason]
    assert str(tmp_path) not in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file")
def test_a_token_that_cannot_be_opened_is_refused(probe, tmp_path):
    """#457 row 2: an unreadable token is refused."""
    login = make_login(tmp_path / "login")
    (login / "token").chmod(0)
    assert refusal(probe, str(login)) == probe.checks.Refusal.TOKEN_UNREADABLE


# --- the preflight -----------------------------------------------------------------------------


def preflight(probe, config: dict) -> None:
    probe.checks.check_resolved(
        config,
        service="agent-service",
        started=STARTED,
        login=(LOGIN, Path("/login")),
        configuration=(CONFIGURATION, Path("/scratch/service.toml")),
    )


def volumes(config: dict) -> list[dict]:
    return config["services"]["agent-service"]["volumes"]


def writable(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config)[index].pop("read_only")


def another_source(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config)[index].update(source="/elsewhere")


def second_bind(index: int) -> Callable[[dict], None]:
    return lambda config: volumes(config).append(copy.deepcopy(volumes(config)[index]))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (writable(0), "LOGIN_BIND"),
        (another_source(0), "LOGIN_BIND"),
        (second_bind(0), "LOGIN_BIND"),
        (lambda config: volumes(config).pop(0), "LOGIN_BIND"),
        (writable(1), "CONFIGURATION_BIND"),
        (another_source(1), "CONFIGURATION_BIND"),
        (second_bind(1), "CONFIGURATION_BIND"),
        (
            lambda config: config["services"]["agent-service"].pop("read_only"),
            "WRITABLE_ROOT",
        ),
        (
            lambda config: config["services"]["agent-issuer"].update(
                logging={"driver": "journald"}
            ),
            "LOG_DRIVER",
        ),
        (lambda config: config["services"]["agent-egress"].pop("logging"), "LOG_DRIVER"),
    ],
    ids=[
        "login bind writable",
        "login bind another source",
        "second login bind",
        "no login bind",
        "config bind writable",
        "config bind another source",
        "second config bind",
        "root writable",
        "another log driver",
        "no log driver",
    ],
)
def test_a_resolved_configuration_that_binds_anything_else_is_refused(probe, mutate, reason):
    """#457 row 3 and 16: a resolved configuration with another bind is refused before up."""
    config = resolved()
    mutate(config)
    with pytest.raises(probe.checks.LoginFailed) as raised:
        preflight(probe, config)
    assert str(raised.value) == probe.checks.Refusal[reason]


def test_the_expected_resolved_configuration_passes(probe):
    """#457 row 3 and 16: the expected resolved configuration passes."""
    preflight(probe, resolved())


# --- the gate ----------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("secret", [TOKEN, "census-key-hex", "minted-token"])
def test_a_line_with_a_known_secret_is_replaced_and_counted(probe, capsys, stream, secret):
    """#457 row 4: a printed line holding a known secret is replaced and counted."""
    with probe.checks.gating([TOKEN, "census-key-hex", "minted-token"]) as gate:
        print(f"before {secret} after", file=getattr(sys, stream), flush=True)
    shown = getattr(capsys.readouterr(), "out" if stream == "stdout" else "err")
    assert shown == "before [secret] after\n"
    assert gate.hits == 1


def test_a_last_write_without_a_newline_is_checked_at_exit(probe, capsys):
    """#457 row 4: the unterminated last write is checked at exit."""
    with probe.checks.gating([TOKEN]) as gate:
        sys.stdout.write(f"tail {TOKEN}")
    assert capsys.readouterr().out == "tail [secret]"
    assert gate.hits == 1


def test_a_secret_split_over_two_writes_of_one_line_is_replaced(probe, capsys):
    """#457 row 4: a secret split over two writes of one line is replaced."""
    with probe.checks.gating([TOKEN]):
        sys.stdout.write(TOKEN[:5])
        sys.stdout.write(TOKEN[5:] + "\n")
    assert capsys.readouterr().out == "[secret]\n"


def test_a_secret_added_later_is_gated_and_a_clean_line_passes(probe, capsys):
    """#457 row 4: a secret added later is gated."""
    with probe.checks.gating([TOKEN]) as gate:
        gate.add("late-key")
        gate.add("")
        print("late-key and a clean line", flush=True)
        print("clean", flush=True)
    assert capsys.readouterr().out == "[secret] and a clean line\nclean\n"
    assert gate.hits == 1


def test_the_streams_are_restored_after_the_gate(probe):
    """#457 row 4: the gate restores the streams."""
    before = sys.stdout, sys.stderr
    with probe.checks.gating([TOKEN]):
        assert sys.stdout is not before[0]
        assert sys.stdout.isatty() is False
    assert (sys.stdout, sys.stderr) == before


# --- S00 ---------------------------------------------------------------------------------------


def mount(probe, *, entries=0, skips=(), token_files=0):
    listings = tuple((f"n-{index}", "file") for index in range(entries))
    return probe.census.Mount(listings, tuple(skips), token_files)


@pytest.mark.parametrize(
    ("entries", "skips", "line"),
    [
        (3, (), "S00 role: listed: 3 entries; skips: none; 0 files hold the token"),
        (0, (), "S00 role: listed: 0 entries; skips: none; 0 files hold the token"),
        (
            2,
            (("refused", 1),),
            "S00 role: listed: 2 entries; skips: refused 1; 0 files hold the token",
        ),
        (
            0,
            (("replaced", 1),),
            "S00 role: not listed (replaced); skips: replaced 1; 0 files hold the token",
        ),
        (
            0,
            (("a reason nobody listed", 1),),
            "S00 role: not listed (other); skips: other 1; 0 files hold the token",
        ),
    ],
    ids=["entries", "empty", "entries and a skip", "root not listed", "unknown reason"],
)
def test_a_mount_line_says_listed_or_not_listed_with_its_reason(probe, entries, skips, line):
    """#457 row 8: a mount line says listed or not listed with its reason."""
    assert probe.checks.mount_line("role", mount(probe, entries=entries, skips=skips)) == line


@pytest.mark.parametrize(
    ("skips", "unlisted"),
    [
        ((("replaced", 1),), 1),
        ((("refused", 1),), 1),
        ((("login excluded", 1),), 1),
        ((("file mount", 1),), 0),
        ((), 0),
    ],
    ids=["replaced", "refused", "login excluded", "file mount", "listed empty"],
)
def test_a_directory_root_that_was_not_listed_is_counted_and_a_file_mount_is_not(
    probe, skips, unlisted
):
    """#457 row 9: an unlisted directory root is counted, a file mount is not."""
    mounts = {"/tmp": mount(probe, entries=1), "n-0": mount(probe, skips=skips)}
    assert probe.checks.unlisted_roots(mounts) == unlisted


def test_token_files_are_summed_over_every_mount(probe):
    """#457 row 10: token files are summed over every mount."""
    mounts = {"/tmp": mount(probe, token_files=1), "n-0": mount(probe, token_files=2)}
    assert probe.checks.token_files(mounts) == 3
