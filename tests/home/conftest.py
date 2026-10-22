from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from home_tools import cli, secrets_view, system_step
from home_tools.secrets_view import load_manifest
from home_tools.setup import QUALITY_SOURCE
from home_tools.tidy_home import Layout, run_command

DAY_SECONDS = 24 * 60 * 60

# Path-shaped tokens make these entries known, so the state `home` writes never becomes a finding itself.
HOME_README = "`.local/state`, `.config/home`, `.secrets` and `git/` belong here.\n"

# Every planted secret file holds this; no output and no report may ever contain any of it.
FAKE_SECRET_VALUE = "FAKE-SECRET-VALUE ghp_0000notarealtoken glpat-0000notarealtoken"


def symlinked_folder_fix(place: str) -> str:
    """The command `home secrets` names for a symlink on the way to a secret: the folder it points to, or a new one, takes its place."""
    real_folder = f'if [ -e "$t" ]; then mv -T "$t" {place}; else mkdir {place}; fi'
    return f"t=$(readlink -m {place}) && rm {place} && {real_folder} && chmod 700 {place}"


# A small manifest of three kinds of place: one file, a wildcard plus a file, and a verified tool login.
TEST_MANIFEST = """\
[[secret]]
name = "gitlab"
kind = "token"
paths = ["~/.secrets/gitlab"]
read_by = [".bashrc"]
recreate = "https://gitlab.example/-/user_settings/personal_access_tokens"

[[secret]]
name = "cloudflared"
kind = "tool-login"
paths = ["~/.secrets/cloudflared/*.json", "~/.secrets/cloudflared/cert.pem"]
read_by = ["cloudflared.service", ".bashrc"]
recreate = "`cloudflared tunnel login`"

[[secret]]
name = "gh"
kind = "tool-login"
paths = ["~/.config/gh"]
read_by = ["gh"]
recreate = "`gh auth login --web`"
verify = "gh auth status"
"""


@pytest.fixture
def use_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    """Points `home secrets` at a manifest written from the given text."""

    def use(text: str) -> Path:
        path = tmp_path / "secrets_manifest.toml"
        path.write_text(text)
        monkeypatch.setattr(secrets_view, "MANIFEST_PATH", path)
        return path

    return use


def plant_secrets(home: Path) -> None:
    """Every place the active manifest names for a live entry gets a fake secret, with the modes a tidy ~/.secrets keeps."""
    for entry in load_manifest(secrets_view.MANIFEST_PATH):
        for pattern in () if entry.retired else entry.paths:
            path = home / str(pattern).replace("*", "fake")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE_SECRET_VALUE)
            path.chmod(0o600)
    secrets_dir = Layout(home).secrets_dir
    for directory in (secrets_dir, *(path for path in secrets_dir.rglob("*") if path.is_dir())):
        directory.chmod(0o700)


@pytest.fixture
def furnish_clean_home(use_manifest: Callable[[str], Path]) -> Callable[[Path], Path]:
    """Makes a home directory one with nothing to report: the README knows every entry and every secret of the test manifest is in place."""

    def furnish(home: Path) -> Path:
        use_manifest(TEST_MANIFEST)
        home.mkdir(exist_ok=True)
        (home / "README.md").write_text(HOME_README)
        plant_secrets(home)
        return home

    return furnish


@pytest.fixture
def clean_home(tmp_path: Path, furnish_clean_home: Callable[[Path], Path]) -> Path:
    return furnish_clean_home(tmp_path / "home")


@pytest.fixture
def leave_pasted_image() -> Callable[[Path, int], Path]:
    """A pasted screenshot `age_days` old; past the default week it is a pasted-images finding."""

    def leave(directory: Path, age_days: int = 30) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        image = directory / "Eingefügtes Bild.png"
        image.write_bytes(b"png")
        stamp = time.time() - age_days * DAY_SECONDS
        os.utime(image, (stamp, stamp))
        return image

    return leave


class RecordingRunner:
    """Delivers notifications (systemd-run succeeds only if notify-send did), fails every other tool as missing, records calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.notify_delivers = True

    def __call__(self, argv: Sequence[str]) -> str | None:
        self.calls.append(list(argv))
        return "" if argv[0] == "systemd-run" and self.notify_delivers else None

    @property
    def notifications(self) -> list[str]:
        """The summary line of every notification sent."""
        return [call[-3] for call in self.notification_calls]

    @property
    def notification_bodies(self) -> list[str]:
        return [call[-2] for call in self.notification_calls]

    @property
    def notification_calls(self) -> list[list[str]]:
        """The detached waiter commands: [..., summary, body, report path]."""
        return [call for call in self.calls if call[0] == "systemd-run"]


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


class RunnerWithRealGitAndQuality(RecordingRunner):
    """The hook check reads git and the stub quality, so these two run for real; every other tool stays as in RecordingRunner."""

    def __call__(self, argv: Sequence[str]) -> str | None:
        if Path(argv[0]).name in {"git", "quality"}:
            self.calls.append(list(argv))
            return run_command(argv)
        return super().__call__(argv)


class FakeProbe:
    """Answers every probe with one exit code (None: the tool hung or is missing) and remembers what it ran."""

    def __init__(self) -> None:
        self.code: int | None = 0
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> int | None:
        self.calls.append(tuple(argv))
        return self.code


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


# What the stub scanner refuses; assembled from parts, as every fake key in these tests is.
FAKE_KEY = "FAKE" + "KEY-" + "0000"

STUB_QUALITY = """\
#!/bin/sh
case "$1" in
scan-staged)
    [ -n "$STUB_SCAN_EXIT" ] && exit "$STUB_SCAN_EXIT"
    if git diff --cached | grep -q "FAKE""KEY-"; then
        echo "quality: secret.txt fake-key sha256=0000 (Wert nicht gezeigt)"
        exit 1
    fi
    ;;
