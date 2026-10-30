"""hal's one settings file, `~/.config/hal/config.toml`: a table per timer job that has settings.

A missing file means defaults. hal only reads it; nothing here writes, moves or deletes it."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hal.workstation.alarm import AlarmSettings
from hal.workstation.check import CheckSettings

CONFIG_PATH_IN_HOME = Path(".config") / "hal" / "config.toml"
CHECK_TABLE = "check"
CHECK_KEY_TYPES: Mapping[str, type] = {"notify": bool}
KNOWN_CHECK_KEYS = frozenset(CHECK_KEY_TYPES)
ALARM_TABLE = "alarm"
ALARM_KEY_TYPES: Mapping[str, type] = {"interval": int, "runs": int}
KNOWN_ALARM_KEYS = frozenset(ALARM_KEY_TYPES)
EXPECTED_OF_TYPE: Mapping[type, str] = {bool: "true or false", int: "a whole number of at least 1"}


class ConfigInvalid(Exception):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Settings:
    check: CheckSettings = field(default_factory=CheckSettings)
    alarm: AlarmSettings = field(default_factory=AlarmSettings)


def config_path(home: Path) -> Path:
    return home / CONFIG_PATH_IN_HOME


def _read(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigInvalid(path, str(error)) from error
    except UnicodeDecodeError as error:
        raise ConfigInvalid(path, "not UTF-8") from error
    except OSError as error:
        raise ConfigInvalid(path, error.strerror or type(error).__name__) from error


def _refuse_unknown(path: Path, names: set[str], known: frozenset[str]) -> None:
    unknown = sorted(names - known)
    if unknown:
        raise ConfigInvalid(path, f"unknown key(s) or table(s): {', '.join(unknown)}")


def _is_of_type(value: object, expected: type) -> bool:
    if expected is int:
        return type(value) is int and value >= 1
    return isinstance(value, expected)


def _table_values(
    path: Path, name: str, table: object, key_types: Mapping[str, type]
) -> dict[str, object]:
    if not isinstance(table, dict):
        raise ConfigInvalid(path, f"[{name}] must be a table")
    _refuse_unknown(path, set(table), frozenset(key_types))
    for key, value in table.items():
        if not _is_of_type(value, key_types[key]):
            raise ConfigInvalid(
                path, f"{key} must be {EXPECTED_OF_TYPE[key_types[key]]}, got {value!r}"
            )
    return table


def load(home: Path) -> Settings:
    path = config_path(home)
    raw = _read(path)
    _refuse_unknown(path, set(raw), frozenset({CHECK_TABLE, ALARM_TABLE}))
    return Settings(
        check=CheckSettings(
            **_table_values(path, CHECK_TABLE, raw.get(CHECK_TABLE, {}), CHECK_KEY_TYPES)
        ),
        alarm=AlarmSettings(
            **_table_values(path, ALARM_TABLE, raw.get(ALARM_TABLE, {}), ALARM_KEY_TYPES)
        ),
    )
