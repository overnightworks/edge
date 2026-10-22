"""The legal unit (#285): the repository's real compose.yaml, fleet.yaml and legal/ tree."""

from pathlib import Path

import pytest
import yaml

from edge import catalog, compose

ROOT = Path(__file__).resolve().parents[1]
FONTS = "./keycloak/themes/hallucinai/login/resources/fonts"


@pytest.fixture(scope="module")
def document() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


@pytest.fixture(scope="module")
def legal(document) -> dict:
    return document["services"]["legal"]


def test_the_unit_is_hardened_like_the_stacks_other_services(legal):
    assert legal["read_only"] is True
    assert legal["cap_drop"] == ["ALL"]
    assert legal["security_opt"] == ["no-new-privileges:true"]
    assert legal["tmpfs"] == ["/tmp"]
    assert legal["profiles"] == ["legal"]


def test_the_image_is_nginx_unprivileged_pinned_by_digest(legal):
    assert legal["image"].startswith("nginxinc/nginx-unprivileged:")
    assert "-alpine-slim@sha256:" in legal["image"]


def test_the_unit_has_no_route_out_and_no_published_port(document, legal):
    assert legal["networks"] == ["legal"]
    assert document["networks"]["legal"] == {"internal": True}
    assert "ports" not in legal


def test_every_bind_is_read_only_and_the_fonts_are_the_login_themes_copy(legal):
    assert sorted(legal["volumes"]) == sorted(
        [
            "./legal/nginx:/etc/nginx/conf.d:ro",
            "./legal/pages:/srv/pages:ro",
            "./legal/assets:/srv/assets:ro",
            f"{FONTS}:/srv/fonts:ro",
        ]
    )
    assert all((ROOT / bind.split(":")[0]).is_dir() for bind in legal["volumes"])


def test_traefik_routes_the_zone_apex_to_port_8080_on_the_edge_entrypoint(legal):
    labels = legal["labels"]
    assert labels["traefik.enable"] == "true"
    assert labels["edge.name"] == "legal"
    assert labels["traefik.http.services.legal.loadbalancer.server.port"] == "8080"
    assert labels["traefik.http.routers.legal.entrypoints"] == "edge"
    assert labels["traefik.http.routers.legal.service"] == "legal"
    assert labels["traefik.http.routers.legal.rule"] == "Host(`${EDGE_DOMAIN}`)"


def test_the_catalog_runs_the_unit_and_gives_it_no_public_host():
    fleet = catalog.read(ROOT / "fleet.yaml")
    profiles = compose.profile_services(yaml.safe_load((ROOT / "compose.yaml").read_text()))
    (unit,) = [one for one in catalog.load(ROOT, profiles) if one.key == "legal"]
    assert unit.enabled
    assert "hosts" not in fleet["services"]["legal"]


def test_the_security_headers_stand_at_server_level_and_no_location_overrides_them():
    config = (ROOT / "legal/nginx/default.conf").read_text()
    server_level, *locations = config.split("location ")
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Cache-Control",
    ):
        assert f"add_header {header} " in server_level
    # nginx drops every inherited add_header in a location that sets its own.
    assert not any("add_header" in location for location in locations)


def test_every_font_the_stylesheet_names_exists_in_the_mounted_directory():
    stylesheet = (ROOT / "legal/assets/legal.css").read_text()
    names = [part.split('"')[0] for part in stylesheet.split('url("fonts/')[1:]]
    assert names
    assert all((ROOT / FONTS / name).is_file() for name in names)
