"""The operator guard shared by `edge external apply` and `edge apply --closed`.

It stops accidental and non-interactive runs; it does not identify the operator (D14).
"""

import sys

CONFIRMATION = "yes"


def refused_without_terminal(command: str) -> bool:
    """True, after saying so, when stdin and stdout are not both a terminal."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return False
    print(f"{command}: refused, stdin and stdout must be a terminal", file=sys.stderr)
    return True


def confirmed(purpose: str) -> bool:
    """Ask for the exact word `yes`; anything else, and a closed stdin, is a no."""
    try:
        return input(f"Type {CONFIRMATION!r} to {purpose}: ") == CONFIRMATION
    except EOFError:
        return False
