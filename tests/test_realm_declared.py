"""The identities the realm files declare (#248): names read from YAML text, never a value."""

from pathlib import Path

import pytest

from edge.realm import observe
from edge.realm.declared import KINDS, DeclaredError, declared_realm

REPOSITORY = Path(__file__).resolve().parents[1]

DECLARATION = """
realm: example
roles:
  realm:
    - name: operator
groups:
  - name: apps
    subGroups:
      - name: songmaker
        subGroups:
          - name: admin
  - name: ai
clientScopes:
  - name: groups
clients:
  - clientId: web
    secret: $(env:WEB_CLIENT_SECRET)
authenticationFlows:
  - alias: browser-mfa
identityProviders:
  - alias: github
"""


def test_each_kind_the_operator_creates_is_read_by_its_identity() -> None:
    declared = declared_realm(DECLARATION)

    assert declared.realm == "example"
    assert declared.identities == {
        observe.GROUP.name: {"/apps", "/apps/songmaker", "/apps/songmaker/admin", "/ai"},
        observe.CLIENT.name: {"web"},
        observe.CLIENT_SCOPE.name: {"groups"},
        observe.REALM_ROLE: {"operator"},
        observe.FLOW.name: {"browser-mfa"},
        observe.IDP.name: {"github"},
    }
    assert tuple(declared.identities) == KINDS


def test_a_file_that_declares_none_of_the_kinds_declares_empty_sets() -> None:
    assert not any(declared_realm("realm: bare\nroles: {}\n").identities.values())


def test_no_value_of_a_declared_object_is_among_the_identities() -> None:
    names = {name for kind in declared_realm(DECLARATION).identities.values() for name in kind}

    assert not [name for name in names if "WEB_CLIENT_SECRET" in name]


@pytest.mark.parametrize(
    "empty",
    [
        "groups:",
        "groups: []",
        "clients: null",
        "clientScopes:",
        "roles:",
        "roles: {realm: }",
        "authenticationFlows:",
        "identityProviders:",
        "groups:\n  - name: apps\n    subGroups:",
    ],
)
def test_a_list_whose_key_is_empty_declares_nothing_of_it(empty: str) -> None:
    declared = declared_realm(f"realm: bare\n{empty}\n")

    assert declared.realm == "bare"
    assert set(declared.identities) == set(KINDS)


@pytest.mark.parametrize(
    "text",
    [
        "realm: [unclosed\n",
        "- a list\n",
        "enabled: true\n",
        "",
        "realm: [a, b]\n",
        "realm: x\nclients:\n  - secret: s\n",
        "realm: x\nclients: {clientId: web}\n",
        "realm: x\nclients:\n  - web\n",
        "realm: x\ngroups:\n  - name: [a]\n",
        "realm: x\ngroups:\n  - name: a\n    subGroups: oops\n",
        "realm: x\nroles: [operator]\n",
        "realm: x\nroles:\n  realm: operator\n",
        "realm: x\nauthenticationFlows:\n  - description: no alias\n",
        "realm: x\nidentityProviders:\n  - alias: 5\n",
    ],
)
def test_a_file_that_is_no_yaml_mapping_with_a_realm_is_refused(text: str) -> None:
    with pytest.raises(DeclaredError):
        declared_realm(text)


@pytest.mark.parametrize(
    ("file", "realm", "kind", "identity"),
    [
        ("realm.yaml", "hallucinai", observe.GROUP.name, "/apps/songmaker/admin"),
        ("realm.yaml", "hallucinai", observe.CLIENT.name, "agent-service"),
        ("realm.yaml", "hallucinai", observe.REALM_ROLE, "agent-workload"),
        ("master.yaml", "master", observe.FLOW.name, "browser-mfa"),
        ("master.yaml", "master", observe.CLIENT.name, "config-cli"),
    ],
)
def test_the_checkouts_realm_files_declare_what_the_operator_made_there(
    file: str, realm: str, kind: str, identity: str
) -> None:
    declared = declared_realm((REPOSITORY / "keycloak" / file).read_text())

    assert declared.realm == realm
    assert identity in declared.identities[kind]
