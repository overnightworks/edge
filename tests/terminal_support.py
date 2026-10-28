"""The terminal stand-in and the environment recipe of hal_output's colour contract."""

import io
import sys
from collections.abc import Callable

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


class Typist:
    """The person at the yes prompt: one typed line, none (EOF), or Ctrl-C. `while_waiting` runs
    once the prompt is shown and before the answer comes, as time passes for a real person."""

    def __init__(
        self,
        answer: str | type[BaseException] | None,
        while_waiting: Callable[[], None] | None = None,
    ) -> None:
        self.answer = answer
        self.while_waiting = while_waiting

    def readline(self) -> str:
        if self.while_waiting:
            self.while_waiting()
        if isinstance(self.answer, type):
            raise self.answer
        return "" if self.answer is None else f"{self.answer}\n"


def type_at_prompt(
    monkeypatch: pytest.MonkeyPatch,
    answer: str | type[BaseException] | None = "yes",
    while_waiting: Callable[[], None] | None = None,
) -> Typist:
    typist = Typist(answer, while_waiting)
    monkeypatch.setattr(sys, "stdin", typist)
    return typist
