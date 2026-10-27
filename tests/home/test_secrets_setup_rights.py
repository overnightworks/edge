import pytest

from home_tools.secrets_setup import github_rights_prose


@pytest.mark.parametrize(
    ("scope", "prose"),
    [
        (("metadata:read",), "Metadata read"),
        (("contents:read", "contents:write"), "Contents read and write"),
        (
            ("workflows:write", "metadata:read", "workflows:read"),
            "Workflows write and read, Metadata read",
        ),
        (
            ("organization_secrets:admin", "metadata:read"),
            "Organization secrets admin, Metadata read",
        ),
    ],
)
def test_github_rights_prose_names_each_permission_once_with_its_levels(scope, prose):
    assert github_rights_prose(scope) == prose
