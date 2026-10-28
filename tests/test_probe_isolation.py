"""The probes' network isolation (#346 P26): what probe/edge_copy.py moves and refuses."""

import sys
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "probe"))
sys.path.insert(0, str(ROOT / "src"))

import edge_copy  # noqa: E402
from git_support import committed_main, plain_git  # noqa: E402

from hal.stack import catalog  # noqa: E402

PROJECT = "edge-some-probe"


def fixed_networks(text: str) -> dict[str, str]:
    networks = yaml.safe_load(text)["networks"]
    return {key: network["name"] for key, network in networks.items() if "name" in network}


def test_every_fixed_network_of_compose_moves_into_the_project():
    compose = (ROOT / "compose.yaml").read_text()

    moved = fixed_networks(edge_copy.namespaced_compose(compose, PROJECT))

    assert moved
    assert all(name.startswith(f"{PROJECT}_") for name in moved.values())
    assert moved.keys() == fixed_networks(compose).keys()


def test_a_compose_text_with_no_fixed_network_is_refused():
    with pytest.raises(edge_copy.ProbeFailed, match="no fixed hal_ network"):
        edge_copy.namespaced_compose("networks:\n  mcp:\n    internal: true\n", PROJECT)


def test_a_hal_network_name_the_rewrite_cannot_match_is_refused():
    text = "networks:\n  a:\n    name: hal_a\n  b:\n    name: hal_b # live\n"

    with pytest.raises(edge_copy.ProbeFailed, match="remains after the rewrite"):
        edge_copy.namespaced_compose(text, PROJECT)


def test_a_resolved_network_outside_the_project_fails_the_guard():
    resolved = {"networks": {"a": {"name": f"{PROJECT}_a"}, "b": {"name": "hal_agent_songmaker"}}}

    with pytest.raises(edge_copy.ProbeFailed, match="hal_agent_songmaker"):
        edge_copy.expect_project_networks(resolved, PROJECT)


def test_a_resolved_configuration_inside_the_project_passes_the_guard():
    edge_copy.expect_project_networks({"networks": {"a": {"name": f"{PROJECT}_a"}}}, PROJECT)


OCCUPIED = ["172.31.250.0/24", "172.31.251.0/24", "10.0.0.0/8"]
CANDIDATES = ["172.31.250.0/24", "172.31.251.0/24", "172.31.249.0/24"]


def test_the_first_candidate_that_overlaps_no_occupied_subnet_is_free():
    assert str(edge_copy.free_subnet(CANDIDATES, OCCUPIED)) == "172.31.249.0/24"


def test_a_candidate_inside_a_wider_occupied_subnet_is_taken():
    with pytest.raises(edge_copy.ProbeFailed, match="no free /24"):
        edge_copy.free_subnet(CANDIDATES, ["172.31.0.0/16"])


MIXED_CATALOG = dedent(
    """\
    # comment before
    cloudflare:
      zone: example.invalid

    services:
      blockapp:
        repo: {default_branch: main}
        run:
          enabled: true
        hosts:
          - {name: blockapp, exposure: public, reason: r, ready: /health}

      # a comment between entries
      flowapp: {repo: {default_branch: master}, run: {enabled: false}}
      hostsonly:
        hosts:
          - {name: only, exposure: public, reason: r}
      library:
        repo: {default_branch: main}
      step-ca:
        run:
          enabled: true
      keycloak:
        run:
          enabled: true
          setup: keycloak/go-live
    """
)
WITHOUT_APPS = dedent(
    """\
    # comment before
    cloudflare:
      zone: example.invalid

    services:

      # a comment between entries
      hostsonly:
        hosts:
          - {name: only, exposure: public, reason: r}
      library:
        repo: {default_branch: main}
      step-ca:
        run:
          enabled: true
      keycloak:
        run:
          enabled: true
          setup: keycloak/go-live
    """
)


def unit_keys(text: str) -> list[str]:
    return list(yaml.safe_load(text)["services"])


def non_app_keys(text: str) -> list[str]:
    services = yaml.safe_load(text)["services"]
    return [key for key, entry in services.items() if not catalog.is_app(entry)]


def test_without_inherited_apps_leaves_the_repositorys_catalog_without_an_app():
    text = (ROOT / "fleet.yaml").read_text()
    original = yaml.safe_load(text)["services"]

    filtered = edge_copy.without_inherited_apps(text)

    kept = yaml.safe_load(filtered)["services"]
    assert kept == {key: entry for key, entry in original.items() if not catalog.is_app(entry)}
    assert {"hopin", "presentator"} <= set(original)
    assert not any(catalog.is_app(entry) for entry in kept.values())
    original_lines = iter(text.splitlines())
    assert all(line in original_lines for line in filtered.splitlines())  # in order
    lines = text.splitlines()
    app_lines = 0
    for key in (key for key, entry in original.items() if catalog.is_app(entry)):
        start = lines.index(f"  {key}:")
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].startswith("  ") and not lines[i].startswith("   ")
            ),
            len(lines),
        )
        app_lines += end - start
    assert len(lines) - len(filtered.splitlines()) == app_lines  # every app's entry, nothing else


