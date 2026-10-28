"""agent-service/setup against a scripted Docker and a throwaway home: no containers, no real token.

The token rules are the service's reader's (agent_runtime.secret_file); the
refused cases below are the ones its docstring names.
"""

import errno
import fcntl
import functools
import getpass
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

SETUP = Path(__file__).resolve().parents[1] / "agent-service" / "setup"
sys.path.insert(0, str(SETUP.parent))  # setup imports its sibling module as a script does
IMAGE = "edge-agent-service:0.0.0-test"
DUMMY_TOKEN = "dummy-setup-token-0123456789"
SECOND_TOKEN = "second-dummy-token-9876543210"


@pytest.fixture(scope="module")
def setup_module() -> Iterator[ModuleType]:
    loader = importlib.machinery.SourceFileLoader("agent_service_setup", str(SETUP))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses resolve their annotations through it
    loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


@dataclass
class DockerHost:
    """Docker as the setup meets it: compose names the image, `image inspect` the built ones."""

    images: set[str] = field(default_factory=set)
    commands: list[list[str]] = field(default_factory=list)
    answers: bool = True

    def __call__(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if not self.answers:
            return subprocess.CompletedProcess(
                command, 1, "[]", "Cannot connect to the Docker daemon"
            )
        if command[:2] == ["docker", "compose"]:
            config = {"services": {"agent-service": {"image": IMAGE}}}
            return subprocess.CompletedProcess(command, 0, json.dumps(config), "")
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] in self.images:
                return subprocess.CompletedProcess(command, 0, "[]", "")
            return subprocess.CompletedProcess(
                command, 1, "[]", f"Error: No such image: {command[3]}"
            )
        assert command[:2] == ["docker", "build"], command
        self.images.add(command[command.index("-t") + 1])
        return subprocess.CompletedProcess(command, 0, "", "")

    def builds(self) -> list[list[str]]:
        return [command for command in self.commands if command[:2] == ["docker", "build"]]


