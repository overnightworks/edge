# edge

The gatekeeper in front of the tools on the operator's PC. Traefik listens on
loopback only: `127.0.0.1:8081` is the tunnel's target and routes
`<name>.<EDGE_DOMAIN>` to the container that carries the label
`edge.name=<name>`; `127.0.0.1:8090` serves the Keycloak admin console on this
PC and is never a tunnel target. Traefik never sees the Docker socket: a
GET-only socket proxy hands it the container list and events.

Nothing here is reachable from outside until the operator applies the steps A8
below. Cloudflare Access enforcement (and JWT checks) arrives with that step
for every name except `auth`: the login service must be public, so its tunnel
line has no Access (see [Keycloak](#keycloak-auth)). Until A8 the edge is a
local service only.

| File | Owns |
|---|---|
| `compose.yaml` | the services, image pins (version and digest), the socket directory and Keycloak database volumes, the Keycloak secrets |
| `traefik.yml` | the two entrypoints (`edge`, `admin`) and the providers |
| `dynamic/` | file-provider routes for host processes (empty for now) |
| `.env.example` | the two settings the stack needs (Keycloak's secrets are files, see [Keycloak](#keycloak-auth)) |
| `keycloak/` | the realms as code, the secrets entrypoint, the bootstrap overlay, the secret check's rules |
| `probe/keycloak/` | the Keycloak probe, re-run for every Keycloak upgrade |
| `cloudflared-config.example.yml` | the tunnel ingress template the operator applies in A8 |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see below) |

## Threat model

Access protects every edge name except `auth`, which is public by design:
there Traefik admits only `/realms/hallucinai/` and `/resources/`, so the master
realm, the admin console, metrics and health never reach the tunnel. On the
machine any process reaches any tool, the Keycloak admin entrypoint included.

The allowed container inspection is privileged read access and can expose
container environment values; review evidence must not dump those payloads.

## The label rule

A container is routed when it carries both labels:

```yaml
labels:
  traefik.enable: "true"
  edge.name: grafana        # routed as grafana.<EDGE_DOMAIN>
```

- `edge.name` must match `^[a-z0-9-]{1,63}$`: one lowercase label, no dots.
  A container whose `edge.name` is missing or does not match is never routed.
  This is the repository's name policy, not full DNS-label validation (leading
  or trailing hyphens pass).
- The host match is case-insensitive (`GRAFANA.<EDGE_DOMAIN>` works too).
- Traefik reaches the container by its bridge IP. If the container exposes more
  than one port, name it:
  `traefik.http.services.<name>.loadbalancer.server.port: "<port>"`.
- Never put a login-less tool on a network shared with a public tool.

## Configure

```sh
cp .env.example .env     # .env is local; never commit it
getent group docker | cut -d: -f3   # put this number into DOCKER_GID
```

## Start, readiness, stop

```sh
docker compose up -d
```

Readiness: the stack is ready when `127.0.0.1:8081` listens and an unknown host
gets 404. The procedure gives up after 30 seconds:

```sh
set -a; . ./.env; set +a
timeout 30 sh -c '
  until ss -ltnH "sport = :8081" | grep -q "127.0.0.1:8081"; do sleep 1; done
  until [ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
      -H "Host: unknown.$EDGE_DOMAIN" http://127.0.0.1:8081/)" = 404 ]; do sleep 1; done
' && echo ready
```

Stop: `docker compose --profile probe down`. A plain `docker compose down` leaves
a running probe `hello` behind. Neither removes a volume; never add `--volumes`,
because that deletes the Keycloak database.

After a reboot, socket-proxy, traefik, keycloak-db and keycloak come back on
their own (`restart: unless-stopped`); `hello` never does.

What the entrypoint answers without a route:

- unknown host or invalid name: `404`;
- HTTP/1.1 request without a `Host` header: `400` (Go's HTTP server rejects it);
- HTTP/1.0 request without a `Host` header: `404`.

## The probe profile

`hello` (traefik/whoami, labelled `edge.name=hello`) lives under the compose
profile `probe`, so a plain `docker compose up -d` never starts it.

```sh
docker compose --profile probe up -d hello
curl -sS -H "Host: hello.$EDGE_DOMAIN" http://127.0.0.1:8081/    # whoami response
```

Retire it:

```sh
docker compose --profile probe stop hello && docker compose --profile probe rm -f hello
```

Afterwards `hello.<EDGE_DOMAIN>` answers 404, and a plain `docker compose up -d`
leaves hello absent.

## edge-check

A read-only alarm: it fails loudly when something on this PC reaches beyond
loopback. The Docker daemon default (operator step A7) enforces loopback
publications; `edge-check` reports what slipped through.

```sh
uv run edge-check            # human table
uv run edge-check --json     # the same findings as a JSON list, for machines
```

It runs `docker ps --all` + `docker inspect` and `ss -H -ltnupe` (fixed
arguments, no shell, nothing written) and reports:

| Problem | When |
|---|---|
| `publication-beyond-loopback` | a container publishes a port on an address that is not loopback (`0.0.0.0`, `::`, a LAN address) |
| `publication-without-host-ip` | a container publishes a port without a host IP, so Docker would bind every interface; `-P`/`--publish-all` does this for every exposed port |
| `listener-beyond-loopback` | a TCP or UDP socket listens on a non-loopback address and no allow-list entry permits it |
| `routed-without-edge-name` | a container carries `traefik.enable=true` but no `edge.name` label |
| `invalid-edge-name` | a container's `edge.name` does not match `^[a-z0-9-]{1,63}$` |
| `duplicate-edge-name` | two or more running containers carry the same `edge.name` (one finding names them all) |

Stopped containers count too: their publications and labels return when they
start; only the duplicate check looks at running containers alone.
docker-proxy's socket for a running container's publication is not reported
again as a listener: the publication is the finding, and `docker.service`
never belongs on the allow-list.
Each finding names the container or the listening process (or its systemd
unit when `ss` cannot see a root-owned process), the address and the port;
an edge-name finding names the containers and the `edge.name` instead.
`--json` prints a list of objects with the keys `problem`, `subject`,
`protocol`, `address`, `port` and `edge_name` (`port` is `null` when Docker
picks a random one, and a string such as `"8000-8010"` when it picks one from
a range; `protocol`, `port` and `edge_name` are `null` where they do not
apply), deduplicated and in a stable order.

| Exit | Meaning |
|---|---|
| 0 | every publication binds loopback; every listener is loopback or allow-listed |
| 1 | at least one finding |
| 2 | it cannot check: the allow-list is missing or invalid (the message names the entry), or `docker`/`ss` failed or printed something it cannot read (the message names the container or line) |

### The allow-list

`edge-check.toml` in this project (or `--allow-list PATH`) names the known
listeners beyond loopback. The default is found next to the project's
`pyproject.toml`, not in the current directory, so `uv run --project PATH
edge-check` works from anywhere. Docker publications are never allow-listed.

```toml
[[allow]]
protocol = "udp"                  # tcp or udp
owner = "avahi-daemon.service"    # process name, or the system unit (under system.slice)
port = 5353                       # optional when owner is given
reason = "mDNS service discovery" # required
```

Each entry needs a `reason` and a `port`, an `owner`, or both; any other key
is an error. Allow by `owner` alone only when the service opens random ports.

## Keycloak (auth)

The one login for the apps: Keycloak with its own Postgres (`keycloak-db`,
volume `keycloak-db`), both on the `keycloak` network (`172.31.253.0/24`, which
`KC_PROXY_TRUSTED_ADDRESSES` names). Traefik reaches Keycloak on port 8080 in
two ways:

| Entrypoint | Host | Admits |
|---|---|---|
| `edge` (`127.0.0.1:8081`, the tunnel target) | `auth.<EDGE_DOMAIN>` | `/realms/hallucinai/…` and `/resources/…` only; everything else is Traefik's 404 |
| `admin` (`127.0.0.1:8090`, this PC only) | `localhost` | the admin console and the master realm (`http://localhost:8090/admin/`) |

The public issuer is `https://auth.<EDGE_DOMAIN>/realms/hallucinai`; the
master realm's issuer is `http://localhost:8090/realms/master`. Every master
login needs a password and a second factor: an administrator without one must
enrol TOTP before the console opens, and may add a passkey (rpId `localhost`)
afterwards. Remote administration while travelling arrives with the A9 travel
setup (an SSH or Tailscale forward to 8090), never through the tunnel.

Configuration is code: `keycloak/master.yaml` and `keycloak/realm.yaml`,
applied by keycloak-config-cli. People and their group memberships are managed
only in the admin console; no file lists a person. App clients arrive with each
app's cutover (K12–K14).

> **Managed in full.** The apply deletes every group, client, client scope and
> flow that an earlier apply created and the YAML no longer lists. Removing or
> renaming a group in `realm.yaml` therefore deletes the group **and every
> membership in it**. Rename a group in the admin console first, then in the
> YAML.

### Keycloak secrets

No secret is in any file of this repository or in any container's `Config.Env`.
Compose mounts three files from `${HOME}/.secrets/keycloak/`; Postgres reads its
password through `POSTGRES_PASSWORD_FILE`, and `keycloak/entrypoint.sh` exports
the others from `/run/secrets` into the process and `exec`s Keycloak or
keycloak-config-cli. Create them once:

```sh
install -d -m 0700 ~/.secrets/keycloak
for name in db-password config-cli-secret bootstrap-admin-password; do
  openssl rand -hex 32 > ~/.secrets/keycloak/$name
done
chmod 0644 ~/.secrets/keycloak/*   # the container users must read them; the 0700 directory keeps everyone else out
```

`bootstrap-admin-password` lives only until the go-live's step 7.
CI's step "No secret in any file" runs gitleaks with
`keycloak/secret-check/gitleaks.toml`: a value whose key ends in `secret` or
`password` in `keycloak/*.yaml` must be a `$(env:…)` reference, and a
`PASSWORD`/`SECRET` key in `compose.yaml` must be a `_FILE` path.

### Start and apply

```sh
docker compose up -d                       # keycloak-db, then keycloak (about 30 s)
docker compose run --rm keycloak-config-cli   # apply keycloak/*.yaml
```

The apply signs in as the `config-cli` service client and fails loudly on a
`$(env:…)` variable that is not set (`IMPORT_VARSUBSTITUTION_UNDEFINEDISERROR`).
It skips a realm whose file has not changed since the last apply; add
`--env IMPORT_CACHE_ENABLED=false` to force it.

### Keycloak go-live

The operator does these himself, in this order; an agent never holds an admin
credential.

1. **Backup.** K11b (Keycloak in the B1 backup) has landed, or the operator
   accepts running without a backup until it lands.
2. **Secrets.** Create the three files (above).
3. **First start**, with the temporary bootstrap admin `temp-admin`:
   `docker compose --file compose.yaml --file keycloak/bootstrap.compose.yaml up -d keycloak`
4. **First apply**, signed in as `temp-admin`:
   `docker compose --file compose.yaml --file keycloak/bootstrap.compose.yaml run --rm keycloak-config-cli`
5. **Leave the bootstrap overlay:** `docker compose up -d keycloak`. Every later
   apply uses the `config-cli` client.
6. **Permanent admin.** Sign in at `http://localhost:8090/admin/` as
   `temp-admin` (enrol TOTP when asked), create the permanent admin in the
   master realm with the realm role `admin`, sign in as that admin, enrol TOTP
   and add a passkey.
7. **Retire the bootstrap.** Delete `temp-admin` in the admin console, then
   `rm ~/.secrets/keycloak/bootstrap-admin-password`.
8. **Tunnel line.** The operator approves and applies the `auth.hallucinai.de`
   line (A8, step 3) and, if not done yet, the DNS wildcard (A8, step 4).
   Ready when
   `https://auth.hallucinai.de/realms/hallucinai/.well-known/openid-configuration`
   answers with the issuer `https://auth.hallucinai.de/realms/hallucinai`.

### Patch upgrade

1. **Dump the database** (until K11b automates it). The dump holds signing keys
   and credential hashes; keep it as private as the secrets:
   `docker compose exec -T keycloak-db pg_dump --username=keycloak --format=custom keycloak > keycloak-$(date +%F).dump`
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version
   and digest), and of keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe** (below). It needs 8081 and 8090, so stop the live edge
   first with `docker compose --profile probe down` (volumes stay).
4. **Start** with `docker compose up -d`; Keycloak migrates its database on the
   first start of the new version.

### Keycloak probe

`probe/keycloak/probe.py` proves the service on a throwaway compose project
(`edge-kc-probe`) built from this checkout, with throwaway secrets in the
scratch directory, and always removes it again, volumes included. It stands in
for Cloudflare by sending `Host: auth.hallucinai.de` and
`X-Forwarded-Proto: https` to `127.0.0.1:8081`. The test relying party
`probe-rp` and its client exist only there.

```sh
flock /tmp/probe-stack.lock timeout 540 python3 probe/keycloak/probe.py --scratch "$(mktemp -d)"
```

It prints one `PROOF <n> held:` line per proof and ends with `PROBE PASSED`
(exit 0) or `PROBE FAILED: <what>` (exit 1); exit 2 means 8081 or 8090 was
taken. The proofs: the public issuer and the path allow-list (1, 2a), the
master issuer and the forced second factor on 8090 (2b), a failing undefined
variable, a fresh apply, an unchanged export after a cache-off second apply and
a changed YAML that applies, with a seeded member untouched (3a–3d), the
`groups` claim and the `agent-service` audience (4), a disabled user refused on
refresh (5), a back-channel logout reaching the test RP (6), no secret in
`Config.Env` (7), and the realm settings as decided (8).

## Rollback

```sh
docker compose --profile probe down
```

This removes every edge container and the `edge-probe` and `keycloak` networks;
the listeners on `127.0.0.1:8081` and `127.0.0.1:8090` disappear. It keeps the
volumes: `keycloak-db` holds every Keycloak user, credential and session and
survives the rollback, so a later start finds them again. Never run
`down --volumes`. The socket directory volume is tmpfs and holds nothing; remove
its definition alone with `docker volume rm edge_socket-proxy-run`. Then revert
the commit.

## Operator steps A8 (going live)

The operator does these himself, in this order; an agent never applies them.
Each step must hold before the next one starts, because the DNS wildcard is
what makes the edge reachable.

1. **Presentator's Access app.** Check that `presentator.hallucinai.de` keeps
   its own Access app (its people, plus the `/sources/*` bypass) and still
   answers 401 to a request without a session.
2. **Wildcard Access app.** Create the Access app for `*.hallucinai.de`,
   operator only. Note its AUD tag and the team name.
3. **Ingress line.** Apply `cloudflared-config.example.yml` to
   `/etc/cloudflared/config.yml` with the real tunnel ID, team name and AUD tag
   (the wildcard rule always carries `access.required`, `teamName` and
   `audTag` together), then restart cloudflared. The apex, `www` and
   presentator rules stay ahead of the wildcard.
   The `auth.hallucinai.de` line goes along only after the operator has
   approved it and the Keycloak go-live below holds up to its step 6: it sits
   ahead of the wildcard, has **no** Access (browsers and apps must reach the
   login), and points to `http://127.0.0.1:8081`.
4. **DNS `*`.** Add the proxied `*` CNAME to the tunnel. Only now do
   `<name>.hallucinai.de` requests reach the edge, `auth` included.