def test_both_styles_of_app_go_and_hosts_only_and_library_entries_stay():
    assert edge_copy.without_inherited_apps(MIXED_CATALOG) == WITHOUT_APPS


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("services: [unclosed\n", "does not parse"),
        ("cloudflare:\n  zone: example.invalid\n", "no services mapping"),
        ("services: [a, b]\n", "no services mapping"),
        (
            "services:\n  run: {repo: {default_branch: main}, run: {enabled: false}}\n"
            "  step-ca: {\n  run: {enabled: true}\n  }\n",
            "differs",
        ),
        (
            "services: {\n  enabled: {repo: {default_branch: main}, run: {enabled: false}},\n"
            "  step-ca: {run: {\n  <<: {enabled: true},\n  enabled: false,\n  setup: null\n"
            "  }}\n}\n",
            "differs",
        ),
    ],
)
def test_a_catalog_text_that_cannot_be_filtered_is_refused_with_its_reason(text, reason):
    with pytest.raises(edge_copy.ProbeFailed, match=reason):
        edge_copy.without_inherited_apps(text)


def test_filtering_the_filtered_text_changes_nothing():
    assert edge_copy.without_inherited_apps(WITHOUT_APPS) == WITHOUT_APPS


def a_repository(tmp_path: Path, fleet: str) -> Path:
    """A committed repository with `fleet`, one compose.yaml network and no dependencies."""
    tree = tmp_path / "repository"
    tree.mkdir()
    (tree / "fleet.yaml").write_text(fleet)
    (tree / "compose.yaml").write_text("networks:\n  mcp:\n    name: hal_mcp\n")
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0"\nrequires-python = ">=3.12"\n'
        "dependencies = []\n"
    )
    return committed_main(tree)


def a_copy(tmp_path: Path, repository: Path, project: str | None) -> tuple[Path, Path]:
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    home.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    copy = edge_copy.repository_copy(repository, home, scratch, project)
    return copy, scratch / edge_copy.REMOTE


def origin_fleet(origin: Path) -> str:
    return plain_git(origin, "show", "main:fleet.yaml").stdout


PROJECTS = pytest.mark.parametrize("project", [PROJECT, None])


@PROJECTS
def test_the_copy_and_its_origin_name_no_inherited_app(tmp_path, project):
    copy, origin = a_copy(tmp_path, a_repository(tmp_path, MIXED_CATALOG), project)

    for text in ((copy / "fleet.yaml").read_text(), origin_fleet(origin)):
        assert non_app_keys(text) == unit_keys(text)


@PROJECTS
def test_the_copy_keeps_every_non_app_line_so_both_switches_stand_once(tmp_path, project):
    copy, _ = a_copy(tmp_path, a_repository(tmp_path, MIXED_CATALOG), project)

    text = (copy / "fleet.yaml").read_text()

    assert text == WITHOUT_APPS
    assert text.count("  step-ca:\n    run:\n      enabled: true\n") == 1
    assert text.count("      enabled: true\n      setup: keycloak/go-live\n") == 1


@PROJECTS
def test_copying_twice_over_one_home_still_has_no_app_and_a_clean_pushed_main(tmp_path, project):
    repository = a_repository(tmp_path, MIXED_CATALOG)
    _, origin = a_copy(tmp_path, repository, project)

    second, _ = a_copy(tmp_path, repository, project)

    assert (second / "fleet.yaml").read_text() == WITHOUT_APPS
    assert origin_fleet(origin) == WITHOUT_APPS
    assert plain_git(second, "status", "--porcelain").stdout == ""
    assert (
        plain_git(second, "rev-parse", "main").stdout
        == plain_git(origin, "rev-parse", "main").stdout
    )


@PROJECTS
def test_an_unparsable_catalog_fails_the_copy_and_pushes_nothing(tmp_path, project):
    repository = a_repository(tmp_path, "services: [unclosed\n")

    with pytest.raises(edge_copy.ProbeFailed, match="does not parse"):
        a_copy(tmp_path, repository, project)

    origin = tmp_path / "scratch" / edge_copy.REMOTE
    assert not origin.exists() or plain_git(origin, "branch", "--list").stdout == ""


@PROJECTS
def test_the_network_rewrite_applies_with_a_project_and_never_without(tmp_path, project):
    repository = a_repository(tmp_path, MIXED_CATALOG)

    copy, _ = a_copy(tmp_path, repository, project)

    text = (copy / "compose.yaml").read_text()
    if project:
        assert fixed_networks(text) == {"mcp": f"{project}_mcp"}
    else:
        assert text == (repository / "compose.yaml").read_text()


@PROJECTS
def test_an_app_written_after_the_copy_and_published_stays(tmp_path, project):
    copy, origin = a_copy(tmp_path, a_repository(tmp_path, MIXED_CATALOG), project)
    fixture_app = "  fixtureapp:\n    repo: {default_branch: main}\n    run: {enabled: true}\n"
    with (copy / "fleet.yaml").open("a") as fleet:
        fleet.write(fixture_app)

    edge_copy.publish(copy, "a fixture app")

    declared = {"repo": {"default_branch": "main"}, "run": {"enabled": True}}
    for text in ((copy / "fleet.yaml").read_text(), origin_fleet(origin)):
        assert yaml.safe_load(text)["services"]["fixtureapp"] == declared
        assert catalog.is_app(yaml.safe_load(text)["services"]["fixtureapp"])
