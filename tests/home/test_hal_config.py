"""The settings file's loader, called directly: defaults, known names and the records it builds."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from conftest import write_hal_config

from hal import config as hal_config
from home_tools.check import CheckSettings


def test_a_missing_file_gives_the_defaults(tmp_path: Path) -> None:
    assert hal_config.load(tmp_path) == hal_config.Settings(check=CheckSettings())


def test_an_empty_file_gives_the_defaults(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "")

    assert hal_config.load(tmp_path) == hal_config.Settings()


def test_every_setting_of_the_check_is_a_known_key() -> None:
    assert {
        field.name for field in dataclasses.fields(CheckSettings)
    } == hal_config.KNOWN_CHECK_KEYS


def test_a_check_key_reaches_the_record(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "[check]\nnotify = false\n")

    assert hal_config.load(tmp_path).check == CheckSettings(notify=False)


def test_a_non_table_check_is_refused(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "check = 1\n")

    with pytest.raises(hal_config.ConfigInvalid, match=r"\[check\] must be a table"):
        hal_config.load(tmp_path)
