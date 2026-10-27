"""The appkit step of `edge apply --closed` (#514): the installed appkit's static files, served by
the legal unit from `legal/assets/appkit-<version>/`, on the scripted boundaries of
`test_edge_apply`."""

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import pytest
from test_edge_apply import (
    applied_core,  # noqa: F401 the fixture, requested by name below
    at_terminal,  # noqa: F401 the fixture, requested by name below
    core_files,  # noqa: F401 the fixture, requested by name below
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
)

from edge import appkit_step

VERSION = metadata.version("overnightworks-appkit")


@dataclass
class World:
    """The repository fixture with its appkit files, and how to run `edge apply --closed`."""

    repository: Path
    switch: Callable[..., tuple[int, str, str]]

    @property
    def served(self) -> Path:
        return appkit_step.served_directory(self.repository)


@pytest.fixture
def world(repository: Path, switch) -> World:  # noqa: F811
    return World(repository, switch)


def files_of(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_a_fresh_checkout_names_the_new_directory_and_writes_it_after_yes(world):
    seeded = files_of(world.served)
    shutil.rmtree(world.served)

    declined_exit, declined_out, _ = world.switch(answer="no")

    assert declined_exit == 1
    assert f"appkit {VERSION}: new" in declined_out
    assert not world.served.exists()

    exit_code, out, err = world.switch()

    assert exit_code == 0, err
    assert f"appkit {VERSION}: new" in out
    assert files_of(world.served) == seeded


def test_the_written_directories_and_files_are_readable_by_the_unit(world):
    shutil.rmtree(world.served)
    previous = os.umask(0o077)
    try:
        exit_code, _, err = world.switch()
    finally:
        os.umask(previous)

    assert exit_code == 0, err
    modes = {
        path: stat.S_IMODE(path.stat().st_mode) for path in (world.served, *world.served.rglob("*"))
    }
    assert all(mode == (0o755 if path.is_dir() else 0o644) for path, mode in modes.items()), modes


def test_a_second_apply_changes_nothing_and_says_so(world):
    world.switch()

    exit_code, out, err = world.switch(answer="no")

    assert exit_code == 0, err
    assert f"appkit {VERSION}: unchanged" in out
    assert "nothing to change" in out


def test_a_damaged_directory_is_named_and_repaired(world):
    original = files_of(world.served)
    (world.served / "appkit.css").write_text("damaged")
    (world.served / "stray.txt").write_text("not the wheel's")

    _, out, _ = world.switch(answer="no")

    assert f"appkit {VERSION}: new" in out

    exit_code, _, err = world.switch()

    assert exit_code == 0, err
    assert files_of(world.served) == original


def test_only_the_steps_own_stale_directories_are_removed(world):
    assets = world.served.parent
    (assets / "appkit-0.4.0").mkdir()
    (assets / "appkit-0.4.0" / "appkit.css").write_text("old")
    (assets / "other").mkdir()

    _, out, _ = world.switch(answer="no")

    assert f"appkit {VERSION}: unchanged, removes appkit-0.4.0" in out
    assert (assets / "appkit-0.4.0").is_dir()

    exit_code, _, err = world.switch()

    assert exit_code == 0, err
    assert sorted(path.name for path in assets.iterdir()) == [f"appkit-{VERSION}", "other"]


def test_a_write_leaves_no_temporary_directory_beside_the_served_one(world):
    (world.served / "appkit.css").write_text("damaged")

    world.switch()

    assert [path.name for path in world.served.parent.iterdir()] == [f"appkit-{VERSION}"]


def test_a_missing_appkit_is_one_cannot_plan_error(repository):  # noqa: F811
    def not_installed(name: str):
        raise ModuleNotFoundError(name)

    with pytest.raises(appkit_step.AppkitError, match="appkit is not installed, run `uv sync`"):
        appkit_step.plan_appkit(repository, not_installed)


def _rename_failing_on(*failing_calls: int):
    calls = []

    def rename(source, destination):
        calls.append(source)
        if len(calls) in failing_calls:
            raise OSError(5, "interrupted")
        os.rename(source, destination)

    return rename


def test_a_failed_final_rename_keeps_the_previous_directory(world):
    served = world.served
    (served / "appkit.css").write_text("previous")

    with pytest.raises(OSError, match="interrupted"):
        appkit_step._replace_directory(
            appkit_step.installed_static(), served, _rename_failing_on(2)
        )

    assert (served / "appkit.css").read_text() == "previous"
    assert [path.name for path in served.parent.iterdir()] == [served.name]


def test_a_failed_restore_keeps_the_previous_copy_and_names_where(world):
    served = world.served
    (served / "appkit.css").write_text("previous")

    with pytest.raises(OSError, match="the previous copy is kept in") as caught:
        appkit_step._replace_directory(
            appkit_step.installed_static(), served, _rename_failing_on(2, 3)
        )

    kept = Path(str(caught.value.strerror).rsplit(" ", 1)[1])
    assert (kept / "appkit.css").read_text() == "previous"


def test_a_missing_assets_directory_is_named_at_plan_time(world):
    assets = world.served.parent
    shutil.rmtree(assets)

    exit_code, _, err = world.switch()

    assert exit_code == 2
    assert "cannot plan," in err
    assert str(assets) in err


def test_the_seeding_helper_serves_the_installed_files(world, seed_appkit):
    shutil.rmtree(world.served)

    seed_appkit(world.repository)

    assert files_of(world.served) == files_of(appkit_step.installed_static())
