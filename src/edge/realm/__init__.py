"""The realms as Keycloak holds them: the observer's inventories and the lines that name what
changed between two of them.

`observe.py` runs inside the `realm-observer` compose service and stays standard library only;
this side prints its changes, every name and value escaped by `compose.printable`, so a console
name holding ESC or a newline stays one line.
"""

import json
import secrets
from typing import Any

from edge.compose import printable
from edge.realm.observe import Change, Inventory, Kind, diff

SERVICE = "realm-observer"
DIGEST_KEY_BYTES = 32


def new_digest_key() -> str:
    """The per-run HMAC key, in hex, that the host writes to the observer's stdin: the same key
    for the two observes it compares, a new one for the next run."""
    return secrets.token_hex(DIGEST_KEY_BYTES)


def change_lines(before: Inventory, after: Inventory) -> list[str]:
    """One line per addition, change and removal, the realm first."""
    return [line(change) for change in diff(before, after)]


def line(change: Change) -> str:
    subject = f"{printable(change.realm)}: {change.kind} {change.type} {printable(change.identity)}"
    if change.kind is not Kind.CHANGE:
        return subject + members(change.members)
    if change.hidden:
        return f"{subject} {printable(change.field or '')}: changed"
    return f"{subject} {printable(change.field or '')}: {shown(change.old)} → {shown(change.new)}"


def members(count: int | None) -> str:
    if count is None:
        return ""
    return f" ({count} member)" if count == 1 else f" ({count} members)"


def shown(value: Any) -> str:
    return printable(json.dumps(value, ensure_ascii=False))
