"""The agent service's API credential for agent-service/setup: judged, made and replaced through
the verified descriptor of ~/.secrets/agent-service/api. `setup` is the only caller.

The setup contract is docs/agent-service.md, "The API credential". The file is the JSON document
agent-runtime's reader accepts, `{"shared_key": ...}`. The rules of that reader live once on the
hal side, in agent-service/litellm/gate.py, and this module judges the bytes it read through them.
The key is never printed.
"""

from __future__ import annotations

import contextlib
import errno
import importlib.util
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Protocol

from signing_key import StepFailure, read_within_limit

API_DIRECTORY = "api"
CREDENTIAL_FILE = "credential.json"
CANDIDATE = "credential.json.new"
NEXT_FILE = "credential.next.json"
LEFTOVERS = (CANDIDATE, NEXT_FILE)
CREDENTIAL_MODE = 0o600
KEY_BYTES = 32
GATE = Path(__file__).resolve().parent / "litellm" / "gate.py"
REPLACE_FLAG = "agent-service/setup --replace-api-credential"
RECREATES = (
    "recreate litellm; while service.toml has an [api] table, recreate agent-service as well"
)


@cache
def gate() -> ModuleType:
    """The credential reader's rules, loaded from the file LiteLLM mounts: one hal-side copy."""
    name = "agent_service_credential_gate"
    spec = importlib.util.spec_from_file_location(name, GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclass resolves annotations through it
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


class CredentialState(Enum):
    SOUND = "sound"
    MISSING = "no credential yet"
    REFUSED = "needs --replace-api-credential"
    OPERATOR_FIX = "needs the operator"


@dataclass(frozen=True)
class Entry:
    """A credential file as one descriptor saw it; `problem` names what the operator fixes."""

    path: Path
    status: os.stat_result
    contents: bytes = b""
    problem: str | None = None


class ApiTools(Protocol):
    service_uid: int
    say: Callable[[str], None]
    has_terminal: Callable[[], bool]
    confirm: Callable[[str], str]


class ApiHost(Protocol):
    """What `setup` lends the credential: its folders, its locking and its owner verdicts."""

    tools: ApiTools
    api_directory: Path
    api_chain: tuple[Path, ...]

    def open_verified_directories(
        self, directories: tuple[Path, ...], tolerate_open_mode: bool = False
    ) -> int: ...

    def lock(self, directory: int) -> None: ...

    def directory_gap(self, directory: Path): ...  # a Gap or None

    def wrong_owner(self, path: Path, uid: int): ...  # a Gap

    def write_pending(self, directory: int, payload: bytes, name: str, mode: int) -> None: ...


class ApiCredential:
    """The service credential, judged and written through the api folder's verified descriptor."""

    def __init__(self, setup: ApiHost) -> None:
        self.setup = setup

    def open_folder(self) -> int:
        return self.setup.open_verified_directories(self.setup.api_chain)

    def preflight(self) -> None:
        """Refuse a credential `setup` cannot use before it changes anything else. A folder that is
        not made yet or is open to others passes: the folder steps of `setup` mend those."""
        try:
            directory = self.setup.open_verified_directories(
                self.setup.api_chain, tolerate_open_mode=True
            )
        except FileNotFoundError:
            return
        try:
            state, line = self.judge(directory)
        finally:
            os.close(directory)
        if state in (CredentialState.REFUSED, CredentialState.OPERATOR_FIX):
            raise StepFailure(line)

    def check(self) -> None:
        directory = self.open_folder()
        try:
            state, line = self.judge(directory)
            leftover = CANDIDATE in self.leftovers_found(directory)
        finally:
            os.close(directory)
        if state is not CredentialState.SOUND:
            raise StepFailure(line)
        if leftover:
            raise StepFailure(
                f"{CANDIDATE} left by an interrupted run; `agent-service/setup` clears"
            )

    def judge(self, directory: int) -> tuple[CredentialState, str]:
        """The first thing wrong with the credential, in the order a fix can follow."""
        credential = self.read_entry(directory)
        statuses = self.leftovers_found(directory)
        problems = [credential.problem] if credential else []
        problems += [self.entry_problem(self.path_of(name), s) for name, s in statuses.items()]
        if problem := next(filter(None, problems), None):
            return CredentialState.OPERATOR_FIX, f"{problem}; fix it yourself"
        if credential is None:
            return CredentialState.MISSING, f"{self.path_of(CREDENTIAL_FILE)} does not exist"
        mode = stat.S_IMODE(credential.status.st_mode)
        if mode != CREDENTIAL_MODE:
            line = f"{credential.path} has mode {mode:04o}, not 0600; fix it yourself"
            return CredentialState.OPERATOR_FIX, line
        if gate().shared_key_of(credential.contents) is None:
            line = (
                f"{credential.path} is refused by the service's credential reader: {REPLACE_FLAG}"
            )
            return CredentialState.REFUSED, line
        return CredentialState.SOUND, ""

    def path_of(self, name: str) -> Path:
        return self.setup.api_directory / name

    @staticmethod
    def leftovers_found(directory: int) -> dict[str, os.stat_result]:
        """The temporary entries that exist, each looked at without following a link."""
        found = {}
        for name in LEFTOVERS:
            try:
                found[name] = os.lstat(name, dir_fd=directory)
            except FileNotFoundError:
                continue
        return found

    def entry_problem(self, path: Path, status: os.stat_result) -> str | None:
        """A link, no regular file or another owner is never narrowed or unlinked."""
        if stat.S_ISLNK(status.st_mode):
            return f"{path} is a symbolic link"
        if not stat.S_ISREG(status.st_mode):
            return f"{path} is not a regular file"
        if status.st_uid != self.setup.tools.service_uid:
            return self.setup.wrong_owner(path, status.st_uid).line
        return None

    def read_entry(self, directory: int, name: str = CREDENTIAL_FILE) -> Entry | None:
        path = self.path_of(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except PermissionError:
            status = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if status.st_mode & stat.S_IRUSR:
                raise  # its owner may read it, so the host failed, not the file
            return Entry(path, status, problem=self.entry_problem(path, status))
        except OSError as error:
            if error.errno == errno.ELOOP:
                status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                return Entry(path, status, problem=f"{path} is a symbolic link")
            raise
        try:
            status = os.fstat(descriptor)
            if problem := self.entry_problem(path, status):
                return Entry(path, status, problem=problem)
            return Entry(path, status, read_within_limit(descriptor))
        finally:
            os.close(descriptor)

    def under_lock(self, action: Callable[[int], None]) -> None:
        """Run `action` on the verified api folder while this run holds its lock."""
        directory = self.open_folder()
        try:
            self.setup.lock(directory)
            action(directory)
        finally:
            os.close(directory)

    def ensure(self) -> None:
        """Make the credential when there is none and clear a leftover `.new`; one that is present
        but refused is never replaced here."""
        self.under_lock(self.make_missing)

    def make_missing(self, directory: int) -> None:
        state, line = self.judge(directory)
        if state in (CredentialState.REFUSED, CredentialState.OPERATOR_FIX):
            raise StepFailure(line)
        if state is CredentialState.MISSING:
            self.new_candidate(directory, CANDIDATE)
            self.publish(directory)
        elif CANDIDATE in self.leftovers_found(directory):
            os.unlink(CANDIDATE, dir_fd=directory)
            os.fsync(directory)
            self.setup.tools.say(f"removed leftovers: {CANDIDATE}")

    def new_candidate(self, directory: int, name: str) -> None:
        """A new credential in `name`, kept only if the reader accepts what was written."""
        document = json.dumps({"shared_key": secrets.token_urlsafe(KEY_BYTES)}).encode()
        try:
            self.setup.write_pending(directory, document, name, CREDENTIAL_MODE)
            entry = self.read_entry(directory, name)
            if entry is None or entry.problem or gate().shared_key_of(entry.contents) is None:
                raise StepFailure("the new credential failed its check; the old one is unchanged")
        except BaseException:
            with contextlib.suppress(OSError):  # the failure being reported is the first one
                os.unlink(name, dir_fd=directory)
            raise

    def publish(self, directory: int) -> None:
        """link(2) refuses any existing entry, so a credential made meanwhile is never replaced."""
        folder = self.setup.api_directory
        os.link(CANDIDATE, CREDENTIAL_FILE, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)  # fmt: skip
        os.unlink(CANDIDATE, dir_fd=directory)
        os.fsync(directory)
        self.setup.tools.say(f"made the API credential in {folder}; nothing is shown")

    def replace(self) -> None:
        """A new credential over the old one after a typed yes at a terminal."""
        tools = self.setup.tools
        if not tools.has_terminal():
            raise StepFailure("--replace-api-credential needs a terminal; nothing was changed")
        if gap := self.setup.directory_gap(self.setup.api_directory):
            raise StepFailure(f"{gap.line}; run agent-service/setup first")
        tools.say(
            "--replace-api-credential replaces the shared key and deletes the old one; "
            "replacement alone revokes nothing: the old key stays accepted until litellm and, "
            "while service.toml has an [api] table, agent-service are recreated"
        )
        try:
            answer = tools.confirm("type yes to replace the API credential: ")
        except EOFError:
            answer = ""
        if answer.strip() != "yes":
            raise StepFailure("not confirmed; nothing was changed")
        self.under_lock(self.swap_in)
        tools.say(f"replaced the API credential; nothing is shown. Now {RECREATES}")

    def swap_in(self, directory: int) -> None:
        state, line = self.judge(directory)
        if state is CredentialState.OPERATOR_FIX:
            raise StepFailure(line)
        for name in LEFTOVERS:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
        self.new_candidate(directory, NEXT_FILE)
        os.replace(NEXT_FILE, CREDENTIAL_FILE, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