@dataclass
class Terminal:
    """The operator's terminal: what the script says, and the lines he pastes in turn."""

    pastes: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=lambda: ["yes"])
    interactive: bool = True

    def say(self, line: str) -> None:
        self.said.append(line)

    def read_secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.pastes.pop(0)

    def confirm(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def has_terminal(self) -> bool:
        return self.interactive


def generate_key(*options: str) -> bytes:
    """A throwaway RSA key made by host openssl; the tests own every key they use."""
    command = ["openssl", "genpkey", "-algorithm", "RSA", *options]
    return subprocess.run(command, capture_output=True, check=True).stdout


@functools.cache
def sample_pair(index: int) -> tuple[bytes, bytes]:
    """A (private, public) pair of 3072 bits; each index is another key."""
    private = generate_key("-pkeyopt", "rsa_keygen_bits:3072")
    public = subprocess.run(
        ["openssl", "pkey", "-pubout"], input=private, capture_output=True, check=True
    )
    return private, public.stdout


@dataclass
class Openssl:
    """Host openssl, except that `genpkey` hands out the sample keys in turn and named subcommands
    exit with a code or answer exit 0 with forged output; the argument lists are kept so a test can
    look for key bytes in them."""

    generated: list[bytes] = field(default_factory=lambda: [sample_pair(i)[0] for i in (1, 2, 3)])
    failing: dict[str, int] = field(default_factory=dict)
    forged: dict[str, bytes] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, arguments: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(arguments)
        for name in (arguments[0], " ".join(arguments[:2])):
            if name in self.failing:
                return subprocess.CompletedProcess(arguments, self.failing[name], b"", b"failed")
            if name in self.forged:
                return subprocess.CompletedProcess(arguments, 0, self.forged[name], b"")
        if arguments[0] == "genpkey":
            return subprocess.CompletedProcess(arguments, 0, self.generated.pop(0), b"")
        return subprocess.run(
            ["openssl", *arguments], input=stdin, capture_output=True, check=False
        )


@pytest.fixture
def openssl() -> Openssl:
    return Openssl()


@pytest.fixture
def docker() -> DockerHost:
    return DockerHost(images={IMAGE})


@pytest.fixture
def terminal() -> Terminal:
    return Terminal(pastes=[DUMMY_TOKEN])


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def make_setup(setup_module, docker, terminal, home, openssl) -> Callable[..., object]:
    def build(service_uid: int | None = None, read_secret=None):
        tools = setup_module.Tools(
            run=docker,
            say=terminal.say,
            read_secret=read_secret or terminal.read_secret,
            service_uid=os.getuid() if service_uid is None else service_uid,
            openssl=openssl,
            has_terminal=terminal.has_terminal,
            confirm=terminal.confirm,
        )
        return setup_module.AgentServiceSetup(tools, home)

    return build


def login_directory(home: Path) -> Path:
    return home / ".secrets" / "agent-service" / "claude"


def token_path(home: Path) -> Path:
    return login_directory(home) / "token"


def make_login_directory(home: Path) -> None:
    for directory in (
        home / ".secrets",
        home / ".secrets" / "agent-service",
        login_directory(home),
    ):
        directory.mkdir(mode=0o700, exist_ok=True)


def signing_directory(home: Path) -> Path:
    return home / ".secrets" / "agent-service" / "signing"


def lay_signing_pair(home: Path, index: int = 0) -> None:
    directory = signing_directory(home)
    directory.mkdir(mode=0o700, exist_ok=True)
    for name, contents, mode in zip(
        ("private.pem", "public.pem"), sample_pair(index), (0o600, 0o644), strict=True
    ):
        (directory / name).write_bytes(contents)
        (directory / name).chmod(mode)


def write_token(home: Path, contents: bytes | str, mode: int = 0o600) -> None:
    """The login of a set-up host, with the signing pair beside it."""
    make_login_directory(home)
    lay_signing_pair(home)
    path = token_path(home)
    path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
    path.chmod(mode)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_set_up_host_passes_the_check_and_prints_nothing_secret(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN + "\n")

    assert make_setup().check() == 0
    assert all(DUMMY_TOKEN not in line for line in terminal.said)


def test_the_check_says_the_image_is_not_built(make_setup, home, docker, terminal):
    write_token(home, DUMMY_TOKEN)
    docker.images.clear()

    assert make_setup().check() == 1
    assert terminal.said[-1] == f"the image {IMAGE} is not built"


def test_the_check_cannot_tell_when_docker_does_not_answer(make_setup, docker, terminal):
    docker.answers = False

    assert make_setup().check() == 2
    assert terminal.said[-1] == "`docker compose config` exited 1: Cannot connect to the Docker daemon"


def test_a_missing_login_directory_is_not_set_up_and_the_check_changes_nothing(
    make_setup, home, terminal
):
    assert make_setup().check() == 1
    assert "does not exist" in terminal.said[-1]
    assert not (home / ".secrets").exists()


@pytest.mark.parametrize(
    ("path_of", "open_mode", "mended_mode"),
    [
        pytest.param(login_directory, 0o755, 0o700, id="login directory"),
        pytest.param(lambda h: signing_directory(h) / "public.pem", 0o600, 0o644, id="public half"),
    ],
)
def test_a_folder_or_public_half_with_another_mode_is_set_to_its_mode(
    make_setup, home, path_of, open_mode, mended_mode
):
    write_token(home, DUMMY_TOKEN)
    path_of(home).chmod(open_mode)
    assert make_setup().check() == 1

    assert make_setup().run(renew=False) == 0
    assert mode_of(path_of(home)) == mended_mode


def test_a_directory_owned_by_another_user_is_reported_not_taken(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)

    assert make_setup(service_uid=os.getuid() + 1).run(renew=False) == 1
    assert "owned by uid" in terminal.said[-1]
    assert mode_of(token_path(home)) == 0o600


@pytest.mark.parametrize("mode", [0o664, 0o640, 0o604])
def test_a_token_file_open_to_group_or_others_is_narrowed_without_asking_for_a_token(
    make_setup, home, terminal, mode
):
    write_token(home, DUMMY_TOKEN, mode=mode)
    assert make_setup().check() == 1
    assert f"mode {mode:04o}" in terminal.said[-1]

    assert make_setup().run(renew=False) == 0
    assert mode_of(token_path(home)) == 0o600
    assert token_path(home).read_text() == DUMMY_TOKEN
    assert terminal.prompts == []


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (b'{"claudeAiOauth": {"accessToken": "x"}}', "JSON"),
        (b'"quoted"', "JSON"),
        (b"sk-ant-api03-dummy\n", "API key"),
        (b"first-line\nsecond-line\n", "more than one line"),
        (b"has a space\n", "whitespace"),
        (b"\n", "empty"),
        (b"tok\x07en\n", "non-printing"),
        (b"\xff\xfe", "UTF-8"),
        pytest.param(b"x" * (16 * 1024 + 1), "larger", id="too-big"),
    ],
)
def test_a_refused_token_file_is_not_set_up_and_never_shown(
    make_setup, home, terminal, contents, reason
):
    write_token(home, contents)

    assert make_setup().check() == 1
    assert reason in terminal.said[-1]
    shown = contents.decode(errors="ignore").strip()
    assert not shown or shown not in " ".join(terminal.said)


