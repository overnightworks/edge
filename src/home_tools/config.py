"""The one configuration file of `home`: a table per subcommand that has settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass

from home_tools.layout import Layout


@dataclass(frozen=True)
class Config:
    notify_enabled: bool = True


def _require_known_keys(where: str, raw: dict[str, object], known_keys: set[str]) -> None:
    unknown_keys = sorted(set(raw) - known_keys)
    if unknown_keys:
        raise ValueError(f"unknown config key(s) in {where}: {', '.join(unknown_keys)}")


def _load_notify_enabled(raw: dict[str, object]) -> bool:
    _require_known_keys("[notify]", raw, {"enabled"})
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled must be true or false, got {enabled!r}")
    return enabled


def _require_table(name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table, got {value!r}")
    return value


def load_config(layout: Layout) -> Config:
    """Defaults apply when the file is absent; a `[tidy]` table, an unknown table or a bad value is a config error."""
    if not layout.config_file.is_file():
        return Config()
    with layout.config_file.open("rb") as handle:
        raw = tomllib.load(handle)
    if "tidy" in raw:
        raise ValueError(f"the [tidy] table is no longer read; delete it from {layout.config_file}")
    _require_known_keys(str(layout.config_file), raw, {"notify"})
    return Config(notify_enabled=_load_notify_enabled(_require_table("notify", raw.get("notify", {}))))
