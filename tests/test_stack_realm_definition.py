"""The digest of config-cli's resolved service (#691): what it covers and what it ignores."""

import copy
from collections.abc import Callable
from pathlib import Path

import pytest

from hal.stack.realm.reconcile import definition_digest


def service(root: Path) -> dict:
    """A resolved service: two binds inside the checkout and one outside it."""
    return {
        "image": "config-cli:one",
        "environment": {"MODE": "one"},
        "entrypoint": ["/entry"],
        "volumes": [
            {"type": "bind", "source": str(root / "keycloak/entry.sh"), "target": "/entry"},
            {"type": "bind", "source": str(root / "keycloak"), "target": "/files"},
            {"type": "bind", "source": "/outside/source", "target": "/outside"},
        ],
        "secrets": [{"source": "client", "target": "CLIENT"}],
        "networks": {"keycloak": None},
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda resolved: resolved.update(image="config-cli:two"),
        lambda resolved: resolved["environment"].update(MODE="two"),
        lambda resolved: resolved.update(entrypoint=["/other"]),
        lambda resolved: resolved["volumes"][0].update(target="/moved"),
        lambda resolved: resolved["volumes"][2].update(source="/outside/elsewhere"),
        lambda resolved: resolved["secrets"].append({"source": "another", "target": "OTHER"}),
        lambda resolved: resolved["networks"].update(another=None),
        lambda resolved: resolved.update(a_field_added_later=True),
    ],
    ids=[
        "image",
        "environment",
        "entrypoint",
        "bind target",
        "outside source",
        "secret",
        "network",
        "added field",
    ],
)
def test_each_changed_field_of_the_service_changes_the_digest(
    tmp_path: Path, change: Callable[[dict], None]
) -> None:
    resolved = service(tmp_path)
    before = definition_digest(resolved, tmp_path)

    change(resolved)

    assert definition_digest(resolved, tmp_path) != before


def test_the_same_service_under_two_checkout_roots_has_one_digest(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"

    assert definition_digest(service(first), first) == definition_digest(service(second), second)


def test_a_bind_source_inside_the_checkout_is_told_from_one_outside_it(tmp_path: Path) -> None:
    inside, outside = service(tmp_path), service(tmp_path)
    outside["volumes"][0]["source"] = str(tmp_path.parent / "keycloak/entry.sh")

    assert definition_digest(inside, tmp_path) != definition_digest(outside, tmp_path)


def test_taking_the_digest_leaves_the_resolved_service_as_it_was(tmp_path: Path) -> None:
    resolved = service(tmp_path)
    before = copy.deepcopy(resolved)

    definition_digest(resolved, tmp_path)

    assert resolved == before
