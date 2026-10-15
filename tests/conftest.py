from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from home_tools import secrets_view
from home_tools.secrets_view import load_manifest
from home_tools.tidy_home import Layout

DAY_SECONDS = 24 * 60 * 60

# Path-shaped tokens make these entries known, so the state `home` writes never becomes a finding itself.
HOME_README = "`.local/state`, `.config/home`, `.secrets` and `git/` belong here.\n"

# Every planted secret file holds this; no output and no report may ever contain any of it.
FAKE_SECRET_VALUE = "FAKE-SECRET-VALUE ghp_0000notarealtoken glpat-0000notarealtoken"

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
def clean_home(tmp_path: Path, use_manifest: Callable[[str], Path]) -> Path:
    """A home with nothing to report: the README knows every entry and every secret of the test manifest is in place."""
    use_manifest(TEST_MANIFEST)
    home = tmp_path / "home"
    home.mkdir()
    (home / "README.md").write_text(HOME_README)
    plant_secrets(home)
    return home


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
    """Delivers desktop notifications, stands in for every other tool as missing, and remembers what was asked of it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.notify_delivers = True

    def __call__(self, argv: Sequence[str]) -> str | None:
        self.calls.append(list(argv))
        return "" if argv[0] == "notify-send" and self.notify_delivers else None

    @property
    def notifications(self) -> list[str]:
        """The summary line of every notification sent."""
        return [call[1] for call in self.calls if call[0] == "notify-send"]

    @property
    def notification_bodies(self) -> list[str]:
        return [call[2] for call in self.calls if call[0] == "notify-send"]


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


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
