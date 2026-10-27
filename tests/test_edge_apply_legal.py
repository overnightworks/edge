"""`edge apply --closed` with the legal step (#435): the three pages built from the repository's own
frame and sections on the scripted boundaries of `test_edge_apply`, in a real git repository (the
Stand of a page is a commit date)."""

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from git_support import committed_main
from test_edge_apply import (
    REPOSITORY,
    ZONE,
    ScriptedCompose,
    applied_core,  # noqa: F401 the fixture, requested by name below
    at_terminal,  # noqa: F401 the fixture, requested by name below
    core_files,  # noqa: F401 the fixture, requested by name below
    edit_traefik_yml,
    home,  # noqa: F401 the fixture, requested by name below
    ingress,  # noqa: F401 the fixture, requested by name below
    realm_backup,  # noqa: F401 the fixture, requested by name below
    repository,  # noqa: F401 the fixture, requested by name below
    scripted,  # noqa: F401 the fixture, requested by name below
    scripted_git,  # noqa: F401 the fixture, requested by name below
    setup_check,  # noqa: F401 the fixture, requested by name below
    setup_run,  # noqa: F401 the fixture, requested by name below
    state,  # noqa: F401 the fixture, requested by name below
    switch,  # noqa: F401 the fixture, requested by name below
    switch_off,
    unreadable_profile_bind,  # noqa: F401 the fixture, requested by name below
    write_fleet,
)

FLEET = (
    f"cloudflare:\n  zone: {ZONE}\nservices:\n"
    "  legal:\n    run: {enabled: true}\n"
    "  agent-service:\n    run: {enabled: false, setup: agent-service/setup}\n"
)
WITHOUT_AGENT = FLEET.split("  agent-service:")[0]
THE_PAGES = [".gitkeep", "datenschutz.html", "impressum.html", "nutzungsbedingungen.html"]
PRIVACY = "legal/sections/allgemein/datenschutz.html"
AGENT = "legal/sections/agent-service/datenschutz.html"
NEW_HEADING = b'<h2 id="neu">Neu</h2>\n<p>Text</p>\n'


@dataclass
class World:
    """A tree with the legal unit enabled, and how to run `edge apply --closed` on it."""

    repository: Path
    scripted: ScriptedCompose
    switch: Callable[..., tuple[int, str, str]]
    state: Path

    @property
    def built(self) -> Path:
        return self.repository / "legal/built"

    def built_names(self) -> list[str]:
        return sorted(path.name for path in self.built.iterdir())

    def built_page(self, name: str) -> str:
        return (self.built / f"{name}.html").read_text()

    def edit(self, path: str, change: Callable[[bytes], bytes]) -> None:
        file = self.repository / path
        file.write_bytes(change(file.read_bytes()))


@pytest.fixture
def world(repository: Path, scripted: ScriptedCompose, switch, state: Path) -> World:  # noqa: F811
    """The repository's own frame, general part and agent service block (a catalog entry,
    disabled) and its Songmaker text (no catalog entry), committed, with an empty `legal/built`."""
    legal = repository / "legal"
    shutil.copytree(REPOSITORY / "legal/frame", legal / "frame")
    for unit in ("allgemein", "agent-service", "songmaker"):
        shutil.copytree(REPOSITORY / "legal/sections" / unit, legal / "sections" / unit)
    (legal / "built").mkdir()
    (legal / "built/.gitkeep").touch()
    write_fleet(repository, FLEET)
    scripted.config["services"]["legal"] = {
        "image": "nginxinc/nginx-unprivileged:test",
        "restart": "unless-stopped",
        "profiles": ["legal"],
    }
    scripted.config["services"]["agent-service"] = {
        "image": "agent-service:test",
        "restart": "unless-stopped",
        "profiles": ["agent-service"],
    }
    shutil.rmtree(repository / ".git")
    committed_main(repository)
    return World(repository, scripted, switch, state)


@pytest.fixture
def private_umask():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


@pytest.mark.usefixtures("private_umask")
def test_the_first_apply_names_and_writes_the_three_pages_readable_by_the_unit(world):
    """L3."""
    exit_code, out, err = world.switch()

    assert exit_code == 0, err
    for page in ("impressum", "datenschutz", "nutzungsbedingungen"):
        assert f"legal: {page} new" in out
    assert world.built_names() == THE_PAGES
    for name in THE_PAGES[1:]:
        assert stat.S_IMODE((world.built / name).stat().st_mode) == 0o644
    privacy = world.built_page("datenschutz")
    assert 'id="agent-dienst"' in privacy
    assert 'id="songmaker"' not in privacy
    assert re.search(r'<p class="stand">Stand: \d{1,2}\. [A-Za-zä]+ 20\d\d</p>', privacy)


def test_the_second_apply_changes_nothing_and_says_so(world):
    """L3."""
    world.switch()
    world.scripted.calls.clear()

    exit_code, out, _ = world.switch(answer="no")

    assert exit_code == 0
    assert "legal: unchanged" in out
    assert "nothing to change" in out
    assert world.scripted.changing_verbs() == []


def test_a_changed_text_goes_live_only_after_yes_and_the_plan_names_its_page(world):
    """L3."""
    world.switch()
    world.edit(PRIVACY, lambda text: text + NEW_HEADING)

    declined_exit, declined_out, _ = world.switch(answer="no")

    assert declined_exit == 1
    assert "legal: datenschutz changes" in declined_out
    assert "nutzungsbedingungen" not in declined_out
    assert 'id="neu"' not in world.built_page("datenschutz")

    exit_code, out, _ = world.switch()

    assert exit_code == 0
    assert "legal: datenschutz changes" in out
    assert 'id="neu"' in world.built_page("datenschutz")


