# edge-check and edge-backup

Reference for the operator. It says what the two commands guarantee and what they do not; `edge-check
--help`, `edge-backup --help`, `backup/apps.toml`, `src/edge_check/`, `src/edge_backup/` and `systemd/`
hold the mechanics. The data layer and the re-setup path they serve: [architecture.md](architecture.md)
(L5, section 6).

## edge-check

A read-only alarm: it fails loudly when something on this PC reaches beyond loopback. The Docker daemon
default (operator step A7) enforces loopback publications; `edge-check` reports what slipped through.
Run `uv run edge-check` (human table) or `uv run edge-check --json` (the same findings for machines).

It runs `docker ps --all`, `docker inspect` and `ss -H -ltnupe` (fixed arguments, no shell, nothing
written) and reports, by `problem`:

| Problem | When |
|---|---|
| `publication-beyond-loopback` | a container publishes a port on an address that is not loopback |
| `publication-without-host-ip` | a container publishes a port without a host IP (Docker would bind every interface; `-P` does this for every exposed port) |
| `listener-beyond-loopback` | a TCP or UDP socket listens on a non-loopback address and no allow-list entry permits it |
| `routed-without-edge-name` | a container carries `traefik.enable=true` but no `edge.name` label |
| `invalid-edge-name` | a container's `edge.name` does not match `^[a-z0-9-]{1,63}$` |
| `duplicate-edge-name` | two or more running containers carry the same `edge.name` (one finding names them all) |

Stopped containers count too, since their publications and labels return when they start; only the
duplicate check looks at running containers alone. docker-proxy's socket for a running container's
publication is not reported again as a listener, and `docker.service` never belongs on the allow-list.
`--json` is a deduplicated, stably ordered list of objects with `problem`, `subject`, `protocol`,
`address`, `port` and `edge_name` (`null` where they do not apply; `port` is a string such as
`"8000-8010"` for a range).

Exit 0: every publication binds loopback and every listener is loopback or allow-listed. Exit 1: at least
one finding. Exit 2: it cannot check (allow-list missing or invalid, or `docker`/`ss` failed, timed out
after 30 s or printed something unreadable; `docker inspect` answering fewer containers than `docker ps`
listed is unreadable too).

### The allow-list

`edge-check.toml` (or `--allow-list PATH`) names the known listeners beyond loopback. The default is
found next to the project's `pyproject.toml`, not in the current directory. Docker publications are never
allow-listed. Each `[[allow]]` entry needs a `reason` and a `port`, an `owner` (process name, or the
system unit under `system.slice`), or both, plus `protocol` (`tcp` or `udp`); any other key is an error.
Allow by `owner` alone only when the service opens random ports.

## edge-backup

A weekly encrypted backup of the declared apps (Songmaker and Keycloak) into a local restic repository,
and a monthly test that restores the last successful backup's snapshot and proves it is good.

**What it protects against:** accidental deletion and corruption. The repository `~/backups/restic` sits
on the same disk as the data, so it does **not** protect against losing the disk. A second disk or an
off-site copy is a later slice (architecture.md, gap 19).

`backup/apps.toml` declares each app as a validated record (compose project, database service, dump kind,
database user and name, the Postgres major, the audio volume with the table that names its files, and
what the restore test asks the restored database). There is no hook language. Audio is optional and all
or nothing: Keycloak is DB-only. The database container and the volume are found by their
`com.docker.compose.*` labels; exactly one of each must match.

restic runs from the official image, pinned by version and digest (`src/edge_backup/settings.py`), with no
network, no capabilities and a read-only root, as your own user. Backup and restore test first check that
the pinned restic image and each app's Postgres restore image are present; `docker image prune -a`
removes them and the next run pulls them again by digest, failing early, before staging anything. The
password file is mounted read-only at `/run/secrets/restic-password` and handed to restic only as
`--password-file`; edge-backup never reads, prints or logs it.

### Setup

