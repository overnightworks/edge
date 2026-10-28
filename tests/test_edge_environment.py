"""The `.env` step of `edge apply --closed` (#136): derive, compare, write atomically."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from edge import environment

ZONE = "edge-fixture.invalid"
DOCKER_GID = 998
CORRECT = f"EDGE_DOMAIN={ZONE}\nDOCKER_GID={DOCKER_GID}\n"
AS_ROOT = os.geteuid() == 0
ROOT_REASON = "root reads and writes anywhere, so the cell cannot show the refusal"


def docker_group(name: str) -> SimpleNamespace:
    if name != environment.DOCKER_GROUP:
        raise KeyError(name)
    return SimpleNamespace(gr_gid=DOCKER_GID)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "fleet.yaml").write_text(f"cloudflare:\n  zone: {ZONE}\n")
    return tmp_path


def ensure(tree: Path, group_lookup=docker_group) -> environment.Settings | None:
    return environment.ensure(tree, group_lookup)


def refusal(tree: Path, group_lookup=docker_group) -> str:
    with pytest.raises(environment.EnvironmentRefused) as refused:
        ensure(tree, group_lookup)
    return str(refused.value)


def test_a_missing_file_is_written_with_the_zone_and_the_docker_gid_privately(tree):
    """E1."""
    said = ensure(tree)

    written = (tree / ".env").read_text()
    assert written.splitlines()[0].startswith("#")
    assert written.endswith(CORRECT)
    assert stat.S_IMODE((tree / ".env").stat().st_mode) == 0o600
    assert said == environment.Settings(ZONE, str(DOCKER_GID))


def test_a_written_file_is_accepted_by_the_next_run_without_a_line(tree):
    ensure(tree)

    assert ensure(tree) is None


@pytest.mark.parametrize(
    "content",
    [
        CORRECT,
        "HAND_ADDED=1\n" + CORRECT,
        f'EDGE_DOMAIN="{ZONE}"\n# note\nDOCKER_GID={DOCKER_GID}\n',
    ],
    ids=["identical", "with a hand-added key", "quoted and commented"],
)
def test_a_matching_file_is_left_untouched_without_a_line(tree, content):
    """E2, E3."""
    (tree / ".env").write_text(content)
    before = (tree / ".env").stat()

    assert ensure(tree) is None
    after = (tree / ".env").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert (tree / ".env").read_text() == content


@pytest.mark.parametrize(
    ("content", "line"),
    [
        (f"EDGE_DOMAIN=old.invalid\nDOCKER_GID={DOCKER_GID}\n", 'EDGE_DOMAIN is "old.invalid"'),
        (f"EDGE_DOMAIN={ZONE}\nDOCKER_GID=123\n", 'DOCKER_GID is "123"'),
        ("", "EDGE_DOMAIN is not set"),
        (f"EDGE_DOMAIN={ZONE}\n", "DOCKER_GID is not set"),
        (f"EDGE_DOMAIN={ZONE}\nDOCKER_GID=\n", 'DOCKER_GID is ""'),
    ],
    ids=["domain moved", "gid changed", "empty file", "key missing", "key empty"],
)
def test_a_differing_file_is_named_with_both_values_and_never_rewritten(tree, content, line):
    """E4, E5, E6."""
    (tree / ".env").write_text(content)

    said = refusal(tree)

    assert line in said
    assert "but edge derives" in said
    assert said.endswith("delete it and `hal apply` writes it again")
    assert (tree / ".env").read_text() == content


def test_without_a_docker_group_it_refuses_naming_the_group_and_writes_nothing(tree):
    """E7."""

    def no_group(name):
        raise KeyError(name)

    assert "`docker` group" in refusal(tree, no_group)
    assert not (tree / ".env").exists()


def test_a_catalog_without_a_valid_zone_refuses_and_writes_nothing(tree):
    (tree / "fleet.yaml").write_text("cloudflare:\n  zone: Upper.Case\n")

    assert "cloudflare.zone" in refusal(tree)
    assert not (tree / ".env").exists()


@pytest.mark.skipif(AS_ROOT, reason=ROOT_REASON)
@pytest.mark.parametrize("make", ["directory", "dangling symlink", "unreadable"])
def test_an_environment_file_that_cannot_be_read_is_refused_by_path(tree, make):
    """E8."""
    path = tree / ".env"
    if make == "directory":
        path.mkdir()
    elif make == "dangling symlink":
        path.symlink_to(tree / "gone")
    else:
        path.touch(mode=0)

    assert refusal(tree).startswith(f"cannot read {path} (")
    assert not path.is_file() or path.stat().st_size == 0


def test_a_symlink_to_a_matching_file_is_followed_and_kept(tree):
    (tree / "real.env").write_text(CORRECT)
    (tree / ".env").symlink_to(tree / "real.env")

    assert ensure(tree) is None
    assert (tree / ".env").is_symlink()


@pytest.mark.skipif(AS_ROOT, reason=ROOT_REASON)
def test_a_failed_write_is_refused_by_path_and_leaves_no_partial_file(tree):
    """E12."""
    tree.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        said = refusal(tree)
    finally:
        tree.chmod(stat.S_IRWXU)

    assert said.startswith(f"cannot write {tree / '.env'} (")
    assert sorted(path.name for path in tree.iterdir()) == ["fleet.yaml"]


def test_a_hand_written_file_that_appears_before_the_write_is_not_replaced(tree, monkeypatch):
    """E13: it appears after the existence check and before the link."""
    handwritten = "EDGE_DOMAIN=mine.invalid\n"
    real_link = os.link

    def link_after_the_operator_wrote(source, destination):
        Path(destination).write_text(handwritten)
        real_link(source, destination)

    monkeypatch.setattr(environment.os, "link", link_after_the_operator_wrote)

    assert 'EDGE_DOMAIN is "mine.invalid"' in refusal(tree)
    assert (tree / ".env").read_text() == handwritten
    assert sorted(path.name for path in tree.iterdir()) == [".env", "fleet.yaml"]
