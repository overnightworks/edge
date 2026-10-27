"""The legal step of `edge apply --closed`: the three pages built from `legal/frame` and
`legal/sections`, compared with `legal/built`, and written there after the operator's `yes`.

The step runs only while `services.legal` is in the catalog and enabled. Every refusal comes at
plan time, before the question, and nothing is written on one.
"""

import json
import os
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from edge import catalog, checkout
from edge.legal_pages import (
    TEMPLATES,
    Block,
    BlockFingerprint,
    BlockKind,
    Frame,
    PageFingerprint,
    build_pages,
    fingerprint_pages,
    frame_page_path,
    frame_template_path,
)
from edge.legal_section import PAGES, LegalError, parse_fragment, parse_general
from edge.unit_plans import Fleet, Progress

LEGAL_UNIT = "legal"
GENERAL_UNIT = "allgemein"
SECTIONS_DIRECTORY = "legal/sections"
BUILT_DIRECTORY = "legal/built"
SECTION_PAGES = ("datenschutz", "nutzungsbedingungen")
# The anchor of a block of edge's own services; any other block's anchor is its catalog key.
SERVICE_ANCHORS = {"keycloak": "anmeldung", "agent-service": "agent-dienst"}
_BUILT_MODE = 0o644  # nginx-unprivileged must read the page whatever the umask


@dataclass(frozen=True)
class LegalPlan:
    repository: Path
    record: Path  # what the last write was made of, in the apply state
    pages: Mapping[str, str]  # empty while the step does not run
    changed: Mapping[str, str]  # the pages to write: "new", "changes" or NO_RECORD
    changed_blocks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # lines by page
    fingerprints: Mapping[str, PageFingerprint] = field(default_factory=dict)

    @property
    def changes_nothing(self) -> bool:
        return not self.changed

    def describe(self) -> list[str]:
        if not self.pages:
            return []
        if not self.changed:
            return ["legal: unchanged"]
        return [
            f"legal: {page} {change}{_named(self.changed_blocks.get(page, ()))}"
            for page, change in self.changed.items()
        ]

    def write(self, progress: Progress) -> str | None:
        """Write the changed pages; the failure line, or None. The progress names this step only
        while it runs."""
        progress.unit, progress.step = LEGAL_UNIT, "write the legal pages"
        failed = ""
        try:
            for page, change in self.changed.items():
                if change != NO_RECORD:
                    failed = f"{BUILT_DIRECTORY}/{page}.html"
                    _replace(self.repository / BUILT_DIRECTORY, page, self.pages[page])
            if self.changed:
                failed = str(self.record)
                write_record(self.record, self.fingerprints)
        except OSError as error:
            return f"legal: could not write {failed} ({error.strerror})"
        finally:
            progress.unit = progress.step = ""
        return None


def plan_legal(
    repository: Path,
    record: Path,
    fleet: Fleet,
    run_git: checkout.Runner = checkout.run_git,
) -> LegalPlan:
    """What the step would write; a plan without pages while the legal unit does not run."""
    if not any(unit.key == LEGAL_UNIT and unit.enabled for unit in fleet.units):
        return LegalPlan(repository, record, {}, {})
    git = checkout.Git(repository, run_git)
    all_blocks = read_blocks(
        repository, _catalog_keys(repository), lambda path: _commit_date(git, path)
    )
    frame = read_frame(repository)
    pages = build_pages(frame, all_blocks)
    fingerprints = fingerprint_pages(frame, all_blocks, pages)
    changed: dict[str, str] = {}
    named: dict[str, tuple[str, ...]] = {}
    for page, text in pages.items():
        built = _read_built(repository, page)
        previous = None if built is None else read_recorded_page(record, built, page)
        if change := _change(built, previous, text):
            changed[page] = change
            named[page] = block_changes(page, previous, fingerprints[page])
    return LegalPlan(repository, record, pages, changed, named, fingerprints)


def _read_built(repository: Path, page: str) -> bytes | None:
    try:
        return (repository / BUILT_DIRECTORY / f"{page}.html").read_bytes()
    except FileNotFoundError:
        return None


def _named(lines: tuple[str, ...]) -> str:
    return f": {', '.join(lines)}" if lines else ""


def read_frame(repository: Path) -> Frame:
    return Frame(
        {page: _read(repository, frame_page_path(page)).decode() for page in PAGES},
        {name: _read(repository, frame_template_path(name)).decode() for name in TEMPLATES},
    )


