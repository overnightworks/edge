"""A throwaway HOME for tests that must not see the real one."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME and XDG_CONFIG_HOME: no global config but the one a test writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LC_ALL", "C")  # git's own lines in English, whatever the machine speaks
    return home
