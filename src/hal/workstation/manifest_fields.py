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


def https_link(path: Path, where: str, raw: Mapping[str, object], key: str) -> str:
    value = string(path, where, raw, key)
    if not value.startswith("https://"):
        raise fail(path, where, f"{key} must be an https:// link")
    return value


def require_key_of_kind(path: Path, where: str, raw: Mapping[str, object], key: str, is_owner_kind: bool, owner_kind: str) -> None:
    """A key that belongs to every entry of one kind and to no other."""
    if is_owner_kind != (key in raw):
        raise fail(path, where, f"{key} belongs to every {owner_kind} entry and to no other kind")
