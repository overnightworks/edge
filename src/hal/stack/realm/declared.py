"""The identities `keycloak/realm.yaml` and `keycloak/master.yaml` declare, for the kinds the
operator creates: pure, text in and names out, never a value (the files hold `$(env:…)`
placeholders and a client secret reference).

A kind is named as the observer names it (`hal.stack.realm.observe`), so a declared identity
compares with an observed one directly.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import yaml

from hal.stack.realm import observe

# The kinds compared, in the order they are named: each is the observer's own name for it.
KINDS = (
    observe.GROUP.name,
    observe.CLIENT.name,
    observe.CLIENT_SCOPE.name,
    observe.REALM_ROLE,
    observe.FLOW.name,
    observe.IDP.name,
)

type Identities = dict[str, frozenset[str]]  # observer kind -> identities


@dataclass(frozen=True)
class DeclaredRealm:
    """One realm file's name and what it declares."""

    realm: str
    identities: Identities


class DeclaredError(Exception):
    """A realm file is no YAML mapping with a `realm`, or declares a list in another shape."""


def declared_realm(text: str) -> DeclaredRealm:
    """The identities one realm file declares. An empty list (`groups:`) declares nothing."""
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as error:
        raise DeclaredError("a realm file is no YAML") from error
    if not isinstance(document, dict) or not isinstance(document.get("realm"), str):
        raise DeclaredError("a realm file names no realm")
    roles = _declared(document, "roles", dict, {})
    group, client, scope, role, flow, provider = KINDS
    return DeclaredRealm(
        document["realm"],
        {
            group: frozenset(_group_paths(_entries(document, "groups"), "")),
            client: _names(_entries(document, "clients"), "clientId"),
            scope: _names(_entries(document, "clientScopes"), "name"),
            role: _names(_entries(roles, "realm"), "name"),
            flow: _names(_entries(document, "authenticationFlows"), "alias"),
            provider: _names(_entries(document, "identityProviders"), "alias"),
        },
    )


def _entries(parent: dict, key: str) -> list[dict]:
    """The mappings listed under `key`; absent or null is none."""
    listed = _declared(parent, key, list, [])
    if not all(isinstance(entry, dict) for entry in listed):
        raise DeclaredError(f"a realm file's {key} are no list of mappings")
    return listed


def _declared[T](parent: dict, key: str, expected: type[T], none: T) -> T:
    """The value under `key` when it has the expected type; only absent or null declares none."""
    value = parent.get(key)
    if value is None:
        return none
    if not isinstance(value, expected):
        raise DeclaredError(f"a realm file's {key} are no {expected.__name__}")
    return value


def _name(entry: dict, key: str) -> str:
    name = entry.get(key)
    if not isinstance(name, str):
        raise DeclaredError(f"a realm file's entry has no {key}")
    return name


def _names(entries: list[dict], key: str) -> frozenset[str]:
    return frozenset(_name(entry, key) for entry in entries)


def _group_paths(groups: list[dict], parent: str) -> Iterable[str]:
    for group in groups:
        path = f"{parent}/{_name(group, 'name')}"
        yield path
        yield from _group_paths(_entries(group, "subGroups"), path)
