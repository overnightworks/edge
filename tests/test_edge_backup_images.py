"""The backup's image pins: one packaged Dockerfile, no digest in Python source."""

import re
from importlib import resources
from pathlib import Path

import edge_backup
from edge_backup.apps import PostgresMajor
from edge_backup.settings import RESTIC_IMAGE, RESTORE_IMAGES


def test_settings_returns_the_reference_of_each_stage_of_the_packaged_dockerfile():
    dockerfile = resources.files("edge_backup").joinpath("images", "Dockerfile").read_text()
    stages = {
        stage: image for image, stage in re.findall(r"^FROM (\S+) AS (\S+)$", dockerfile, re.M)
    }

    assert stages == {
        "restic": RESTIC_IMAGE,
        "restore-16": RESTORE_IMAGES[PostgresMajor.SIXTEEN],
        "restore-17": RESTORE_IMAGES[PostgresMajor.SEVENTEEN],
    }


def test_no_python_source_pins_an_image_digest():
    """Image pins live in compose and Dockerfiles, where Dependabot proposes their updates."""
    source = Path(edge_backup.__file__).parents[1]

    pinning = [path for path in source.rglob("*.py") if "@sha256:" in path.read_text()]

    assert pinning == []
