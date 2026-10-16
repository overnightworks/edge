"""The step-ca probe's teardown, checked without Docker: it must finish whatever signal arrives."""

import importlib.util
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

PROBE = Path(__file__).resolve().parents[1] / "probe" / "step-ca" / "probe.py"


@pytest.fixture(scope="module")
def probe() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("step_ca_probe", PROBE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_teardown_finishes_when_a_stop_signal_arrives_during_cleanup(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_signal: signal.Signals
) -> None:
    removed: list[str] = []

    class SignalledDuringCleanup:
        def __init__(self, _scratch: Path) -> None:
            pass

        def teardown(self) -> None:
            os.kill(os.getpid(), stop_signal)
            removed.append("containers and volume")

    monkeypatch.setattr(probe, "Probe", SignalledDuringCleanup)
    monkeypatch.setattr(probe, "prove", lambda _probe: 0)
    monkeypatch.setattr(sys, "argv", ["probe", "--scratch", str(tmp_path / "scratch")])
    handler_before = signal.getsignal(signal.SIGTERM)

    try:
        exit_code = probe.main()
    finally:
        signal.signal(signal.SIGTERM, handler_before)

    assert removed == ["containers and volume"]
    assert exit_code == 0
