"""Loading a script of the repository as a module for the length of a module-scoped fixture."""

import importlib.util
import sys
from collections.abc import Iterator
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType


def loaded_script(name: str, path: Path) -> Iterator[ModuleType]:
    """The module of the script at `path` (with or without a `.py` suffix) under `name`, until the
    iterator is resumed."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses resolve their annotations through it
    loader.exec_module(module)
    yield module
    del sys.modules[name]
