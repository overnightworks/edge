from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from home_tools import secrets_view
from home_tools.layout import Layout


TUNNEL_CREDENTIAL = PurePosixPath(".secrets/cloudflared/{tunnel_id}.json")


@pytest.mark.parametrize(
    ("config_text", "credential_exists", "matched"),
    [
        ("tunnel: 0000-test-tunnel\ncredentials-file: /etc/cloudflared/x.json\n", True, [".secrets/cloudflared/0000-test-tunnel.json"]),
        ("tunnel: 0000-test-tunnel\n", False, []),
        ("credentials-file: /etc/cloudflared/x.json\n", True, None),
        ("tunnel: [not, an, id]\n", True, None),
        ("tunnel: ../escape\n", True, None),
        (": : not yaml [", True, None),
        (None, True, None),
    ],
    ids=["present", "missing", "no-tunnel-key", "tunnel-not-a-string", "tunnel-not-a-file-name", "unparseable", "no-config"],
)
def test_the_tunnel_credential_is_found_by_the_id_the_machines_tunnel_config_names(
    tmp_path: Path, stand_in_tunnel_config: Path, config_text: str | None, credential_exists: bool, matched: list[str] | None
) -> None:
    if config_text is None:
        stand_in_tunnel_config.unlink()
    else:
        stand_in_tunnel_config.write_text(config_text)
    if credential_exists:
        credentials = tmp_path / ".secrets" / "cloudflared"
        credentials.mkdir(parents=True)
        (credentials / "0000-test-tunnel.json").write_text("x")

    facts = secrets_view.stat_matches(Layout(tmp_path), TUNNEL_CREDENTIAL)

    assert (None if facts is None else [str(fact.relative) for fact in facts]) == matched


def test_an_unreadable_tunnel_config_leaves_the_credential_unknown(tmp_path: Path, stand_in_tunnel_config: Path) -> None:
    stand_in_tunnel_config.unlink()
    stand_in_tunnel_config.mkdir()

    assert secrets_view.stat_matches(Layout(tmp_path), TUNNEL_CREDENTIAL) is None