engines)
    case "$2" in
    install)
        [ -n "$STUB_ENGINES_FAIL" ] && exit 1
        mkdir -p "$3" && touch "$3/installed"
        ;;
    verify) [ -e "$3/installed" ] || exit 1 ;;
    esac
    ;;
esac
exit 0
"""

# The `git` value real uv 0.10.9 wrote to uv-receipt.toml after installing QUALITY_SOURCE's pinned SHA; only the revision follows the pin.
RECORDED_RECEIPT_GIT = f"https://github.com/overnightworks/quality?rev={QUALITY_SOURCE.rpartition('@')[2]}"

# Writes what `uv tool install` leaves behind for the requirement it is given: the tool's executable and uv's receipt for it.
# Ansible's executable is a dummy, since the sandbox hands the system step a FakePlaybook instead of running it.
STUB_UV = """\
#!/bin/sh
echo "$@" >> "$STUB_UV_LOG"
[ -n "$STUB_UV_FAIL" ] && exit 1
mkdir -p "$HOME/.local/bin"
case "$4" in
ansible-core==*)
    mkdir -p "$HOME/.local/share/uv/tools/ansible-core"
    printf '#!/bin/sh\\nexit 1\\n' > "$HOME/.local/bin/ansible-playbook" && chmod 755 "$HOME/.local/bin/ansible-playbook"
    printf '[tool]\\nrequirements = [{ name = "ansible-core", specifier = "==%s" }]\\n' "${4#ansible-core==}" \\
        > "$HOME/.local/share/uv/tools/ansible-core/uv-receipt.toml"
    ;;
*)
    mkdir -p "$HOME/.local/share/uv/tools/overnightworks-quality"
    cp "$STUB_QUALITY" "$HOME/.local/bin/quality" && chmod 755 "$HOME/.local/bin/quality"
    printf '[tool]\\nrequirements = [{ name = "overnightworks-quality", git = "%s" }]\\n' "$STUB_RECEIPT_GIT" \\
        > "$HOME/.local/share/uv/tools/overnightworks-quality/uv-receipt.toml"
    ;;
esac
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class FakePlaybook:
    """Ansible as the system step sees it: a converging playbook, whose check finds a change until a run has applied it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.succeeds = True
        self.applied = False

    def __call__(self, argv: Sequence[str]) -> system_step.PlaybookRun:
        self.calls.append(list(argv))
        changed = 0 if self.applied else 1
        if self.succeeds and "--check" not in argv:
            self.applied = True
        return system_step.PlaybookRun(self.succeeds, changed)


@dataclass
class FakeSystem:
    """The system step's boundaries: a terminal that is there or not, the FakePlaybook, and a fixed operator."""

    playbook: FakePlaybook
    terminal: bool = True

    @property
    def boundaries(self) -> system_step.Boundaries:
        return system_step.Boundaries(lambda: self.terminal, self.playbook, lambda: "operator")


@dataclass(frozen=True)
class Sandbox:
    """A throwaway HOME whose global git config, `uv`, scanner and system step are stubs; nothing reaches the operator's own or sudo."""

    home: Path
    global_git_config: Path
    uv_calls: Path
    system: FakeSystem

    @property
    def layout(self) -> Layout:
        return Layout(self.home)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    home = tmp_path / "home"
    home.mkdir()
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "uv", STUB_UV)
    _write_executable(tmp_path / "stub-quality", STUB_QUALITY)
    global_git_config = tmp_path / "global-gitconfig"
    global_git_config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
    uv_calls = tmp_path / "uv-calls"
    unit_directory = tmp_path / "checkout-units"
    unit_directory.mkdir()
    for name, value in {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": str(global_git_config),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "STUB_QUALITY": str(tmp_path / "stub-quality"),
        "STUB_UV_LOG": str(uv_calls),
        "STUB_RECEIPT_GIT": RECORDED_RECEIPT_GIT,
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("STUB_UV_FAIL", "STUB_ENGINES_FAIL", "STUB_SCAN_EXIT", "HOME_GIT_HOOK_CHAINED"):
        monkeypatch.delenv(name, raising=False)
    # A real ~/git/edge in the sandbox home would be a repository the hook check and tidy report on.
    monkeypatch.setattr(cli, "edge_units_directory", lambda _layout: unit_directory)
    system = FakeSystem(FakePlaybook())
    monkeypatch.setattr(system_step, "TERMINAL_BOUNDARIES", system.boundaries)
    return Sandbox(home, global_git_config, uv_calls, system)