def test_the_plan_names_the_block_whose_text_changed_and_no_other(world):
    """L3."""
    world.switch()
    world.edit(AGENT, lambda text: text + b"<p>Neu</p>\n")

    _, out, _ = world.switch(answer="no")

    assert f"legal: datenschutz changes: {AGENT}" in out
    assert PRIVACY not in out


def test_the_plan_names_a_block_whose_title_was_only_shortened(world):
    """L3."""
    terms = "legal/sections/allgemein/nutzungsbedingungen.html"
    world.switch()
    world.edit(terms, lambda text: text.replace(b"Anwendbares Recht", b"Recht", 1))

    _, out, _ = world.switch(answer="no")

    assert f"legal: nutzungsbedingungen changes: {terms}" in out


def test_a_block_whose_catalog_entry_left_leaves_the_page(world):
    """L3."""
    world.switch()
    assert 'id="agent-dienst"' in world.built_page("datenschutz")
    write_fleet(world.repository, WITHOUT_AGENT)

    exit_code, out, _ = world.switch()

    assert exit_code == 0
    assert "legal: datenschutz changes" in out
    assert 'id="agent-dienst"' not in world.built_page("datenschutz")


def test_a_unit_switched_off_by_edge_stop_still_gets_its_pages_written(world):
    """L3."""
    switch_off(world.state, unit="legal")

    exit_code, out, _ = world.switch()

    assert exit_code == 0
    assert "legal: datenschutz new" in out
    assert world.built_names() == THE_PAGES


def add_image(text: bytes) -> bytes:
    return text.replace(b"<p>", b'<p><img src="x">', 1)


def grow_beyond_the_limit(text: bytes) -> bytes:
    return text + b"x" * 70_000


def write_a_non_breaking_space(text: bytes) -> bytes:
    return text.replace(b"<p>", b"<p>a&nbsp;", 1)


def link_to_a_missing_id(text: bytes) -> bytes:
    return text.replace(b"<p>", b'<p><a href="#nirgends">x</a>', 1)


def link_to_the_agent_service(text: bytes) -> bytes:
    return text.replace(b"<p>", b'<p><a href="#agent-dienst">Agent</a> ', 1)


@pytest.mark.parametrize(
    ("path", "change", "line"),
    [
        (AGENT, add_image, f"{AGENT}: line 2: <img> is not allowed"),
        (AGENT, grow_beyond_the_limit, f"{AGENT}: is 7"),
        (AGENT, write_a_non_breaking_space, f"{AGENT}: line 2: not in canonical form"),
        (PRIVACY, link_to_a_missing_id, f"{PRIVACY}: line 2: link to #nirgends"),
    ],
)
def test_a_refused_section_stops_the_run_before_the_question_and_writes_nothing(
    world, path, change, line
):
    """L11."""
    world.edit(path, change)

    exit_code, out, err = world.switch()

    assert exit_code == 2
    assert f"cannot plan, legal: {line}" in err
    assert "apply exactly this plan" not in out
    assert world.built_names() == [".gitkeep"]


def test_a_link_to_a_block_that_left_refuses_the_run(world):
    """L13."""
    world.edit(PRIVACY, link_to_the_agent_service)
    assert world.switch(answer="no")[0] == 1
    write_fleet(world.repository, WITHOUT_AGENT)

    exit_code, _, err = world.switch()

    assert exit_code == 2
    assert (
        f"cannot plan, legal: {PRIVACY}: line 2: "
        "link to #agent-dienst has no target on /datenschutz" in err
    )


@pytest.mark.parametrize(
    ("missing", "what"),
    [(PRIVACY, "the general part"), ("legal/frame/datenschutz.html", "the frame")],
)
def test_a_missing_general_part_or_frame_is_cannot_plan(world, missing, what):
    """L11."""
    (world.repository / missing).unlink()

    exit_code, _, err = world.switch()

    assert exit_code == 2
    assert (
        f"cannot plan, legal: {missing} is missing; {what} is required while services.legal runs"
        in err
    )


@pytest.mark.parametrize("services", ["{}", "\n  legal:\n    run: {enabled: false}"])
def test_without_an_enabled_legal_unit_there_is_no_step_and_no_line(world, services):
    """L3."""
    write_fleet(world.repository, f"cloudflare:\n  zone: {ZONE}\nservices: {services}\n")
    (world.repository / PRIVACY).write_bytes(b"<script>not even read</script>")

    exit_code, out, err = world.switch()

    assert exit_code == 0
    assert "legal" not in out + err
    assert world.built_names() == [".gitkeep"]


def test_a_write_that_fails_is_partial_and_switches_no_unit(world):
    """L3."""
    world.built.chmod(0o500)
    try:
        exit_code, _, err = world.switch()
    finally:
        world.built.chmod(0o700)

    assert exit_code == 1
    assert (
        "partial, legal: could not write legal/built/impressum.html (Permission denied); "
        "no unit switched; run `edge apply --closed` again"
    ) in err
    assert world.scripted.changing_verbs() == []


@pytest.mark.usefixtures("unreadable_profile_bind")
def test_a_core_failure_after_the_write_names_its_own_step_and_never_legal(world):
    """L3."""
    edit_traefik_yml(world.repository)

    exit_code, _, err = world.switch()

    assert exit_code == 1
    assert world.built_names() == THE_PAGES
    assert "partial, could not find the services edge follows, cannot read" in err
    assert "partial, legal" not in err


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("legal/built/.datenschutz.k3j2.tmp", True),
        ("legal/built/datenschutz.html", True),
        ("legal/built/.gitkeep", False),
    ],
)
def test_the_ignore_rules_keep_the_built_pages_and_leftovers_out_of_the_tree(path, ignored):
    """L3."""
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), "check-ignore", "--quiet", path],
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert (result.returncode == 0) is ignored