The one-time setup is: `uv sync` (builds the `.venv` the units run); a restic password in
`~/.secrets/restic/password` (random, mode 0600, directory 0700); `uv run edge-backup init` (creates
`~/backups/restic` once, mode 700); `loginctl enable-linger "$USER"` so the user timers run without a
login session; and the units in `systemd/` (`edge-backup{,-restore-test}.{service,timer}`) linked into
`~/.config/systemd/user/` and enabled as user timers. The units expect the checkout at `~/git/edge`.

Keep a copy of the password in the password manager or on paper. **Without it the repository cannot be
read by anyone, ever:** losing the key loses every backup, and restic has no recovery.

### Schedule and run now

| Timer | When | Does |
|---|---|---|
| `edge-backup.timer` | Sunday 03:00, `Persistent=true` | dump, count, back up; then `forget --keep-weekly 4 --keep-monthly 3 --prune` and `check`, only when every app backed up |
| `edge-backup-restore-test.timer` | first Sunday of the month 05:00, `Persistent=true` | restore the last successful backup's snapshot, load and verify it; then `check --read-data-subset=10%` |

A missed run (PC off) runs at the next boot. If Docker or the app's database is not up yet, the backup
waits up to 10 minutes, then fails. One host lock (`~/.local/state/edge/backup.lock`) serialises every
edge-backup command: run by hand during a backup or restore test, `snapshots`, `restore` or any other
command waits silently until that run ends.

To run now, start the same units the timers take (`systemctl --user start edge-backup.service` and
`edge-backup-restore-test.service`); the last journal line of the unit is the verdict.
`uv run edge-backup snapshots` lists the repository.

### What a backup holds, and its limit

Each snapshot (tag = app name, host `edge-backup`) holds `db/db.dump` (`pg_dump --format=custom`,
transactional), `db/manifest.json` (the number of audio files counted just before the scan) and, for an
app with audio, `audio/` (the volume, read live). No service is stopped or started, so the backup is
crash-consistent: a file written during the scan may be missing or partial. The restore test catches
exactly that, so such a backup never counts as good.

Keycloak's dump holds its realm signing keys, the credential hashes and the client secrets. In the
repository it exists only encrypted. In plain text it exists in exactly three places, each mode 0700:
the staging directory during a backup, the restore test's directory during a restore test (both removed
when the run ends), and the target of `edge-backup restore keycloak --target`, which you delete yourself.
If Keycloak is off or its database is down, the weekly run waits 10 minutes, reports keycloak as failed
and still backs up Songmaker. A failed run skips retention and the repository check for every app.

### The restore test

It restores the last successful backup's snapshot into a private directory under `~/.cache/edge-backup`
and checks:

- `pg_restore --list` reads the dump;
- the dump loads with `pg_restore --exit-on-error --single-transaction` into a throwaway PostgreSQL
  container without network, of the major the app's database runs (16 for Songmaker, 17 for Keycloak),
  each image pinned in `src/edge_backup/settings.py`; the pin of major 17 equals `keycloak-db` in
  `compose.yaml` and a test fails when they differ. The server's diagnostics are discarded because
  PostgreSQL quotes row values in them: a failure shows the step and the exit code only;
- the row count of the app's table (`songs`, Keycloak's `realm`) answers; for Keycloak
  (`require_rows = true`) it is above zero;
- for an app with audio: the restored audio file count equals the manifest's, and every audio file the
  restored database references (`generations.mp3_path`) exists in the copy with a size above zero.

It never touches the live database or volume. Backup and restore test stage their plaintext in
`~/.cache/edge-backup` and remove it when they end. A run killed outright cannot, so the plaintext stays
until the next backup or restore test, which removes it first; that sweep removes only directories a run
marked as its own.

### Status

`~/.local/state/edge/backup-status.json` is written atomically at every phase (schema 2; the shape is in
`src/edge_backup/`). The run's verdict stands at the top (`state`: `running`, `ok` or `failed`, with
`phase`, `failing_phase`, `failing_app`, `exit_category`, `last_attempt`); each app (its name, which is
also its restic tag) has its own last good snapshot and times. The file holds no output, command line,
path or file name.

