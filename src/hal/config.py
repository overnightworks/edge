"""hal's one settings file, `~/.config/hal/config.toml`: a table per timer job that has settings.

A missing file means defaults. hal only reads it; nothing here writes, moves or deletes it."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hal.workstation.check import CheckSettings

CONFIG_PATH_IN_HOME = Path(".config") / "hal" / "config.toml"
CHECK_TABLE = "check"
CHECK_KEY_TYPES: Mapping[str, type] = {"notify": bool}
KNOWN_CHECK_KEYS = frozenset(CHECK_KEY_TYPES)


class ConfigInvalid(Exception):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Settings:
    check: CheckSettings = field(default_factory=CheckSettings)


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


def _check_settings(path: Path, table: object) -> CheckSettings:
    if not isinstance(table, dict):
        raise ConfigInvalid(path, f"[{CHECK_TABLE}] must be a table")
    _refuse_unknown(path, set(table), KNOWN_CHECK_KEYS)
    for key in set(table):
        if not isinstance(table[key], CHECK_KEY_TYPES[key]):
            raise ConfigInvalid(path, f"{key} must be true or false, got {table[key]!r}")
    return CheckSettings(**table)


def load(home: Path) -> Settings:
    path = config_path(home)
    raw = _read(path)
    _refuse_unknown(path, set(raw), frozenset({CHECK_TABLE}))
    if CHECK_TABLE not in raw:
        return Settings()
    return Settings(check=_check_settings(path, raw[CHECK_TABLE]))
