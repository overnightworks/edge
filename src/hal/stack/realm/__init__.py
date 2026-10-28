"""The realms as Keycloak holds them: the observer's inventories and the lines that name what
changed between two of them.

`observe.py` runs inside the `realm-observer` compose service and stays standard library only;
this side compares two of its inventories and prints the changes, every name and value escaped
by `hal.output.printable`, so a console name holding ESC or a newline stays one line.
"""

import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hal.output import printable
from hal.stack.realm.observe import Entity, Inventory

OBSERVER_SERVICE = "realm-observer"
DIGEST_KEY_BYTES = 32


def new_digest_key() -> str:
    """The per-run HMAC key, in hex, that the host writes to the observer's stdin: the same key
    for the two observes it compares, a new one for the next run."""
    return secrets.token_hex(DIGEST_KEY_BYTES)


class Kind(StrEnum):
    ADDITION = "addition"
    CHANGE = "change"
    REMOVAL = "removal"


@dataclass(frozen=True)
class Change:
    """One addition, change or removal. A change of a digested key carries no value."""

    realm: str
    kind: Kind
    type: str
    identity: str
    field: str | None = None
    old: Any = None
    new: Any = None
    hidden: bool = False
    members: int | None = None


def diff(before: Inventory, after: Inventory) -> list[Change]:
    """Compares two inventories object by object, by identity, never by UUID."""
    changes: list[Change] = []
    for realm in sorted(before.keys() | after.keys()):
        old_realm, new_realm = before.get(realm, {}), after.get(realm, {})
        for kind in sorted(old_realm.keys() | new_realm.keys()):
            old_objects, new_objects = old_realm.get(kind, {}), new_realm.get(kind, {})
            for identity in sorted(old_objects.keys() | new_objects.keys()):
                old, new = old_objects.get(identity), new_objects.get(identity)
                if old is None:
                    members = new["fields"].get("members")
                    changes.append(Change(realm, Kind.ADDITION, kind, identity, members=members))
                elif new is None:
                    members = old["fields"].get("members")
                    changes.append(Change(realm, Kind.REMOVAL, kind, identity, members=members))
                else:
                    changes += field_changes(realm, kind, identity, old, new)
    return changes


def field_changes(realm: str, kind: str, identity: str, old: Entity, new: Entity) -> list[Change]:
    changes = [
        Change(realm, Kind.CHANGE, kind, identity, name, old["fields"].get(name), value)
        for name in sorted(old["fields"].keys() | new["fields"].keys())
        if (value := new["fields"].get(name)) != old["fields"].get(name)
    ]
    changes += [
        Change(realm, Kind.CHANGE, kind, identity, name, hidden=True)
        for name in sorted(old["digests"].keys() | new["digests"].keys())
        if old["digests"].get(name) != new["digests"].get(name)
    ]
    return changes


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
