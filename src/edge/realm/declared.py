"""The identities `keycloak/realm.yaml` and `keycloak/master.yaml` declare, for the kinds the
operator creates: pure, text in and names out, never a value (the files hold `$(env:…)`
placeholders and a client secret reference).

A kind is named as the observer names it (`edge.realm.observe`), so a declared identity compares
with an observed one directly.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from edge.realm.observe import REALM_ROLE

GROUP = "group"
CLIENT = "client"
CLIENT_SCOPE = "client scope"
FLOW = "flow"
IDENTITY_PROVIDER = "identity provider"

type Identities = dict[str, frozenset[str]]  # observer kind -> identities


@dataclass(frozen=True)
class DeclaredRealm:
    """One realm file's name and what it declares."""

    realm: str
    identities: Identities


class DeclaredError(Exception):
    """A realm file is no YAML mapping with a `realm`."""


def declared_realm(text: str) -> DeclaredRealm:
    """The identities one realm file declares."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise DeclaredError("a realm file is no YAML") from error
    if not isinstance(document, dict) or "realm" not in document:
        raise DeclaredError("a realm file names no realm")
    return DeclaredRealm(
        document["realm"],
        {
            GROUP: frozenset(_group_paths(document.get("groups", ()), "")),
            CLIENT: _names(document.get("clients", ()), "clientId"),
            CLIENT_SCOPE: _names(document.get("clientScopes", ()), "name"),
            REALM_ROLE: _names((document.get("roles") or {}).get("realm", ()), "name"),
            FLOW: _names(document.get("authenticationFlows", ()), "alias"),
            IDENTITY_PROVIDER: _names(document.get("identityProviders", ()), "alias"),
        },
    )


def _names(objects: Iterable[Mapping[str, Any]], key: str) -> frozenset[str]:
    return frozenset(item[key] for item in objects)


def _group_paths(groups: Iterable[Mapping[str, Any]], parent: str) -> Iterable[str]:
    for group in groups:
        path = f"{parent}/{group['name']}"
        yield path
        yield from _group_paths(group.get("subGroups", ()), path)
