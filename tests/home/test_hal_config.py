"""The settings file's loader, called directly: defaults, known names and the records it builds."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from conftest import write_hal_config

from hal import config as hal_config
from hal.workstation.check import CheckSettings


def test_an_empty_file_gives_the_defaults(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "")

    assert hal_config.load(tmp_path) == hal_config.Settings()


def test_every_setting_of_the_check_is_a_known_key() -> None:
    assert {
        field.name for field in dataclasses.fields(CheckSettings)
    } == hal_config.KNOWN_CHECK_KEYS
