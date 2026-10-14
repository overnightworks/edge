from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

DAY_SECONDS = 24 * 60 * 60

# Path-shaped tokens make these entries known, so the state `home` writes never becomes a finding itself.
HOME_README = "`.local/state`, `.config/home` and `git/` belong here.\n"


@pytest.fixture
def clean_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "README.md").write_text(HOME_README)
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
        return [call[1] for call in self.calls if call[0] == "notify-send"]


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()
