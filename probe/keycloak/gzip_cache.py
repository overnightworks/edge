"""Keycloak's gzip copies of theme files sit on a tmpfs owned by its user: a bare tmpfs comes back
root-owned after a restart, and Keycloak then answers 404 for every gzip theme file."""

from __future__ import annotations

import gzip
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

CACHE_FOLDER = "/opt/keycloak/data/tmp"
REALM = "hallucinai"
ACCOUNT_THEME_FILE = Path(__file__).resolve().parents[2] / (
    f"keycloak/themes/{REALM}/account/resources/content.json"
)


def prove_survives_a_restart(
    probe: Any, expect: Callable[[bool, str], None], wait_until_ready: Callable[[str], None]
) -> None:
    """Restarts keycloak, checks the folder's owner and a gzip request for the account theme's
    content.json, and records proof 1a on `probe`."""
    restarted = probe.stack.compose("restart", "keycloak")
    expect(restarted.returncode == 0, f"restart keycloak failed:\n{restarted.stderr[-1500:]}")
    wait_until_ready(probe.stack.admin_url)
    owner = probe.stack.compose("exec", "-T", "keycloak", "stat", "-c", "%u %m", CACHE_FOLDER)
    expect(
        owner.stdout.split() == ["1000", CACHE_FOLDER],
        f"the gzip cache folder is not a tmpfs owned by uid 1000: {owner.stdout!r}",
    )
    account_page = probe.through_tunnel("GET", f"/realms/{REALM}/account/")
    version = re.search(r"/resources/([a-z0-9]{5})/account/", account_page.text)
    expect(version is not None, "the account console links no versioned resource")
    served = probe.through_tunnel(
        "GET",
        f"/resources/{version.group(1)}/account/{REALM}/content.json",
        headers={"Accept-Encoding": "gzip"},
    )
    body = served.body
    if served.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    expect(
        served.status == 200 and body == ACCOUNT_THEME_FILE.read_bytes(),
        f"gzip content.json after a restart answered {served.status}, not the theme's file",
    )
    expect(b'"groups"' not in body, "the gzip answer holds the parent theme's groups entry")
    probe.held(
        "1a",
        "after a restart the gzip cache folder is a tmpfs owned by uid 1000 and a gzip "
        "request for the account theme's content.json answers 200 with the theme's file",
    )