def test_a_token_file_that_is_not_a_regular_file_is_refused(make_setup, home, terminal):
    make_login_directory(home)
    token_path(home).mkdir()

    assert make_setup().check() == 1
    assert "not a regular file" in terminal.said[-1]


def test_a_refused_token_is_replaced_by_the_pasted_one(make_setup, home, terminal):
    write_token(home, b'{"accessToken": "x"}')

    assert make_setup().run(renew=False) == 0
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"
    assert mode_of(token_path(home)) == 0o600
    assert not (login_directory(home) / "token.new").exists()
    assert make_setup().check() == 0


def test_a_fresh_home_builds_makes_the_folders_takes_the_token_and_then_passes(
    make_setup, home, docker, terminal
):
    docker.images.clear()

    assert make_setup().run(renew=False) == 0
    assert make_setup().check() == 0
    assert [command[:2] for command in docker.builds()] == [["docker", "build"]]
    assert IMAGE in docker.builds()[0]
    assert mode_of(home / ".secrets" / "agent-service") == 0o700
    assert mode_of(login_directory(home)) == 0o700
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


def test_a_second_run_changes_nothing_and_asks_nothing(make_setup, home, docker, terminal):
    docker.images.clear()
    make_setup().run(renew=False)
    before = token_path(home).stat()
    builds_before = len(docker.builds())
    prompts_before = len(terminal.prompts)

    assert make_setup().run(renew=False) == 0
    assert len(docker.builds()) == builds_before
    assert len(terminal.prompts) == prompts_before
    assert token_path(home).stat().st_ino == before.st_ino


def test_the_token_never_appears_in_anything_the_script_says(make_setup, home, docker, terminal):
    docker.images.clear()
    make_setup().run(renew=False)

    assert not any(DUMMY_TOKEN in line for line in terminal.said + terminal.prompts)
    assert not any(DUMMY_TOKEN in word for command in docker.commands for word in command)


@pytest.mark.parametrize(
    "pasted",
    ['{"accessToken": "x"}', "sk-ant-api03-dummy", "two words", "", "   ", "tok\x07en"],
)
def test_a_refused_paste_writes_nothing(make_setup, home, terminal, pasted):
    make_login_directory(home)
    terminal.pastes = [pasted]

    assert make_setup().run(renew=False) == 1
    assert not token_path(home).exists()
    assert not (login_directory(home) / "token.new").exists()
    assert "nothing was written" in terminal.said[-1]
    assert not pasted.strip() or pasted.strip() not in terminal.said[-1]


