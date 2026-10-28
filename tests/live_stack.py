"""The loopback ports of the operator's live stack, which no test may reach.

A module of its own: several files are named `conftest`, so a test module that imports from
`conftest` gets the one loaded last and breaks in a full run.
"""

# Traefik on 8081, songmaker on 8080.
LIVE_STACK_PORTS = (8080, 8081)
