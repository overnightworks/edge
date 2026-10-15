# edge

The gatekeeper in front of the tools on the operator's PC. Traefik listens on
`127.0.0.1:8081` only and routes `<name>.<EDGE_DOMAIN>` to the container that
carries the label `edge.name=<name>`. Traefik never sees the Docker socket: a
GET-only socket proxy hands it the container list and events.

Nothing here is reachable from outside until the operator applies the steps A8
below. Cloudflare Access enforcement (and JWT checks) arrives with that step;
until then the edge is a local service only.

| File | Owns |
|---|---|
| `compose.yaml` | the three services, image pins (version and digest), the socket directory volume |
| `traefik.yml` | the one entrypoint and the providers |
| `dynamic/` | file-provider routes for host processes (empty for now) |
| `.env.example` | the two settings the stack needs |
| `cloudflared-config.example.yml` | the tunnel ingress template the operator applies in A8 |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see below) |
| `src/edge_backup/`, `backup/apps.toml`, `systemd/` | the `edge-backup` runner, the declared apps, its user timers (see below) |

## Threat model

Access protects the edge; on the machine any process reaches any tool.

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
a running probe `hello` behind.

After a reboot, socket-proxy and traefik come back on their own
(`restart: unless-stopped`); `hello` never does.

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

## edge-backup

A weekly encrypted backup of the declared apps (B1: Songmaker only) into a
local restic repository, and a monthly test that restores the latest snapshot
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
network, no capabilities and a read-only root, as your own user. The password
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
| `edge-backup-restore-test.timer` | first Sunday of the month 05:00, `Persistent=true` | restore the latest snapshot, load and verify it; then `check --read-data-subset=10%` |

A missed run (PC off) runs at the next boot. If Docker or the app's database
is not up yet, the backup waits up to 10 minutes, then fails. One host lock
(`~/.local/state/edge/backup.lock`) serialises backup and restore test.

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

It restores the latest snapshot into a private directory under
`~/.cache/edge-backup` and checks:

- the restored audio file count equals the manifest's;
- `pg_restore --list` reads the dump;
- the dump loads with `pg_restore --exit-on-error --single-transaction` into a
  throwaway PostgreSQL 16 container without network (removed afterwards);
- the row count of `songs` answers;
- every audio file the restored database references (`generations.mp3_path`)
  exists in the copy with a size above zero.

It never touches the live database or volume.

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

| Exit | Meaning |
|---|---|
| 0 | every phase succeeded |
| 1 | a phase failed; one German line names it, and the status says `failed` |

### Restore one app

```sh
uv run edge-backup restore songmaker --target ~/restore-songmaker   # a new directory
```

It holds `db/db.dump` and `audio/`. Loading the dump into the live database
and copying audio back into the volume is a deliberate manual step: stop the
app first, then use `pg_restore` and a container that mounts the volume.

### Stale locks

edge-backup's host lock frees itself when the process ends. A restic lock left
by a killed run is removed with `uv run edge-backup unlock` (it removes stale
locks only); every command waits up to 5 minutes for a live lock and never
runs with `--no-lock`.

### The old backup

`~/backups/songmaker` (unencrypted) is left untouched. The operator deletes it
after the first successful backup and restore test (home-tools#20).

## Rollback

```sh
docker compose --profile probe down
```

This removes all three containers and the `edge-probe` network; the listener on
`127.0.0.1:8081` disappears. The socket directory volume is tmpfs and holds
nothing; `docker compose --profile probe down --volumes` removes its definition
too. Then revert the commit.

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
4. **DNS `*`.** Add the proxied `*` CNAME to the tunnel. Only now do
   `<name>.hallucinai.de` requests reach the edge.
