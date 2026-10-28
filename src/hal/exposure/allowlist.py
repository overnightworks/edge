"""The allow-list of known listeners beyond loopback, read from `allow-list.toml`."""

import tomllib
from dataclasses import dataclass

from hal.exposure.exposure import Protocol
from hal.exposure.listeners import Listener

_ENTRY_KEYS = frozenset({"protocol", "port", "owner", "reason"})
_LISTENER_PROTOCOLS = (Protocol.TCP, Protocol.UDP)
_HIGHEST_PORT = 65535


class AllowListError(ValueError):
    """The allow-list cannot be trusted; the message names the offending entry."""


@dataclass(frozen=True)
class AllowEntry:
    protocol: Protocol
    port: int | None
    owner: str | None
    reason: str

    def permits(self, listener: Listener) -> bool:
        return (
            listener.protocol == self.protocol
            and self.port in (None, listener.port)
            and (self.owner is None or listener.is_owned_by(self.owner))
        )


def parse_allow_list(text: str) -> tuple[AllowEntry, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AllowListError(f"not valid TOML: {error}") from error
    if unknown := set(document) - {"allow"}:
        raise AllowListError(f"unknown top-level key {sorted(unknown)}")
    entries = document.get("allow", [])
    if not isinstance(entries, list):
        raise AllowListError("`allow` must be an array of tables ([[allow]])")
    return tuple(_parse_entry(number, entry) for number, entry in enumerate(entries, start=1))


def _parse_entry(number: int, entry: object) -> AllowEntry:
    if not isinstance(entry, dict):
        raise AllowListError(f"allow entry {number}: must be a table")
    label = f"allow entry {number} ({_describe(entry)})"
    if unknown := set(entry) - _ENTRY_KEYS:
        raise AllowListError(f"{label}: unknown key {sorted(unknown)}")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise AllowListError(f"{label}: missing reason")
    protocol = entry.get("protocol")
    if protocol not in _LISTENER_PROTOCOLS:
        raise AllowListError(f"{label}: protocol must be tcp or udp, not {protocol!r}")
    port = entry.get("port")
    if port is not None and not _is_port(port):
        raise AllowListError(f"{label}: bad port {port!r}, expected 1-{_HIGHEST_PORT}")
    owner = entry.get("owner")
    if owner is not None and (not isinstance(owner, str) or not owner):
        raise AllowListError(f"{label}: owner must be a non-empty string")
    if port is None and owner is None:
        raise AllowListError(f"{label}: needs a port, an owner, or both")
    return AllowEntry(protocol=Protocol(protocol), port=port, owner=owner, reason=reason)


def _is_port(value: object) -> bool:
    return type(value) is int and 1 <= value <= _HIGHEST_PORT


def _describe(entry: dict[str, object]) -> str:
    parts = [part for key in ("owner", "protocol", "port") if (part := str(entry.get(key, "")))]
    return " ".join(parts) or "empty"
