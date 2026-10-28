"""The operator guard shared by the publish and `hal apply`.

It stops accidental and non-interactive runs; it does not identify the operator (D14).
"""

import sys

NEEDS_TERMINAL = "refused, stdin and stdout must be a terminal"


def at_terminal() -> bool:
    """Whether somebody can answer a prompt: stdin and stdout are both a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def refused_without_terminal(command: str) -> bool:
    """True, after saying so, when stdin and stdout are not both a terminal."""
    if at_terminal():
        return False
    print(f"{command}: {NEEDS_TERMINAL}", file=sys.stderr)
    return True