def read_blocks(
    repository: Path, catalog_keys: Collection[str], date_of: Callable[[str], date]
) -> list[Block]:
    """The blocks of `legal/sections`: the general part, and one per unit that is in the catalog."""
    zone = catalog.zone(catalog.read(repository / catalog.FLEET_FILE))
    blocks: list[Block] = []
    for page in SECTION_PAGES:
        path = f"{SECTIONS_DIRECTORY}/{GENERAL_UNIT}/{page}.html"
        parsed = parse_general(path, _read(repository, path), zone)
        blocks.append(Block(path, page, BlockKind.GENERAL, parsed, date_of(path)))
    for unit in sorted(path.name for path in (repository / SECTIONS_DIRECTORY).iterdir()):
        if unit in catalog_keys and unit != GENERAL_UNIT:
            blocks += _unit_blocks(repository, unit, zone, date_of)
    return blocks


def _unit_blocks(
    repository: Path, unit: str, zone: str, date_of: Callable[[str], date]
) -> list[Block]:
    anchor = SERVICE_ANCHORS.get(unit, unit)
    kind = BlockKind.SERVICE if unit in SERVICE_ANCHORS else BlockKind.APP
    blocks = []
    for page in SECTION_PAGES:
        path = f"{SECTIONS_DIRECTORY}/{unit}/{page}.html"
        if (repository / path).is_file():
            parsed = parse_fragment(path, _read(repository, path), anchor, zone)
            blocks.append(Block(path, page, kind, parsed, date_of(path)))
    return blocks


def _catalog_keys(repository: Path) -> set[str]:
    document = catalog.read(repository / catalog.FLEET_FILE)
    return set(document.get("services", {}))


def _read(repository: Path, relative: str) -> bytes:
    try:
        return (repository / relative).read_bytes()
    except FileNotFoundError as error:
        what = "the general part" if relative.startswith(SECTIONS_DIRECTORY) else "the frame"
        raise LegalError(
            f"{relative} is missing; {what} is required while services.{LEGAL_UNIT} runs"
        ) from error
    except OSError as error:
        raise LegalError(f"{relative}: cannot read it ({error.strerror})") from error


def _commit_date(git: checkout.Git, relative: str) -> date:
    """The committer date of the newest commit that touched the file, in git's `%cs` form."""
    answer = git.call(["log", "-1", "--format=%cs", "--", relative]).stdout.decode().strip()
    try:
        return date.fromisoformat(answer)
    except ValueError as error:
        raise LegalError(f"{relative}: has no commit") from error


def _change(built: bytes | None, previous: PageFingerprint | None, text: str) -> str | None:
    if built is None:
        return "new"
    if built != text.encode():
        return "changes"
    return NO_RECORD if previous is None else None


def _replace(directory: Path, page: str, text: str) -> None:
    """The page's new bytes under a temporary name in the same directory, then renamed over it."""
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{page}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, _BUILT_MODE)  # NOSONAR python:S2612 a public page, read by nginx
        with os.fdopen(descriptor, "wb") as file:
            file.write(text.encode())
        os.replace(temporary, directory / f"{page}.html")
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# The record of what the step last wrote sits in the apply state, not beside the pages: nginx serves
# `legal/built/`. A page's entry is trusted only while the built page still has the bytes the entry
# was made from; a missing, unreadable or stale entry makes every block of the page new, and the
# plan then writes the record even when the page bytes already match.
RECORD_FILE = "legal-built.json"
NO_RECORD = "has no usable record"
_RECORD_MODE = 0o600


class Change(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    LEFT = "left"


def read_recorded_page(record: Path, built_text: bytes, page: str) -> PageFingerprint | None:
    """The page's entry, or None when there is none or it does not describe `built_text`."""
    try:
        entry = json.loads(record.read_text())[page]
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


def write_record(target: Path, fingerprints: Mapping[str, PageFingerprint]) -> None:
    """The whole record under a temporary name, then renamed over the old one."""
    text = json.dumps({page: asdict(one) for page, one in fingerprints.items()}, indent=1)
    temporary = target.with_name(f".{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _RECORD_MODE)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)
    os.replace(temporary, target)


def block_changes(
    page: str, previous: PageFingerprint | None, current: PageFingerprint
) -> tuple[str, ...]:
    """The lines naming what the new build changes on the page against what was written."""
    if previous is None:
        return tuple(_change_line(block, Change.NEW) for block in current.blocks)
    before = {(block.path, block.anchor): block for block in previous.blocks}
    after = {(block.path, block.anchor): block for block in current.blocks}
    lines = []
    if previous.frame_sha256 != current.frame_sha256:
        lines.append(f"{frame_page_path(page)} {Change.CHANGED}")
    for key, block in after.items():
        if key not in before:
            lines.append(_change_line(block, Change.NEW))
        elif before[key] != block:
            lines.append(_change_line(block, Change.CHANGED))
    lines += (_change_line(block, Change.LEFT) for key, block in before.items() if key not in after)
    return tuple(lines)


def _change_line(block: BlockFingerprint, change: Change) -> str:
    return f"{block.path}#{block.anchor} {change}"
