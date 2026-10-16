# Keycloak probe

`probe.py` starts a throwaway Keycloak stack (own compose project, generated secrets in a scratch
directory, no live data) from this checkout. Both modes need 127.0.0.1:8081 and :8090 free (exit
3 when taken), an empty or new `--scratch` directory, and always tear the stack down.

    flock /tmp/probe-stack.lock timeout --kill-after=50 540 \
        python3 probe/keycloak/probe.py [run|hold] --scratch DIR

An exported `DOCKER_GID` overrides the host's `docker` group id in the compose environment; unset
it if it is stale. `--admin-url` (default `http://localhost:8090`) is where the probe reaches
Keycloak's admin port; only tests set it, to talk to a fake.

The `timeout` caps the whole command, bring-up included. When the cap is reached after `READY`,
its SIGTERM ends `hold` as a normal `HOLD ENDED` with exit 0 once the teardown succeeded, while
`timeout` itself exits 124, so `HOLD ENDED` alone does not mean you asked for the stop: your
proof window is bounded by that cap. A cap reached before `READY`, or a teardown that leaves the
stack behind, ends with `PROBE FAILED` and exit 1.

- `run` (the default) proves proofs 1-8 and ends with `PROBE PASSED: proofs 1-8 hold`.
- `hold` brings the same stack up, runs no proof and keeps it for another repository's proof.
  It seeds nothing: callers create their own clients and users through the admin API.

## `hold` contract

Progress lines (`stack: ...`) go to stdout. Read stdout until a line starting `READY ` and parse
the rest as JSON:

    READY {"admin_url": "http://localhost:8090", "edge_url": "http://127.0.0.1:8081", "realm": "hallucinai", "public_issuer": "https://auth.hallucinai.de/realms/hallucinai", "admin_client_id": "config-cli", "admin_client_secret_file": "<scratch>/secrets/config-cli-secret"}

The line holds a path, never a secret value. Get an admin token with `client_credentials`, the
client id above and the secret read from that file, against
`http://localhost:8090/realms/master/protocol/openid-connect/token`.

Keep stdin an open pipe (`subprocess.PIPE`). `hold` blocks until SIGTERM, SIGINT or EOF on stdin;
any of them after `READY` is a normal stop: teardown runs, `teardown: stack ... removed` prints,
then `HOLD ENDED`, exit status 0.

Exit 1 with `PROBE FAILED: ...` when the bring-up fails, when a signal arrives before `READY`, or
when the teardown leaves the stack behind (after `READY` too).

Callers pin edge by commit SHA and require a clean checkout of it.
