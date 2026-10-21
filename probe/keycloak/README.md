# Keycloak probe

`probe.py` starts a throwaway Keycloak stack (own compose project, generated secrets in a scratch
directory, no live data) from this checkout. It runs beside the live edge, which stays up: its
Traefik entrypoints listen on two free loopback ports it picks (a copy of `traefik.yml` in the
scratch directory), its admin front end is `http://localhost:<admin port>`, and its keycloak
network takes a free /24 from 172.31.200.0 up. Both modes need an empty or new `--scratch`
directory and always tear the stack down. While a probe runs, `edge-check` reports its Keycloak as
`invalid-edge-name`, by design of the `edge.name: PROBE` label that keeps the live Traefik away.

    flock /tmp/probe-stack.lock timeout --kill-after=50 540 \
        python3 probe/keycloak/probe.py [run|hold] --scratch DIR

An exported `DOCKER_GID` overrides the host's `docker` group id in the compose environment; unset
it if it is stale. The probe reaches Keycloak's admin entrypoint on the `admin_url` and the edge
entrypoint on the `edge_url` of the `READY` line; it has no flag for another address.

The `timeout` caps the whole command, bring-up included. When the cap is reached after `READY`,
its SIGTERM ends `hold` as a normal `HOLD ENDED` with exit 0 once the teardown succeeded, while
`timeout` itself exits 124, so `HOLD ENDED` alone does not mean you asked for the stop: your
proof window is bounded by that cap. A cap reached before `READY`, or a teardown that leaves the
stack behind, ends with `PROBE FAILED` and exit 1.

- `run` (the default) proves proofs 1-11 and ends with `PROBE PASSED: proofs 1-11 hold`. Proof 1
  fetches the login and account pages through the edge entrypoint and holds when both link a 200 resource under
  `/resources/<version>/{login,account}/hallucinai/` and the login page speaks German for
  `Accept-Language: de` (with a language switcher) and English for `fr` or none. Proof 3c
  runs the realm observer (`realm-observer`, #66) around a default apply with the cache off and
  holds when it lists exactly the console changes the apply removes; proof 9 checks the
  observer's container (65534, read-only, the keycloak network alone, its secret a file), an
  escaped line for a group named with ESC and a newline, that SIGINT and SIGTERM, sent as its
  container runs and as its inventory arrives, leave no container, and that an exit 0 carries
  the whole inventory (a stopped `compose run` exits 130 and may leave part of one on stdout;
  a signal it takes as the container begins to run may be lost, and the observe completes), and
  `does not answer` with Keycloak stopped. Proof 8 also asserts the two themes, the locales and
  the master's untouched theme and language. Proof 10 signs a member of the app's group
  (`/apps/songmaker/admin`, `/apps/hopin`, `/apps/presentator/admin`) in to each of the
  songmaker, hopin and presentator clients through the edge entrypoint with PKCE, its secret a scratch file the
  overlay mounts. For songmaker and presentator it also drives the agent workload cells of #183
  (W1 to W3: the `client_credentials` token has `agent-workload`, the `agent-service` audience, `azp`
  and the realm's token lifespan, with no client override; a person's token has no `agent-workload`), hopin's refusal of
  `client_credentials` (W5) and `agent-service` with every flow off (W4). Proof 11 (#129) loads
  `keycloak/go-live` and runs only its step 6 (never `run()`, whose compose calls would reach the
  operator's project): the admin it creates holds `admin` with `CONFIGURE_TOTP` and
  `webauthn-register` pending (both enabled in master), is refused a password grant without TOTP,
  meets the TOTP enrolment and then the passkey page at the browser sign-in, and a second run asks
  nothing and adds no user. Its typed password joins proof 7's sweep, and master's admin events
  are off. Passkey registration itself is not driven. Proof EV2 (#181) signs in, sends a wrong
  password and logs out through the edge entrypoint, sending `Forwarded` the way Cloudflare's
  rule sets it (`external/cloudflare.tf`) next to a forged `X-Forwarded-For`, and holds when the
  user's stored events are exactly one LOGIN and one LOGIN_ERROR, both with exactly the
  `Forwarded` address, a wrong password from an IPv6 address stores that address, and the logout
  stores none. The probe simulates the rule; it does not prove that Cloudflare applies it (#190).
- `hold` brings the same stack up, runs no proof and keeps it for another repository's proof.
  It seeds nothing beyond the realm's `songmaker`, `hopin` and `presentator` clients, which callers must not create
  themselves; they create their other clients and users through the admin API.

## `hold` contract

Progress lines (`stack: ...`) go to stdout. Read stdout until a line starting `READY ` and parse
the rest as JSON:

    READY {"admin_url": "http://localhost:<admin port>", "edge_url": "http://127.0.0.1:<edge port>", "realm": "hallucinai", "public_issuer": "https://auth.hallucinai.de/realms/hallucinai", "admin_client_id": "config-cli", "admin_client_secret_file": "<scratch>/secrets/config-cli-secret"}

The line holds a path, never a secret value. Get an admin token with `client_credentials`, the
client id above and the secret read from that file, against
`<admin_url>/realms/master/protocol/openid-connect/token`. The ports differ on every run; read
them from the line.

Keep stdin an open pipe (`subprocess.PIPE`). `hold` blocks until SIGTERM, SIGINT or EOF on stdin;
any of them after `READY` is a normal stop: teardown runs, `teardown: stack ... removed` prints,
then `HOLD ENDED`, exit status 0.

Exit 1 with `PROBE FAILED: ...` when the bring-up fails, when a signal arrives before `READY`, or
when the teardown leaves the stack behind (after `READY` too).

Callers pin edge by commit SHA and require a clean checkout of it.