- A `running` that outlasts the timer's 6-hour limit means the run died: treat it as failed.
- `exit_category` is one of `configuration`, `not-ready`, `command-failed`, `verification-failed`,
  `interrupted`, `internal`; `failing_app` is `null` for a failure that belongs to no app.
- An app that fails does not stop the others, and each app's snapshot is recorded right after its own
  `restic backup` exits 0, so earlier successes stay. A run with any failure prunes nothing. An app
  removed from `apps.toml` keeps its entry; renaming an app makes it a new app.
- A status file that cannot be read (mode 000, corrupt JSON, an unknown `schema`) is never overwritten:
  backup, restore test and restore fail with one German line and exit 1. Past successes are never
  replaced by nulls; repair or remove the file by hand to go on.
- A schema-1 file is read into this shape: its id and times go to the declared app only when exactly one
  app is declared and restic confirms the id in that app's tag scope; otherwise they stay in an
  `unattributed` entry and `restore` asks for `--snapshot`. `restore` never writes the file. An older
  edge-backup refuses a schema-2 file like any unknown one: move the file aside, then restore by
  `--snapshot` or run the next backup.
- `state` describes the latest run of either command: a failed Sunday backup followed by a passing
  restore test reads `ok`, and a timer that never fires leaves the last `ok` standing. A reader judges
  freshness per app: `last_successful_backup` no older than about 8 days,
  `last_successful_restore_test` no older than about 5 weeks.

Exit 0: every phase succeeded. Exit 1: a run failed; one German line names why.

### Restore one app

`uv run edge-backup restore <app> --target <new directory>` restores the snapshot of that app's **last
successful backup** (`apps.<app>.last_snapshot_id`) and prints its short ID, never a newer snapshot that
a failed run left behind. The target is a new directory, not under `~/.cache/edge-backup`; it holds
`db/db.dump` and `audio/`. It refuses with one German line and exit 1, naming the newest snapshot, when
no successful backup of that app is recorded or the recorded snapshot is missing in the repository. On a
machine without the status file, list with `edge-backup snapshots` and pass `--snapshot` with the first 8
or more characters of the ID; an unknown, ambiguous or other app's ID is refused.

**Loading a restore into the live app is a deliberate manual step and no command ships it yet**
(architecture.md, gap 18: a scripted `restore <app> --live` is a planned item). Its invariants:

- Stop every container of the app except its database; the database and the audio volume are found by
  their compose labels, and exactly one database container must match.
- Load the dump with `pg_restore --clean --if-exists --single-transaction --exit-on-error`. A failed
  load changes nothing, and the app stays down until the cause is fixed.
- Copy the audio back with `cp -a` from inside edge-backup's own pinned restic image, so no unpinned
  image gets write access to the live audio volume. It adds and overwrites; audio written after the
  snapshot stays. After a failed copy the database is already restored: fix the cause and copy again
  before starting the app.
- Start the stopped containers again only when both halves succeeded.
- Keycloak: only the operator restores it. Stop `keycloak` alone (Traefik and every other service keep
  running), load the dump into `keycloak-db` as its superuser `keycloak` with server error logging off
  and `pg_restore`'s messages discarded (they can quote rows of a dump that holds keys), start
  `keycloak`, then delete the restore directory.

The exact sequences of both loads are in git history (README before the move to this file).

### Stale locks

edge-backup's host lock frees itself when the process ends. A restic lock left by a killed run is removed
with `uv run edge-backup unlock`, but only once it is stale: every container is host `edge-backup` with
restic as PID 1, so restic counts the lock stale only after 30 minutes. Until then `unlock` leaves it,
and a new run waits up to 5 minutes for it, then fails. No command ever runs with `--no-lock`.

### The old backup

`~/backups/songmaker` (unencrypted) is left untouched. The operator deletes it after the first
successful backup and restore test (home-tools#20).
