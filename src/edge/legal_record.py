"""The record of what the legal step last wrote, and what the next build changes against it.

The record sits beside `legal/built/`, not in it: nginx serves that directory. A page's entry is
trusted only while the built page still has the bytes the entry was made from; a missing,
unreadable or stale entry makes every block of the page new, never unchanged.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from edge.legal_pages import (
    BlockFingerprint,
    BlockKind,
    PageFingerprint,
    frame_page_path,
)

RECORD_FILE = "legal/built-record.json"
_RECORD_MODE = 0o600


class Change(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    LEFT = "left"


def read_page(repository: Path, built_text: bytes, page: str) -> PageFingerprint | None:
    """The page's entry, or None when there is none or it does not describe `built_text`."""
    try:
        entry = json.loads((repository / RECORD_FILE).read_text())[page]
        fingerprint = PageFingerprint(
            entry["page_sha256"],
            entry["frame_sha256"],
            tuple(
                BlockFingerprint(
                    block["path"], block["anchor"], BlockKind(block["kind"]), block["sha256"]
                )
                for block in entry["blocks"]
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return fingerprint if fingerprint.page_sha256 == sha256(built_text).hexdigest() else None


def write(repository: Path, fingerprints: Mapping[str, PageFingerprint]) -> None:
    """The whole record under a temporary name, then renamed over the old one."""
    text = json.dumps({page: asdict(one) for page, one in fingerprints.items()}, indent=1)
    target = repository / RECORD_FILE
    temporary = target.with_name(f".{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _RECORD_MODE)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)
    os.replace(temporary, target)


def changes(
    page: str, previous: PageFingerprint | None, current: PageFingerprint
) -> tuple[str, ...]:
    """The lines naming what the new build changes on the page against what was written."""
    if previous is None:
        return tuple(_line(block, Change.NEW) for block in current.blocks)
    before = {(block.path, block.anchor): block for block in previous.blocks}
    after = {(block.path, block.anchor): block for block in current.blocks}
    lines = []
    if previous.frame_sha256 != current.frame_sha256:
        lines.append(f"{frame_page_path(page)} {Change.CHANGED}")
    for key, block in after.items():
        if key not in before:
            lines.append(_line(block, Change.NEW))
        elif before[key] != block:
            lines.append(_line(block, Change.CHANGED))
    lines += (_line(block, Change.LEFT) for key, block in before.items() if key not in after)
    return tuple(lines)


def _line(block: BlockFingerprint, change: Change) -> str:
    return f"{block.path}#{block.anchor} {change}"
