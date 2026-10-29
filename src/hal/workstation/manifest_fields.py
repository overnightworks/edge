"""Reading one key of a secrets-manifest table, and the error that names what is wrong."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


class ManifestError(ValueError):
    """The manifest cannot be trusted; `hal secrets` exits 2 on it."""


def fail(path: Path, where: str, message: str) -> ManifestError:
    return ManifestError(f"{path}: {where}: {message}")


def string(path: Path, where: str, raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise fail(path, where, f"{key} must be a non-empty string")
    return value


def strings(path: Path, where: str, raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise fail(path, where, f"{key} must be a list of non-empty strings")
    return tuple(value)
