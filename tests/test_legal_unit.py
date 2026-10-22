"""The legal unit (#285): the repository's real compose.yaml, fleet.yaml and legal/ tree."""

import re
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


PAGES = ("impressum", "datenschutz", "nutzungsbedingungen")
TEXT_PAGES = PAGES[1:]


def read_page(name: str) -> str:
    return (ROOT / "legal/pages" / f"{name}.html").read_text()


@pytest.mark.parametrize("name", PAGES)
def test_each_page_file_exists_and_the_config_resolves_paths_through_try_files(name):
    config = (ROOT / "legal/nginx/default.conf").read_text()
    assert "try_files $uri.html =404;" in config
    assert (ROOT / "legal/pages" / f"{name}.html").is_file()


@pytest.mark.parametrize("name", PAGES)
def test_a_page_links_only_assets_the_unit_has_files_for(name):
    served = {f"/_rechtliches/{path.name}" for path in (ROOT / "legal/assets").iterdir()}
    served |= {f"/_rechtliches/fonts/{path.name}" for path in (ROOT / FONTS).iterdir()}
    linked = set(re.findall(r'(?:href|src)="(/_rechtliches/[^"]+)"', read_page(name)))
    assert linked
    assert linked <= served


@pytest.mark.parametrize("name", PAGES)
def test_a_page_carries_no_inline_style_and_no_script_the_csp_would_refuse(name):
    page = read_page(name)
    assert " style=" not in page
    assert "<style" not in page
    assert "<script" not in page


@pytest.mark.parametrize("name", TEXT_PAGES)
def test_every_table_of_contents_entry_targets_an_id_of_the_page(name):
    page = read_page(name)
    contents = page.split('<aside class="toc"')[1].split("</aside>")[0]
    targets = re.findall(r'href="#([^"]+)"', contents)
    assert "songmaker" in targets
    assert all(f'id="{target}"' in page for target in targets)


@pytest.mark.parametrize("name", TEXT_PAGES)
def test_the_songmaker_section_has_an_anchored_heading_and_subsection_ids(name):
    page = read_page(name)
    assert page.count('id="songmaker"') == 1
    assert 'id="songmaker-' in page


@pytest.mark.parametrize(
    "path",
    [f"legal/pages/{name}.html" for name in PAGES] + ["docs/design/legal-pages.html"],
)
@pytest.mark.parametrize(
    "mark",
    [
        "Zu prüfen",
        "zu prüfen",
        'class="check',
        'class="todo"',
        'class="slot"',
        "hopin",
        "presentator",
        "TODO",
    ],
)
def test_neither_a_page_nor_the_frozen_picture_carries_a_placeholder_or_a_review_mark(path, mark):
    assert mark not in (ROOT / path).read_text()
