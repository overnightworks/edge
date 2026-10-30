"""What the `test_stack_plan_legal_rows` tests share with other test modules."""

from pathlib import Path

from support.stack_plan_rows import (
    REPOSITORY,
)

from hal.stack.appkit_step import TARGETS, AppkitPlan
from hal.stack.legal_step import BlockChange, LegalPlan, PageChange


def legal_plan(
    changed: dict[str, PageChange], blocks: dict[str, tuple[BlockChange, ...]] | None = None
) -> LegalPlan:
    return LegalPlan(Path(), Path(), {page: "" for page in changed}, changed, blocks or {})


def appkit_plan(changed_labels: tuple[str, ...] = (), stale: tuple[str, ...] = ()) -> AppkitPlan:
    changed = tuple(
        target.served(REPOSITORY, "0.5.0") for target in TARGETS if target.label in changed_labels
    )
    return AppkitPlan("0.5.0", Path(), REPOSITORY, changed, tuple(Path(path) for path in stale))


ALL_LABELS = tuple(target.label for target in TARGETS)
