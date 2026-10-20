"""The loopback ports of the operator's live stack, which no test may reach.

A module of its own: `conftest` is a different file under `tests/home/`, so a test module that
imports from it breaks in a full run.
"""

# Traefik on 8081, songmaker on 8080.
LIVE_STACK_PORTS = (8080, 8081)
