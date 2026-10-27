"""The legal step of `edge apply --closed`: the three pages built from `legal/frame` and
`legal/sections`, compared with `legal/built`, and written there after the operator's `yes`.

The step runs only while `services.legal` is in the catalog and enabled. Every refusal comes at
plan time, before the question, and nothing is written on one.
"""

import os
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from edge import catalog, checkout
from edge.legal_pages import (
    TEMPLATES,
    Block,
    BlockKind,
    Frame,
    build_pages,
    frame_page_path,
    frame_template_path,
    is_published_in,
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
    pages: Mapping[str, str]  # empty while the step does not run
    changed: Mapping[str, str]  # the pages that differ from `legal/built`: "new" or "changes"
    changed_blocks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # files by page

    @property
    def changes_nothing(self) -> bool:
        return not self.changed

    def describe(self) -> list[str]:
        if not self.pages:
            return []
        if not self.changed:
            return ["legal: unchanged"]
        return [
            f"legal: {page} {change}: {_named(self.changed_blocks.get(page, ()))}"
            for page, change in self.changed.items()
        ]

    def write(self, progress: Progress) -> str | None:
        """Write the changed pages; the failure line, or None. The progress names this step only
        while it runs."""
        progress.unit, progress.step = LEGAL_UNIT, "write the legal pages"
        try:
            for page in self.changed:
                _replace(self.repository / BUILT_DIRECTORY, page, self.pages[page])
        except OSError as error:
            return f"legal: could not write {BUILT_DIRECTORY}/{page}.html ({error.strerror})"
        finally:
            progress.unit = progress.step = ""
        return None


def plan_legal(
    repository: Path, fleet: Fleet, run_git: checkout.Runner = checkout.run_git
) -> LegalPlan:
    """What the step would write; a plan without pages while the legal unit does not run."""
    if not any(unit.key == LEGAL_UNIT and unit.enabled for unit in fleet.units):
        return LegalPlan(repository, {}, {})
    git = checkout.Git(repository, run_git)
    all_blocks = read_blocks(
        repository, _catalog_keys(repository), lambda path: _commit_date(git, path)
    )
    frame = read_frame(repository)
    pages = build_pages(frame, all_blocks)
    changed = {
        page: change
        for page, text in pages.items()
        if (change := _change(repository / BUILT_DIRECTORY / f"{page}.html", text))
    }
    blocks = {page: _changed_blocks(repository, frame, page, all_blocks) for page in changed}
    return LegalPlan(repository, pages, changed, blocks)


def _changed_blocks(
    repository: Path, frame: Frame, page: str, blocks: Sequence[Block]
) -> tuple[str, ...]:
    """The files of the page's blocks whose text the built page does not hold yet."""
    try:
        built = (repository / BUILT_DIRECTORY / f"{page}.html").read_text()
    except FileNotFoundError:
        built = ""
    return tuple(
        block.path for block in blocks if block.page == page and not is_published_in(frame, block, built)
    )


def _named(files: Sequence[str]) -> str:
    return ", ".join(files) if files else "no block (the frame or a block that left)"


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


def _change(built: Path, text: str) -> str | None:
    try:
        current = built.read_bytes()
    except FileNotFoundError:
        return "new"
    return None if current == text.encode() else "changes"


def _replace(directory: Path, page: str, text: str) -> None:
    """The page's new bytes under a temporary name in the same directory, then renamed over it."""
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{page}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, _BUILT_MODE)
        with os.fdopen(descriptor, "wb") as file:
            file.write(text.encode())
        os.replace(temporary, directory / f"{page}.html")
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
