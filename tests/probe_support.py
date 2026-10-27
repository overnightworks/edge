"""Importing the modules of `probe/agent-service`: they import each other by bare name from their
own directory, so a test puts that directory on the path for its module and removes every module
it brought in afterwards."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def probe_imports() -> Iterator[None]:
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    sys.path[:0] = [str(ROOT / "probe" / "agent-service"), str(ROOT / "agent-service")]
    try:
        yield
    finally:
        sys.path[:] = path_before
        for name in set(sys.modules) - modules_before:
            sys.modules.pop(name)
