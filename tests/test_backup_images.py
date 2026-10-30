"""The backup's image pins: one packaged Dockerfile, no digest in Python source."""

import re
from importlib import resources
from pathlib import Path

import hal.backup
from hal.backup.apps import PostgresMajor
from hal.backup.settings import RESTIC_IMAGE, RESTORE_IMAGES, SQLITE_IMAGE


def test_settings_returns_the_reference_of_each_stage_of_the_packaged_dockerfile():
    dockerfile = resources.files("hal.backup").joinpath("images", "Dockerfile").read_text()
    stages = {
        stage: image for image, stage in re.findall(r"^FROM (\S+) AS (\S+)$", dockerfile, re.M)
    }

    assert stages == {
        "restic": RESTIC_IMAGE,
        "restore-16": RESTORE_IMAGES[PostgresMajor.SIXTEEN],
        "restore-17": RESTORE_IMAGES[PostgresMajor.SEVENTEEN],
        "sqlite": SQLITE_IMAGE,
    }


def test_no_python_source_pins_an_image_digest():
    """Image pins live in compose and Dockerfiles, where Dependabot proposes their updates."""
    source = Path(hal.backup.__file__).parents[1]

    pinning = [path for path in source.rglob("*.py") if "@sha256:" in path.read_text()]

    assert pinning == []
