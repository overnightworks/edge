"""The terminal stand-in and the environment recipe of hal_output's colour contract."""

import io
import sys

import pytest


class TerminalStream(io.StringIO):
    """A stream a person watches."""

    def isatty(self) -> bool:
        return True


def put_stdout_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> TerminalStream:
    """Stdout on a terminal, neither NO_COLOR nor TERM=dumb set. Called in the test body:
    pytest swaps `sys.stdout` again between fixture setup and the call."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    out = TerminalStream()
    monkeypatch.setattr(sys, "stdout", out)
    return out


def put_on_terminals(monkeypatch: pytest.MonkeyPatch) -> tuple[TerminalStream, TerminalStream]:
    """Both streams on a terminal, under the same conditions as `put_stdout_on_a_terminal`."""
    out = put_stdout_on_a_terminal(monkeypatch)
    err = TerminalStream()
    monkeypatch.setattr(sys, "stderr", err)
    return out, err
