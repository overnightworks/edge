# edge

The gatekeeper in front of the tools on the operator's PC. Traefik listens on
loopback only: `127.0.0.1:8081` is the tunnel's target and routes
`<name>.<EDGE_DOMAIN>` to the container that carries the label
`edge.name=<name>`; `127.0.0.1:8090` serves the Keycloak admin console on this
PC and is never a tunnel target. Traefik never sees the Docker socket: a
GET-only socket proxy hands it the container list and events.

Nothing here is reachable from outside until the operator applies the steps A8
below. Cloudflare Access enforcement (and JWT checks) arrives with that step
for every name except `auth`: the login service must be public, so it gets its
own Access app whose one policy is Bypass, and its tunnel line has no JWT check
(see [Keycloak](#keycloak-auth)). Until A8 the edge is a local service only.

| File | Owns |
|---|---|
| `compose.yaml` | the services, image pins (version and digest), the socket directory and Keycloak database volumes, the Keycloak secrets |
| `traefik.yml` | the two entrypoints (`edge`, `admin`) and the providers |
| `dynamic/` | file-provider routes for host processes (empty for now) |
| `.env.example` | the two settings the stack needs (Keycloak's secrets are files, see [Keycloak](#keycloak-auth)) |
| `keycloak/` | the realms as code, the secrets entrypoint, the bootstrap overlay and its master step, the secret check's rules |
| `probe/keycloak/` | the Keycloak probe, re-run for every Keycloak upgrade |
| `cloudflared-config.example.yml` | the tunnel ingress template the operator applies in A8 |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see below) |
| `src/edge_backup/`, `backup/apps.toml`, `systemd/` | the `edge-backup` runner, the declared apps, its user timers (see below) |

## Threat model

Access protects every edge name except `auth`, which is public by design:
there Traefik admits only `/realms/hallucinai/`, `/resources/hallucinai/` and
Keycloak's versioned static `/resources/<5 characters>/`, so the master realm (its
`/resources/master/` texts included), the admin console, metrics and health never
reach the tunnel. On the machine any process reaches any tool, the Keycloak admin
entrypoint included.

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
# Traefik reads traefik.yml (its entrypoints) only when it starts, and a plain `up -d`
# keeps a running Traefik whose service definition did not change.
docker compose up -d --force-recreate socket-proxy traefik
```

Readiness: the stack is ready when `127.0.0.1:8081` and `127.0.0.1:8090`
listen, an unknown host gets 404 on 8081, and Keycloak (with its database)
answers the master realm on 8090. Keycloak's start takes longest, so the
procedure gives up after 240 seconds:

```sh
set -a; . ./.env; set +a
timeout 240 sh -c '
  for port in 8081 8090; do
    until ss -ltnH "sport = :$port" | grep -q "127.0.0.1:$port"; do sleep 1; done
  done
  until [ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
      -H "Host: unknown.$EDGE_DOMAIN" http://127.0.0.1:8081/)" = 404 ]; do sleep 1; done
  until [ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
      http://localhost:8090/realms/master)" = 200 ]; do sleep 1; done
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
| 2 | it cannot check: the allow-list is missing or invalid (the message names the entry), or `docker`/`ss` failed, did not answer within 30 s, or printed something it cannot read (the message names the command, container or line); `docker inspect` answering fewer containers than `docker ps` listed is unreadable too |

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

## edge-backup

A weekly encrypted backup of the declared apps (B1: Songmaker only) into a
local restic repository, and a monthly test that restores the last successful backup's snapshot
and proves it is good.

**What it protects against:** accidental deletion and corruption. The
repository `~/backups/restic` sits on the same disk as the data, so it does
**not** protect against losing the disk. A second disk or an off-site copy is
a later slice.

`backup/apps.toml` declares each app as a validated record (compose project,
database service, dump kind, database user and name, audio volume, and what
the restore test asks the restored database). There is no hook language.
The database container and the volume are found by their
`com.docker.compose.*` labels; exactly one of each must match.

restic runs from the official image, pinned by version and digest
(`restic/restic:0.19.1@sha256:…`, see `src/edge_backup/settings.py`), with no
network, no capabilities and a read-only root, as your own user. Backup and
restore test check first that the pinned restic and postgres images are
present. `docker image prune -a` removes them; the next run pulls them again
by their digest-pinned reference and fails early, before staging anything,
naming the image only if that pull fails. The password
file is mounted read-only at `/run/secrets/restic-password` and handed to
restic only as `--password-file`; edge-backup never reads, prints or logs it.

### One-time setup

```sh
uv sync                                   # builds ~/git/edge/.venv, which the units run
mkdir -p ~/.secrets/restic && chmod 700 ~/.secrets/restic
(umask 077 && head -c 32 /dev/urandom | base64 > ~/.secrets/restic/password)
```

Store a copy of the password in the password manager or on paper now.
**Without it the repository cannot be read by anyone, ever:** losing the key
loses every backup, and restic has no recovery.

```sh
uv run edge-backup init                   # creates ~/backups/restic once (mode 700)
loginctl enable-linger "$USER"            # user timers run without a login session
loginctl show-user "$USER" --property=Linger   # must print Linger=yes
mkdir -p ~/.config/systemd/user
ln -s ~/git/edge/systemd/edge-backup{,-restore-test}.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now edge-backup.timer edge-backup-restore-test.timer
```

The units expect the checkout at `~/git/edge`.

### Schedule

| Timer | When | Does |
|---|---|---|
| `edge-backup.timer` | Sunday 03:00, `Persistent=true` | dump, count, back up; then `forget --keep-weekly 4 --keep-monthly 3 --prune` and `check` |
| `edge-backup-restore-test.timer` | first Sunday of the month 05:00, `Persistent=true` | restore the last successful backup's snapshot, load and verify it; then `check --read-data-subset=10%` |

A missed run (PC off) runs at the next boot. If Docker or the app's database
is not up yet, the backup waits up to 10 minutes, then fails. One host lock
(`~/.local/state/edge/backup.lock`) serialises every edge-backup command: run
by hand during a backup or restore test, `snapshots`, `restore` or any other
command waits silently until that run ends.

### Run now

```sh
systemctl --user start edge-backup.service          # the same path the timer takes
journalctl --user -u edge-backup.service -n 20      # its last line is the verdict
systemctl --user start edge-backup-restore-test.service
uv run edge-backup snapshots
```

### What a backup holds, and its limit

Each snapshot (tag = app name, host `edge-backup`) holds `db/db.dump`
(`pg_dump --format=custom`, transactional), `db/manifest.json` (the number of
audio files counted just before the scan) and `audio/` (the volume, read
live). No service is stopped, so the backup is crash-consistent: a file
written during the scan may be missing or partial. The restore test catches
exactly that, so such a backup never counts as good.

### The restore test

It restores the last successful backup's snapshot into a private directory under
`~/.cache/edge-backup` and checks:

- the restored audio file count equals the manifest's;
- `pg_restore --list` reads the dump;
- the dump loads with `pg_restore --exit-on-error --single-transaction` into a
  throwaway PostgreSQL 16 container without network (removed afterwards);
- the row count of `songs` answers;
- every audio file the restored database references (`generations.mp3_path`)
  exists in the copy with a size above zero.

It never touches the live database or volume.

Backup and restore test stage their plaintext (the dump, the restored copy) in
`~/.cache/edge-backup` and remove it when they end, on success or failure. A
run killed outright (power cut, out of memory, systemd's stop timeout) cannot,
so its plaintext stays there until the next backup or restore test, which
removes it before anything else. That sweep removes only directories a run
marked as its own; anything else in that directory stays.

### Status

`~/.local/state/edge/backup-status.json`, written atomically at every phase:

```json
{"schema": 1, "state": "ok", "phase": "check", "failing_phase": null,
 "exit_category": null, "last_attempt": "2026-10-18T01:00:00+00:00",
 "last_successful_backup": "2026-10-18T01:12:40+00:00",
 "last_successful_restore_test": "2026-10-04T03:09:02+00:00",
 "last_snapshot_id": "4f1c…"}
```

`state` is `running`, `ok` or `failed`. A `running` that outlasts the timer's
6-hour limit means the run died: treat it as failed. `exit_category` is one of
`configuration`, `not-ready`, `command-failed`, `verification-failed`,
`interrupted`, `internal`. When the backup succeeded but prune or check failed,
`last_successful_backup` moves on and `state` is `failed`. The file holds no
output, command line, path or file name.

A status file that cannot be read (mode 000, corrupt JSON, an unknown
`schema`) is never overwritten: backup, restore test and restore fail with one
German line and exit 1, and the file stays byte-identical. Past successes are
never replaced by nulls; repair or remove the file by hand to go on.

`state` describes the latest run of either command, backup or restore test: a
failed Sunday backup followed two hours later by a passing restore test reads
`ok`, and a timer that never fires leaves the last `ok` standing. A reader
therefore also judges freshness: `last_successful_backup` no older than about
8 days, `last_successful_restore_test` no older than about 5 weeks.

| Exit | Meaning |
|---|---|
| 0 | every phase succeeded |
| 1 | a run failed; one German line names why. The status says `failed`, except for an unreadable status file, which stays as it is |

### Restore one app

```sh
uv run edge-backup restore songmaker --target ~/restore-songmaker   # a new directory, not under ~/.cache/edge-backup
```

It restores the snapshot of the **last successful backup**
(`last_snapshot_id` in the status) and prints its short ID, never a newer
snapshot that a failed run left behind (restic exit 3 leaves a partial one). If
that snapshot is gone, or the status names none, `restore` refuses with one
German line and exit 1, names the newest snapshot and says how to choose it.
The restore test verifies the same snapshot and refuses the same way.

Disaster recovery on a machine without the status file: list the repository
with `uv run edge-backup snapshots`, then choose a snapshot of that app by the
first 8 or more characters of its ID:

```sh
uv run edge-backup restore songmaker --snapshot 1a2b3c4d --target ~/restore-songmaker
```

An ID that is unknown, ambiguous or another app's is refused with one German
line. The target holds
`db/db.dump` and `audio/`. Loading them into the live app is a deliberate
manual step. Stop every Songmaker container except its database,
load the dump, copy the audio back into the volume, start them again. Paste the
block as a whole: it runs in a subshell that stops at the first failing command,
so a failed load or copy never goes on to the other half or restarts the app:

```sh
(
set -euo pipefail
restored=~/restore-songmaker
# The copy runs in edge-backup's own restic image, pinned by version and digest: no
# unpinned image ever gets write access to the live audio volume.
image=$(cd ~/git/edge && .venv/bin/python -c \
  'from edge_backup.settings import RESTIC_IMAGE; print(RESTIC_IMAGE)')
pg=$(docker ps --quiet --filter label=com.docker.compose.project=songmaker \
  --filter label=com.docker.compose.service=postgres \
  --filter label=com.docker.compose.oneoff=False)
[ "$(wc -w <<<"$pg")" -eq 1 ] \
  || { echo "need exactly one Songmaker database container, found: ${pg:-none}" >&2; exit 1; }
stopped=$(docker ps --quiet --filter label=com.docker.compose.project=songmaker \
  | { grep -vx "$pg" || true; })
[ -n "$stopped" ] || { echo "no running Songmaker container to stop" >&2; exit 1; }
docker stop $stopped
docker exec -i "$pg" pg_restore --username songmaker --dbname songmaker \
  --clean --if-exists --single-transaction --exit-on-error < "$restored/db/db.dump"
docker run --rm --network none \
  --mount type=bind,source="$restored/audio",target=/from,readonly \
  --mount type=volume,source=songmaker_audiofiles,target=/to \
  --entrypoint cp "$image" -a /from/. /to/
docker start $stopped
)
```

When it stops, Songmaker stays down; `docker stop` printed the IDs of the
containers it stopped. A failed load changed nothing (`--single-transaction`),
so `docker start` with those IDs brings the old state back. After a failed copy
the database is already restored: fix the cause and copy again before you start
them.

`cp -a` adds and overwrites; audio written after the snapshot stays in the
volume. The copied files keep the owner restic restored them with (your user).

### Stale locks

edge-backup's host lock frees itself when the process ends. A restic lock left
by a killed run is removed with `uv run edge-backup unlock`, but only once it
is stale: every container is host `edge-backup` with restic as PID 1, so
restic cannot tell from the PID that the run died and counts its lock as
stale only after 30 minutes. Until then `unlock` leaves it, and a new run
waits up to 5 minutes for it, then fails. No command ever runs with
`--no-lock`.

### The old backup

`~/backups/songmaker` (unencrypted) is left untouched. The operator deletes it
after the first successful backup and restore test (home-tools#20).

## Keycloak (auth)

The one login for the apps: Keycloak with its own Postgres (`keycloak-db`,
volume `keycloak-db`), both on the `keycloak` network (`172.31.253.0/24`, which
`KC_PROXY_TRUSTED_ADDRESSES` names). Traefik reaches Keycloak on port 8080 in
two ways:

| Entrypoint | Host | Admits |
|---|---|---|
| `edge` (`127.0.0.1:8081`, the tunnel target) | `auth.<EDGE_DOMAIN>` | `/realms/hallucinai/…`, `/resources/hallucinai/…` and the versioned static `/resources/<5 characters>/…` only; everything else, `/resources/master/…` included, is Traefik's 404 |
| `admin` (`127.0.0.1:8090`, this PC only) | `localhost` | the admin console and the master realm (`http://localhost:8090/admin/`); `127.0.0.1:8090` answers a bare 404 by design, because the WebAuthn rpId is a hostname |

The public issuer is `https://auth.<EDGE_DOMAIN>/realms/hallucinai`; the
master realm's issuer is `http://localhost:8090/realms/master`. Every master
login needs a password and a second factor: an administrator without one must
enrol TOTP before the console opens, and may add a passkey (rpId `localhost`)
afterwards. A password grant on the master realm (`admin-cli`, `kcadm.sh`)
demands a TOTP code as well, so an administrator without TOTP gets no token
that way. Remote administration while travelling arrives with the A9 travel
setup (an SSH or Tailscale forward to 8090), never through the tunnel.

Configuration is code: `keycloak/master.yaml` and `keycloak/realm.yaml`,
applied by keycloak-config-cli. People and their group memberships are managed
only in the admin console; no file lists a person. App clients arrive with each
app's cutover (K12–K14).

> **Managed in full.** Groups are created and renamed only in `realm.yaml`.
> The apply deletes every top-level group, client, client scope and flow that
> an earlier apply created and the YAML no longer lists, and below a listed
> group it deletes **every** subgroup the YAML lacks, whoever created it: a
> group made in the admin console under `/apps` or `/ki-abo` (say
> `/apps/newtool`) disappears on the next apply, **with every membership in
> it**. Removing or renaming a group in `realm.yaml` deletes it the same way.
> Renaming a group in the admin console first is safe only if no apply runs
> before the matching YAML change lands.

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
CI's step "No secret in any file" runs gitleaks for literal-looking secrets and
`keycloak/secret-check/secret_check.py`, which reads the YAML (block, flow and
list style alike): a value whose key ends in `secret` or `password` in
`keycloak/*.yaml` must be a `$(env:…)` reference, and a `PASSWORD`/`SECRET`
environment entry in any compose file Compose may load (`compose*.yaml`,
`docker-compose*.yml`, overlays included) must be a `_FILE` path. The script's dependencies are locked in
`keycloak/secret-check/secret_check.py.lock`; after changing them run
`uv lock --script keycloak/secret-check/secret_check.py` (not `uv lock`).

### Start and apply

Start the stack as in [Start, readiness, stop](#start-readiness-stop), Traefik
included, and wait until the readiness check prints `ready`. Then apply
`keycloak/*.yaml`:

```sh
docker compose run --rm keycloak-config-cli
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
4. **Master on the admin entrypoint, then the first apply**, both signed in as
   `temp-admin`. The apply cannot move the master realm itself: that move
   changes the issuer of the token it signs in with.

   ```sh
   docker compose --file compose.yaml --file keycloak/bootstrap.compose.yaml exec keycloak /opt/edge/admin-frontend.sh
   docker compose --file compose.yaml --file keycloak/bootstrap.compose.yaml run --rm keycloak-config-cli
   ```

   If the apply fails after it applied `master.yaml`, a second run with the
   overlay gets a 401: master now demands TOTP for `temp-admin`'s password
   grant. Re-run it without the overlay, `docker compose run --rm
   keycloak-config-cli`, because the `config-cli` client exists by then.
5. **Leave the bootstrap overlay and start the edge.** Start as in
   [Start, readiness, stop](#start-readiness-stop): the plain `up -d` recreates
   Keycloak without the overlay, and the forced recreate starts Traefik with
   the current `traefik.yml`, `admin` entrypoint included. Step 6 needs
   `http://localhost:8090`, so wait until the readiness check prints `ready`.
   Every later apply uses the `config-cli` client.
6. **Permanent admin.** Sign in at `http://localhost:8090/admin/` as
   `temp-admin` (enrol TOTP when asked), create the permanent admin in the
   master realm with the realm role `admin`, sign in as that admin, enrol TOTP
   and add a passkey.
7. **Retire the bootstrap.** Delete `temp-admin` in the admin console, then
   `rm ~/.secrets/keycloak/bootstrap-admin-password`.
8. **Public login.** The operator approves the `auth.hallucinai.de` bypass
   app and tunnel line, then applies them (A8, steps 3 and 4) and, if not done
   yet, the DNS wildcard (A8, step 5). Ready when a request without any
   Cloudflare cookie gets the discovery document, not a 302 to
   `cloudflareaccess.com`:

   ```sh
   curl -s https://auth.hallucinai.de/realms/hallucinai/.well-known/openid-configuration \
     | grep -o '"issuer":"[^"]*"'
   # "issuer":"https://auth.hallucinai.de/realms/hallucinai"
   ```

   The operator's own browser is no proof: it holds an Access session and
   passes the wildcard app either way.

### Patch upgrade

1. **Dump the database** (until K11b automates it). The dump holds signing keys
   and credential hashes, so it goes beside the secrets, never into the
   checkout, and only its owner may read it:

   ```sh
   (umask 077; docker compose exec -T keycloak-db pg_dump --username=keycloak --format=custom keycloak \
     > ~/.secrets/keycloak/keycloak-$(date +%F).dump)
   ```
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version
   and digest), and of keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe** (below). It needs 8081 and 8090, so stop the live edge
   first with `docker compose --profile probe down` (volumes stay).
4. **Start** as in [Start, readiness, stop](#start-readiness-stop) and wait
   until the readiness check prints `ready`; Keycloak migrates its database on
   the first start of the new version.

### Keycloak probe

`probe/keycloak/probe.py` proves the service on a throwaway compose project
(`edge-kc-probe`) built from this checkout, with throwaway secrets in the
scratch directory, and always removes it again, volumes included. It stands in
for Cloudflare by sending `Host: auth.hallucinai.de` and
`X-Forwarded-Proto: https` to `127.0.0.1:8081`. The test relying party
`probe-rp` and its client exist only there.

```sh
flock /tmp/probe-stack.lock \
  timeout --kill-after=50 540 python3 probe/keycloak/probe.py --scratch "$(mktemp -d)"
```

It prints one `PROOF <n> held:` line per proof and ends with `PROBE PASSED`
(exit 0) or `PROBE FAILED: <what>` (exit 1, also when SIGTERM or Ctrl-C
stopped it: the teardown still runs and finishes; under the `timeout` wrapper above
Ctrl-C never reaches the probe, so stop it with `kill -TERM` on the probe); exit 2 means a refused
argument (`--scratch` must be a new or empty directory), exit 3 that 8081 or
8090 was taken. The lock is held for at most 590 seconds: at 540 `timeout` stops the
probe, which still tears its stack down (exit 124), and 50 seconds later it
kills the probe and its teardown (exit 137). After an exit 137 or a
`teardown: stack edge-kc-probe FAILED` line, remove it under the lock, from a directory
without a compose file; Compose finds the project's containers, networks and
volumes by its name, and the lock is held for at most 330 seconds:

```sh
flock /tmp/probe-stack.lock \
  timeout --kill-after=30 300 docker compose --project-name edge-kc-probe down --volumes --remove-orphans
```

The proofs: the public issuer and the path allow-list (1, 2a), the master issuer
and the forced second factor on 8090, for the console and for a password grant
(2b), a failing undefined variable, a fresh apply, an unchanged export after a
cache-off second apply and a changed YAML that applies and deletes a
console-made subgroup it does not list, with a seeded member untouched (3a–3d),
the `groups` claim and the `agent-service` audience (4), a disabled user refused
on refresh (5), a back-channel logout reaching the test RP (6), none of the
credentials it made or received (secret files, passwords, the TOTP seed, the RP
secret, every token of every token response, and the admin console's PKCE
verifiers, authorization codes and session cookies) in the `Config.Env` of any
container it ran (the bootstrap Keycloak and every keycloak-config-cli run
included), in any compose, apply or service log output, or in its own output
(7), and the realm settings as decided (8).

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
3. **Login bypass app.** Only after the operator has approved it and the
   Keycloak go-live above holds through its step 7 (until then skip this step
   and the `auth` line in step 4; go-live step 8 returns here): create a second
   Access app for `auth.hallucinai.de` alone, with exactly one policy, action
   **Bypass**, include **Everyone**. Cloudflare enforces Access at its own
   edge, per hostname, before a request reaches the tunnel; without this more
   specific app the wildcard app covers `auth` too, and browsers and the apps'
   token calls meet the Access login. That a per-host app beats the wildcard
   app is proven live by E4 in overnightworks/edge#1.
4. **Ingress line.** Apply `cloudflared-config.example.yml` to
   `/etc/cloudflared/config.yml` with the real tunnel ID, team name and AUD tag
   (the wildcard rule always carries `access.required`, `teamName` and
   `audTag` together), then restart cloudflared. The apex, `www` and
   presentator rules stay ahead of the wildcard.
   The `auth.hallucinai.de` line goes along only together with step 3: it
   sits ahead of the wildcard, carries **no** `access` block (no JWT check:
   the bypass app issues no token), and points to `http://127.0.0.1:8081`.
5. **DNS `*`.** Add the proxied `*` CNAME to the tunnel. Only now do
   `<name>.hallucinai.de` requests reach the edge, `auth` included.
