"""The release grammar, its order and the skip lines of an app's tags (#255 A4, A5)."""

import pytest

from edge import releases


def newest_of(*tags: str, on_branch: tuple[str, ...] | None = None) -> releases.Choice:
    return releases.choose(tags, tags if on_branch is None else on_branch, "master")


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("v2026.10.22.9", "v2026.10.22.10"),
        ("v2026.12.31.3", "v2027.01.05.1"),
        ("v2026.10.22.2", "v2026.10.23.1"),
    ],
)
def test_the_newest_release_is_greatest_by_date_then_by_number_as_a_number(older, newer):
    assert newest_of(older, newer).newest.tag == newer
    assert newest_of(newer, older).newest.tag == newer


@pytest.mark.parametrize(
    "tag",
    [
        "v1.0-rc1",
        "v2027.1.5.1",
        "v2026.10.22.01",
        "v2026.10.22.0",
        "v2026.13.01.1",
        "v2026.02.30.1",
        "v2026.10.22.1\n",
        "v\uff12026.10.22.1",
        "v2026.10.22.\u0661",
        "V2026.10.22.1",
        "v2026.10.22",
    ],
)
def test_a_tag_that_is_not_a_release_is_skipped_and_named(tag):
    choice = newest_of("v2026.10.22.1", tag)

    assert choice.newest.tag == "v2026.10.22.1"
    assert len(choice.skipped) == 1
    assert choice.skipped[0].reason == "not vYYYY.MM.DD.N"


def test_a_release_not_on_the_default_branch_is_skipped_and_named():
    choice = newest_of("v2026.10.22.2", "v2026.10.22.9", on_branch=("v2026.10.22.2",))

    assert choice.newest.tag == "v2026.10.22.2"
    assert choice.skipped == (releases.SkippedTag("v2026.10.22.9", "not on origin master"),)


def test_without_a_release_there_is_no_newest_and_every_tag_is_named():
    choice = newest_of("v2026.10.22.9", "latest", on_branch=())

    assert choice.newest is None
    assert len(choice.skipped) == 2


def test_a_tag_name_with_a_control_character_is_named_as_it_is_for_the_row_writer_to_escape():
    choice = newest_of("v1\x1b[2J")

    assert choice.skipped == (releases.SkippedTag("v1\x1b[2J", "not vYYYY.MM.DD.N"),)
