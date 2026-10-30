"""The settings file's loader, called directly: defaults, known names and the records it builds."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from workstation_support import write_hal_config

from hal import config as hal_config
from hal.workstation.alarm import AlarmSettings
from hal.workstation.check import CheckSettings


def test_an_empty_file_gives_the_defaults(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "")

    assert hal_config.load(tmp_path) == hal_config.Settings()


def test_every_setting_of_the_check_is_a_known_key() -> None:
    assert {
        field.name for field in dataclasses.fields(CheckSettings)
    } == hal_config.KNOWN_CHECK_KEYS


def test_an_alarm_table_sets_the_interval_and_the_runs_in_a_row(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "[alarm]\ninterval = 15\nruns = 3\n")

    assert hal_config.load(tmp_path).alarm == AlarmSettings(interval=15, runs=3)


def test_a_missing_alarm_table_gives_five_minutes_and_two_runs(tmp_path: Path) -> None:
    write_hal_config(tmp_path, "[check]\nnotify = false\n")

    assert hal_config.load(tmp_path).alarm == AlarmSettings(interval=5, runs=2)


def test_every_setting_of_the_alarm_is_a_known_key() -> None:
    assert {
        field.name for field in dataclasses.fields(AlarmSettings)
    } == hal_config.KNOWN_ALARM_KEYS


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[alarm]\nspeed = 3\n", "unknown key(s) or table(s): speed"),
        ("[alarm]\ninterval = 2.5\n", "interval must be a whole number of at least 1, got 2.5"),
        ("[alarm]\nruns = false\n", "runs must be a whole number of at least 1, got False"),
        ("[alarm]\ninterval = 0\n", "interval must be a whole number of at least 1, got 0"),
        ("alarm = 5\n", "[alarm] must be a table"),
        ("[check]\nnotify = 1\n", "notify must be true or false, got 1"),
    ],
)
def test_a_wrong_alarm_or_check_setting_fails_loud(tmp_path: Path, text: str, reason: str) -> None:
    config_file = write_hal_config(tmp_path, text)

    with pytest.raises(hal_config.ConfigInvalid) as raised:
        hal_config.load(tmp_path)

    assert (raised.value.path, raised.value.reason) == (config_file, reason)