def test_a_paste_with_echo_unavailable_fails_closed(
    setup_module, make_setup, home, terminal, monkeypatch
):
    def getpass_without_echo_control(prompt: str) -> str:
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        return DUMMY_TOKEN

    monkeypatch.setattr(getpass, "getpass", getpass_without_echo_control)
    make_login_directory(home)

    assert make_setup(read_secret=setup_module.read_pasted_line).run(renew=False) == 1
    assert not token_path(home).exists()
    assert "cannot hide the paste" in terminal.said[-1]


def test_renew_swaps_in_a_new_token_beside_the_old_one(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    terminal.pastes = [SECOND_TOKEN]

    assert make_setup().run(renew=True) == 0
    assert token_path(home).read_text() == SECOND_TOKEN + "\n"
    assert mode_of(token_path(home)) == 0o600
    assert not (login_directory(home) / "token.new").exists()


def test_renew_with_a_missing_token_asks_once(make_setup, home, terminal):
    make_login_directory(home)

    assert make_setup().run(renew=True) == 0
    assert len(terminal.prompts) == 1


def test_a_stale_pending_file_never_blocks_the_swap(make_setup, home, terminal):
    write_token(home, b"{}")
    (login_directory(home) / "token.new").write_text("stale", encoding="utf-8")

    assert make_setup().run(renew=False) == 0
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


def pending_path(home: Path) -> Path:
    return login_directory(home) / "token.new"


def test_a_token_file_owned_by_another_user_is_reported_not_taken(
    setup_module, make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN)
    monkeypatch.setattr(setup_module.AgentServiceSetup, "judge_directory", lambda *_: None)

    assert make_setup(service_uid=os.getuid() + 1).check() == 1
    assert f"{token_path(home)} is owned by uid" in terminal.said[-1]


def test_a_directory_narrowed_to_0700_says_so(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN)
    login_directory(home).chmod(0o755)

    make_setup().run(renew=False)
    assert f"narrowing {login_directory(home)} to 0700" in terminal.said


def test_the_check_reads_a_token_in_short_reads_as_the_reader_does(
    make_setup, home, terminal, monkeypatch
):
    write_token(home, "first\nsecond\n")
    read = os.read
    monkeypatch.setattr(os, "read", lambda descriptor, count: read(descriptor, min(count, 2)))

    assert make_setup().check() == 1
    assert "more than one line" in terminal.said[-1]


def test_an_oversized_paste_leaves_the_old_token_byte_for_byte(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN + "\n")
    terminal.pastes = ["x" * (16 * 1024)]  # printable, and with its newline one byte too big

    assert make_setup().run(renew=True) == 1
    assert "larger than 16384 bytes" in terminal.said[-1]
    assert token_path(home).read_bytes() == (DUMMY_TOKEN + "\n").encode()
    assert not pending_path(home).exists()


@pytest.mark.parametrize("linked", ["agent-service", "claude"])
@pytest.mark.parametrize("renew", [False, True])
def test_a_symlinked_folder_is_refused_before_the_paste_and_nothing_is_written(
    make_setup, home, terminal, tmp_path, linked, renew
):
    make_login_directory(home)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if linked == "agent-service":
        (outside / "claude").mkdir(mode=0o700)
        os.replace(home / ".secrets" / "agent-service", tmp_path / "moved")
        (home / ".secrets" / "agent-service").symlink_to(outside)
    else:
        login_directory(home).rmdir()
        login_directory(home).symlink_to(outside)

    assert make_setup().run(renew=renew) == 1
    assert ("symbolic link" if linked == "claude" else "not a plain folder") in terminal.said[-1]
    assert terminal.prompts == []
    assert list(outside.rglob("*")) == ([outside / "claude"] if linked == "agent-service" else [])


def test_a_folder_swapped_for_a_link_after_the_check_still_receives_nothing(
    setup_module, make_setup, home, terminal, tmp_path
):
    make_login_directory(home)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    setup = make_setup()

    def swap_then_paste(prompt: str) -> str:
        login_directory(home).rmdir()
        login_directory(home).symlink_to(outside)
        return DUMMY_TOKEN

    setup.tools = setup_module.Tools(
        run=setup.tools.run, say=terminal.say, read_secret=swap_then_paste,
        service_uid=os.getuid(),
    )  # fmt: skip

    assert setup.run(renew=False) == 1
    assert "not a plain folder" in terminal.said[-1]
    assert list(outside.iterdir()) == []


def test_the_end_of_the_input_at_the_paste_is_one_line_and_keeps_the_old_token(
    setup_module, make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN + "\n")

    def input_ends(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(getpass, "getpass", input_ends)

    assert make_setup(read_secret=setup_module.read_pasted_line).run(renew=True) == 1
    assert "nothing was written" in terminal.said[-1]
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


def test_a_stale_pending_folder_is_one_line_and_keeps_the_old_token(make_setup, home, terminal):
    write_token(home, DUMMY_TOKEN + "\n")
    pending_path(home).mkdir()
    terminal.pastes = [SECOND_TOKEN]

    assert make_setup().run(renew=True) == 1
    assert "token.new" in terminal.said[-1]
    assert "the old token is kept" in terminal.said[-1]
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


def test_a_failing_write_removes_the_pending_file_and_keeps_the_old_token(
    make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN + "\n")
    terminal.pastes = [SECOND_TOKEN]

    def disk_fails(descriptor: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "fsync", disk_fails)

    assert make_setup().run(renew=True) == 1
    assert "Input/output error" in terminal.said[-1]
    assert not pending_path(home).exists()
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


@pytest.mark.parametrize("failing", ["lstat", "read"])
def test_a_host_that_cannot_be_read_makes_the_check_cannot_tell_not_not_set_up(
    make_setup, home, terminal, monkeypatch, failing
):
    write_token(home, DUMMY_TOKEN + "\n")
    original = getattr(os, failing)
    refused = OSError(errno.EIO, os.strerror(errno.EIO))

    def fail_on_the_login_folder(*arguments):
        if failing == "read" or str(login_directory(home)) == str(arguments[0]):
            raise refused
        return original(*arguments)

    monkeypatch.setattr(os, failing, fail_on_the_login_folder)

    assert make_setup().check() == 2
    assert terminal.said[-1] == "Input/output error"


def test_a_renewal_holds_a_lock_on_the_login_folder_from_cleanup_to_rename(
    make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN + "\n")
    terminal.pastes = [SECOND_TOKEN]
    fsync = os.fsync
    competitors_refused: list[bool] = []

    def fsync_while_a_second_run_tries_the_lock(descriptor: int) -> None:
        competitor = os.open(login_directory(home), os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            competitors_refused.append(False)
        except BlockingIOError:
            competitors_refused.append(True)
        finally:
            os.close(competitor)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync_while_a_second_run_tries_the_lock)

    assert make_setup().run(renew=True) == 0
    assert competitors_refused
    assert all(competitors_refused)


def test_a_folder_that_cannot_be_synced_after_the_rename_says_the_new_token_is_in_place(
    make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN + "\n")
    terminal.pastes = [SECOND_TOKEN]
    fsync = os.fsync

    def only_the_folder_fails(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", only_the_folder_fails)

    assert make_setup().run(renew=True) == 1
    assert "the new token is in place" in terminal.said[-1]
    assert "Input/output error" in terminal.said[-1]
    assert "old token is kept" not in terminal.said[-1]
    assert token_path(home).read_text() == SECOND_TOKEN + "\n"


@pytest.mark.parametrize("target_exists", [False, True], ids=["dangling", "valid"])
def test_a_symlinked_token_is_not_set_up_and_setup_replaces_the_link_not_its_target(
    make_setup, home, terminal, tmp_path, target_exists
):
    make_login_directory(home)
    target = tmp_path / "elsewhere-token"
    if target_exists:
        target.write_text(DUMMY_TOKEN + "\n")
        target.chmod(0o640)
    token_path(home).symlink_to(target)

    assert make_setup().check() == 1
    assert "is a symbolic link; it must be a regular file" in terminal.said[-1]

    terminal.pastes = [SECOND_TOKEN]
    assert make_setup().run(renew=False) == 0
    assert not token_path(home).is_symlink()
    assert token_path(home).read_text() == SECOND_TOKEN + "\n"
    assert mode_of(token_path(home)) == 0o600
    assert target.exists() == target_exists
    if target_exists:
        assert target.read_text() == DUMMY_TOKEN + "\n"
        assert mode_of(target) == 0o640
    assert make_setup().check() == 0


def test_an_ancestor_swapped_for_a_link_after_the_check_is_never_narrowed_through(
    setup_module, make_setup, home, terminal, tmp_path
):
    write_token(home, DUMMY_TOKEN + "\n")
    login_directory(home).chmod(0o755)
    outside = tmp_path / "outside"
    (outside / "claude").mkdir(parents=True, mode=0o755)
    (outside / "claude").chmod(0o755)

    def swap_when_narrowing(line: str) -> None:
        terminal.say(line)
        if line.startswith("narrowing"):
            os.replace(home / ".secrets" / "agent-service", tmp_path / "moved")
            (home / ".secrets" / "agent-service").symlink_to(outside)

    setup = make_setup()
    setup.tools = setup_module.Tools(
        run=setup.tools.run, say=swap_when_narrowing, read_secret=terminal.read_secret,
        service_uid=os.getuid(),
    )  # fmt: skip

    assert setup.run(renew=False) == 1
    assert "not a plain folder" in terminal.said[-1]
    assert mode_of(outside / "claude") == 0o755


def test_a_pasted_line_that_is_not_utf8_is_one_line_and_chains_nothing(
    setup_module, make_setup, home, terminal, monkeypatch
):
    write_token(home, DUMMY_TOKEN + "\n")

    def not_utf8(prompt: str) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff-secret", 0, 1, "invalid start byte")

    monkeypatch.setattr(getpass, "getpass", not_utf8)

    assert make_setup(read_secret=setup_module.read_pasted_line).run(renew=True) == 1
    assert terminal.said[-1] == (
        "agent-service/setup FAILED: the pasted line is not UTF-8 text; nothing was written"
    )
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"


def test_a_token_that_cannot_be_read_is_refused_and_replaced_by_the_paste(
    make_setup, home, terminal
):
    write_token(home, DUMMY_TOKEN + "\n", mode=0o000)

    assert make_setup().check() == 1
    assert terminal.said[-1].endswith("cannot be read: Permission denied")

    assert make_setup().run(renew=False) == 0
    assert token_path(home).read_text() == DUMMY_TOKEN + "\n"
    assert make_setup().check() == 0


def test_a_run_that_finds_the_lock_held_says_it_waits(make_setup, home, terminal, monkeypatch):
    write_token(home, DUMMY_TOKEN + "\n")
    terminal.pastes = [SECOND_TOKEN]
    flock = fcntl.flock

    def held_until_the_blocking_attempt(descriptor: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            raise BlockingIOError
        flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", held_until_the_blocking_attempt)

    assert make_setup().run(renew=True) == 0
    assert "waiting for another run of agent-service/setup" in terminal.said
    assert token_path(home).read_text() == SECOND_TOKEN + "\n"


def test_a_long_docker_diagnostic_is_cut_to_a_bounded_line(setup_module):
    result = subprocess.CompletedProcess(["docker"], 1, "", "first\n" + "x" * 5000 + "\n")

    line = setup_module.failure_line("docker build", result)

    assert line.startswith("`docker build` exited 1: xxx")
    assert len(line) < 300
