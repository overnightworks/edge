# edge

The gatekeeper in front of the tools on the operator's PC. Traefik listens on
loopback only: `127.0.0.1:8081` is the tunnel's target and routes
`<name>.<EDGE_DOMAIN>` to the container that carries the label
`edge.name=<name>`; `127.0.0.1:8090` serves the Keycloak admin console on this
PC and is never a tunnel target. Traefik never sees the Docker socket: a
GET-only socket proxy hands it the container list and events.

Nothing here is reachable from outside until the operator applies the steps A8
below. A8 publishes the catalog's hosts; `auth` is public (see
[Keycloak](#keycloak-auth)). There is no Zero Trust for now (operator,
20.10.2026); X8b on #30 adds the wildcard protection. Until A8 the edge is a
local service only.

The platform picture (layers, services, secrets, deploy, re-setup, operations, decisions) is [docs/architecture.md](docs/architecture.md).

| File | Owns |
|---|---|
| `compose.yaml` | the services, image pins (version and digest), the socket directory, the Keycloak database, `step-ca` and `traefik-mcp-acme` volumes, the Keycloak secrets and `step_ca_password` |
| `traefik.yml` | the two entrypoints (`edge`, `admin`) and the providers |
| `dynamic/` | file-provider routes for host processes (empty for now) and the emergency barriers `switch-<unit>.yml`, written by `edge stop` and git-ignored |
| `keycloak/` | the realms as code, the secrets entrypoint, the bootstrap overlay and its master step, the secret check's rules |
| `keycloak/go-live` | the one command that takes Keycloak live (see [Keycloak go-live](#keycloak-go-live)) |
| `probe/keycloak/` | the Keycloak probe, re-run for every Keycloak upgrade |
| `step-ca/` | the internal CA: its `ca.json` and `step-ca/init`, the operator's tool for its keys (see [docs/internal-ca.md](docs/internal-ca.md)) |
| `traefik-mcp.yml`, `mcp/dynamic/` | the second Traefik for the MCP origins: its static configuration and one router file per app (see [docs/internal-ca.md](docs/internal-ca.md)) |
| `probe/step-ca/` | the probe of the internal CA and of `traefik-mcp`, re-run for every step-ca or Traefik upgrade |
| `agent-service/egress/` | the third Traefik, `agent-egress`: the agent service's SNI-allowlisting gateway, its static file and the one file of allowed names (see [docs/internal-ca.md](docs/internal-ca.md)) |
| `agent-service/service.toml` | the agent service's configuration, a read-only bind of the `agent-service` service (see [The agent service](#the-agent-service)) |
| `probe/agent-service/` | the probes of the image, of the egress gateway and its packet windows, and of the service unit, re-run for every Traefik, Docker or agent-providers upgrade |
| `tunnel/` | the cloudflared unit, its drop-ins (`20-nonroot.conf`, the boot skip `30-edge-global-stop.conf`) and the stop-only polkit rule, with `@HOME@`, `@OPERATOR_USER@` and `@TUNNEL_ID@`; edge ships them, overnightworks/home-tools#9 installs them (see [docs/stack.md](docs/stack.md)) |
| `fleet.yaml` | the service catalog: which units of this repository run (`services.<unit>.run.enabled`), and the OpenTofu inputs (see [docs/stack.md](docs/stack.md)) |
| `src/edge/`, `external/` | the `edge` command (`edge apply`, which is `edge apply --closed` and then the publish, `edge stop` and `edge start`, `edge external plan` and `apply`) and the pinned OpenTofu root it plans (see [docs/stack.md](docs/stack.md), [docs/external.md](docs/external.md)) |
| `src/home_tools/`, `docs/home.md`, `bin/home` | the `home` workstation tool (German, see [docs/home.md](docs/home.md)); `bin/home` is a symlink to the one launcher `bin/edge` |
| `src/hal/`, `bin/hal` | the `hal` door of the workstation (see [Running hal](#running-hal)); `bin/hal` is a symlink to the launcher `bin/edge` |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see [docs/backup.md](docs/backup.md)) |
| `src/edge_backup/`, `backup/apps.toml`, `systemd/` | the `edge-backup` runner, the declared apps, its user timers (see [docs/backup.md](docs/backup.md)) |
| `tests/`, `pyproject.toml`, `uv.lock` | the tests of `edge-check`, `edge-backup`, the probe, the secret check, `keycloak/go-live`, `step-ca/init`, `edge apply` (`tests/test_edge_apply.py`), the external edge tests (`tests/test_edge_external.py`) and the OpenTofu tests under `external/tests/`, run only through `python -m edge.external.tofu_test`, with their locked dependencies |
| `.github/workflows/ci.yml` | the static checks (lint, tests, compose renders, no published ports, no secret in any file); CI never starts containers |
| [`docs/naming.md`](docs/naming.md) | the naming rule: how every overnightworks repo, tool, package and unit is named |

## Threat model

Access protects every edge name except `auth`, which is public by design:
there Traefik admits only `/realms/hallucinai/`, `/resources/hallucinai/` and
Keycloak's versioned static `/resources/<5 characters>/`, so the master realm (its
`/resources/master/` texts included), the admin console, metrics and health never
reach the tunnel. The bare `https://auth.<domain>/` answers 302 to the hallucinai
realm's account page. On the machine any process reaches any tool, the Keycloak admin
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

## Running hal

`hal` is the daily door of the workstation. `hal` alone, `hal -h` and `hal --help` list its
commands; `hal <command> --help` shows one. The commands:

- `hal version` names the code that runs: the commit and branch of the checkout the code lives in,
  whether its tree is clean (the rule `edge apply` uses), and how it stands to the `origin/main`
  the last fetch left. It never fetches and changes nothing. `-v` prints each git call on stderr.
  Installed from a wheel there is no checkout, and it says so.
- `hal apply [--closed]`, `hal stop <unit>`, `hal start <unit>`, `hal offline` and `hal online` do
  exactly what `edge apply [--closed]`, `edge stop <unit>`, `edge start <unit>`,
  `edge stop --all` and `edge start --all` do: the same guards, prompts, state files and lines.
  `hal offline` stops the tunnel (no terminal needed); `hal online` reopens it once every unit
  barrier is proven. `stop` and `offline` ask for no confirmation from hal (without a terminal,
  `offline` suppresses password prompts with `--no-ask-password`); `apply`, `start` and `online`
  need a terminal and the exact `yes`. There is no `-y` and no `--json` on these five.
  Agents run `hal stop` and `hal offline` only, never `hal apply`, `hal start` or `hal online`.

Every command ends with one of these exit codes:

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | the command ran and found something that needs attention |
| 2 | the command line is wrong |
| 3 | the command could not do its job, or failed unexpectedly (only the error's type is shown) |
| 4 | a guard of the command refused |
| 130 | interrupted with Ctrl-C |

An error is `error: <what>` on stderr and, where there is a way on, one `next: <command>` line;
never a traceback. There is no `-y`, and `--json` exists only on a command whose result a program
reads. The fleet rules for command lines are overnightworks/quality#80; hal's exit codes and error
form are implemented in `src/hal/grammar.py`. The five stack commands deviate in three ways until
overnightworks/edge#207 H5a2 (Exception E in [docs/stack.md](docs/stack.md)).

## Operations

Run, apply, stop, switches and rollback: [docs/stack.md](docs/stack.md). Cloudflare as code
(`edge external`): [docs/external.md](docs/external.md). `edge-check` and `edge-backup`:
[docs/backup.md](docs/backup.md). The internal CA, `traefik-mcp` and `agent-egress`:
[docs/internal-ca.md](docs/internal-ca.md).

## Keycloak (auth)

### In plain words

Keycloak is the one login for all the apps behind this edge: a person signs in
once and every app asks Keycloak who they are, so no app keeps its own
passwords.

It has two doors. The **public door** is the login page at
`auth.<EDGE_DOMAIN>`; it comes through the Cloudflare tunnel and shows only
what a login needs. The **admin door** is `http://localhost:8090`; it reaches
the admin console and the master realm, works on this PC only and is never
routed through the tunnel.

The secrets (the database password, the password of the tool that applies the
configuration, and a temporary admin password) are three files in
`~/.secrets/keycloak/`; each confidential app client has one more file of its own in
`~/.secrets/keycloak/clients/`. No file of this repository holds one.

"Realm as code" means the settings of the logins (groups, clients, login rules)
are YAML files in `keycloak/`, applied by a tool, not clicked together in the
console. Only the people and their group memberships are managed in the
console. Change the YAML and apply it again, and the running Keycloak follows.

Going live needs you in one place only: in the browser, to create your own
admin with a second factor (a machine must not hold that). Everything else is
`keycloak/go-live` (see [Keycloak go-live](#keycloak-go-live)), and `bin/edge apply`
publishes the public door afterwards. The sections below are the reference.

### Doors, issuers and configuration

The one login for the apps: Keycloak with its own Postgres (`keycloak-db`,
volume `keycloak-db`), both on the `keycloak` network (`172.31.253.0/24`, which
`KC_PROXY_TRUSTED_ADDRESSES` names). Traefik reaches Keycloak on port 8080 in
two ways:

| Entrypoint | Host | Admits |
|---|---|---|
| `edge` (`127.0.0.1:8081`, the tunnel target) | `auth.<EDGE_DOMAIN>` | `/realms/hallucinai/…`, `/resources/hallucinai/…` and the versioned static `/resources/<5 characters>/…` only; everything else, `/resources/master/…` included, is Traefik's 404, except the bare `/`, which redirects (302) to the realm's account page |
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
only in the admin console; no file lists a person. The songmaker, hopin and presentator
clients are in `realm.yaml` before the go-live, each secret the file
`~/.secrets/keycloak/clients/<clientId>` (the go-live makes it; presentator's one client serves
`presentator` and `presentator-talk`); the other app clients
arrive at K13/K14.

songmaker and presentator are also the workload identity of the agent service (A1d, #183): their
service accounts are the only users `realm.yaml` lists, each holding the realm role
`agent-workload` and no group, and their default scopes add the `agent-service` audience. Whoever
holds such an app's client secret can therefore get a workload token for that app, which is the
design; the secret stays the app's own file. hopin has no service account.

#### Theme and language

Keycloak's login and account pages use the realm theme `hallucinai`
(`loginTheme` and `accountTheme` in `realm.yaml`). Its files sit in
`keycloak/themes/hallucinai/{login,account}/theme.properties`, which for now
only name the parents `keycloak.v2` and `keycloak.v3`; compose mounts the
directory read-only into the `keycloak` service. A later look (logo, colours,
font) therefore changes theme files only, never the realm. The email and
admin themes, the master realm and the admin console stay Keycloak's own.

The pages appear in German and English: the browser's language decides,
English applies when neither is asked for, and the pages carry a language
switcher (`internationalizationEnabled`, `supportedLocales`, `defaultLocale`
in `realm.yaml`).

Keycloak in `start` mode caches themes, so a changed theme file shows after
`docker compose --profile keycloak restart keycloak`; that is not a realm
change. `/resources/<version>/...` is served with `Cache-Control:
max-age=2592000` (30 days), and the five-character version survives a plain
restart, so a restart keeps the same URL. A
CSS-only edit, which needs that restart to load, would still come from browser
caches for up to 30 days under the same URL; a look change therefore names a
new stylesheet in `theme.properties` (`styles=`) instead of editing one in
place.

> **The apply adds and changes, and deletes no undeclared top-level object and no person.**
> `compose.yaml` sets every `IMPORT_MANAGED_*` type of keycloak-config-cli 6.5.1 to `no-delete`,
> the cache off, and `IMPORT_USERS_MERGEROLES`/`MERGEGROUPS` on. What the console creates on its
> own stays: people, their memberships, and the groups, clients, roles and top-level flows the file
> does not declare. A console addition inside an object the file declares is part of that object
> and is reset to the file with it, as listed below. No person is ever deleted (a service-account
> user that a console-enabled service account on a declared client created goes with that reset,
> below):
> - **Stays:** an object the file stopped listing. Renaming a group in the YAML therefore creates
>   the new group and leaves the old one.
> - **Reset:** a console change to an object the file declares is reset to the file on the next
>   apply, together with what hangs off that object. The probe (proofs 3c and 3d) measures these, except where marked not measured:
>   - a step or subflow in a declared flow (`browser-mfa-forms`, `direct-grant-mfa-totp`);
>   - a mapper on a declared client scope (the scope `groups`); a mapper on a declared client is
>     ruled the same way but not measured;
>   - a default scope on a declared client that lists its scopes (songmaker, hopin, presentator;
>     an optional scope is ruled the same way but not measured), while a client without scope lists (`agent-service`) keeps its additions;
>   - a client role on a listed service account (`service-account-songmaker`); its realm roles
>     and groups stay;
>   - a service account enabled in the console on a declared client (hopin), which removes the
>     service-account user with it (driven without a membership);
>   - a flow binding override on a declared client.
>
>   A change to a declared object is made in the file, by an agent, never in the console. Deleting
>   a group, client or role is done in the admin console, after a backup, with your yes. The realm
>   observer (`realm-observer`, `src/edge/realm/observe.py`) reads the realms before and after,
>   and `edge.realm` compares the two reads and prints one line per change with its realm.

### Keycloak secrets

No secret is in any file of this repository or in any container's `Config.Env`.
Compose mounts three files from `${HOME}/.secrets/keycloak/`; Postgres reads its
password through `POSTGRES_PASSWORD_FILE`, and `keycloak/entrypoint.sh` exports
the others from `/run/secrets` into the process and `exec`s Keycloak or
keycloak-config-cli. `keycloak/go-live` creates them; by hand, once:

```sh
install -d -m 0700 ~/.secrets/keycloak ~/.secrets/keycloak/clients
for name in db-password config-cli-secret bootstrap-admin-password; do
  openssl rand -hex 32 > ~/.secrets/keycloak/$name
done
chmod 0644 ~/.secrets/keycloak/{db-password,config-cli-secret,bootstrap-admin-password}
# 0644: the container users must read the files; the 0700 directories keep everyone else out
```

Every confidential app client has its own file, `~/.secrets/keycloak/clients/<clientId>`
(0644 in the 0700 directory; by hand, `openssl rand -hex 32 > …/<clientId>` and `chmod 0644` on that file only). `compose.yaml` names them as top-level secrets; go-live reads that
list (with `--profile apply`) and creates only the missing files, once, never over an existing one.
A later app adds one secret to `compose.yaml`, and the next go-live creates only its file.

`bootstrap-admin-password` lives only until the go-live's step 7.
The quality gate (`quality / hygiene`) scans the whole history for literal-looking
secrets; CI's step "No secret in any file" runs
`keycloak/secret-check/secret_check.py`, which reads the YAML (block, flow and
list style alike): a value whose key ends in `secret` or `password` in
`keycloak/*.yaml` must be a `$(env:…)` reference, and a `PASSWORD`/`SECRET`
environment entry in any compose file Compose may load (`compose*.yaml`,
`docker-compose*.yml`, overlays included) must be a `_FILE` path. The script's dependencies are locked in
`keycloak/secret-check/secret_check.py.lock`; after changing them run
`uv lock --script keycloak/secret-check/secret_check.py` (not `uv lock`).

### Start and apply

Start the stack as in [docs/stack.md](docs/stack.md) (Start, readiness, stop) (Keycloak with
`edge apply`), Traefik included, and wait until Keycloak answers the master realm on 8090. A realm
change is then three steps, in this order:

1. **Back up, and test the restore.** The apply deletes no person, but it overwrites a console edit
   to a field the YAML lists and resets the console changes to declared objects named above, so there is a snapshot to go
   back to:

   ```sh
   uv run edge-backup backup
   uv run edge-backup restore-test
   ```

   Both must end with exit 0. The way back is [Restore Keycloak](docs/backup.md#restore-keycloak).
2. **Apply `keycloak/*.yaml`:**

   ```sh
   docker compose --profile keycloak run --rm keycloak-config-cli
   ```

3. **Look at the result.** `git diff` of the YAML is what you asked for; the admin console shows
   what the apply left.

The apply signs in as the `config-cli` service client and fails loudly on a
`$(env:…)` variable that is not set (`IMPORT_VARSUBSTITUTION_UNDEFINEDISERROR`). It always
reconciles for real (`IMPORT_CACHE_ENABLED=false` in `compose.yaml`), never skips a realm whose
file is unchanged. Whatever exists only in Keycloak stays; deleting a group, client or role the
YAML no longer lists is done in the admin console, with your yes, after the backup above. The
step inside `edge apply`, with the list of what changed and of what exists only in Keycloak,
comes when realm changes become frequent (#50).

### Keycloak go-live

Run it from the checkout, as the operator (an agent never runs it against the
live project and never holds an admin credential):

```sh
keycloak/go-live
```

It explains each step before it runs (`keycloak/go-live --help` lists them) and
can be run again after any interruption: every step first checks whether it is
done and the run continues at the first unfinished one. A failure ends with one
line naming the step and exit code 1. It never prints a secret.

**The emergency switch.** While `~/.local/state/edge/stopped/keycloak` exists (`edge stop
keycloak`), go-live refuses at the start of its run, asks nothing and starts nothing:
``go-live: refused, keycloak is switched off (since …); `edge start keycloak` ends the switch``
(exit 1). It reads the marker again, under the switch lock, right before each start (step 3,
step 4b, step 5), so a stop made during the run wins. `--check` starts nothing and ignores the
marker. Once the catalog switches Keycloak on and while go-live is not done, `edge apply` (its local part) asks and runs it in your terminal on yes (`--check` first).

- **1. Backup.** It asks whether to continue without the Keycloak backup only while
   edge-backup ([docs/backup.md](docs/backup.md)) is not set up on this PC (no password file with mode 600, or no
   initialised repository). There is no default; the answer is yours. Nothing is asked once
   edge-backup is set up (its next weekly run covers Keycloak; the check does not prove a
   snapshot exists) or Keycloak's database volume exists (the first start creates it), so a
   "no" is asked again and a finished install is never asked, even with Keycloak stopped.
- **2. Secrets.** It creates the three files with random values, in a 0700
   directory with 0644 files. An existing file is never overwritten; if only
   some of the files exist it refuses and names the missing ones.
   The one exception: once Keycloak's database exists, a missing
   `bootstrap-admin-password` alone means step 7 has already retired it, and the
   run goes on. If the database volume is missing as well, the line names the volume
   `keycloak-db`; nothing is deleted.
- **2b. Client secrets.** It creates one random secret file per confidential app client
   that `compose.yaml` names, in `~/.secrets/keycloak/clients/`. Only a missing file is
   created; an existing one is never overwritten. A client name that is not lower-case
   letters, digits and hyphens is refused. A run after the go-live, with one such file
   missing, creates only that file.
- **3. First start** with the temporary admin `temp-admin`.
- **4a. Master on the admin entrypoint**, with `temp-admin`. The apply cannot move the
   master realm itself, because the move changes the issuer of the token it signs in with.
- **4b. First apply** of the realms, with `temp-admin`. While step 5 restarts
   Keycloak, a resumed run waits for the edge before it reads whether the apply
   completed.
- **5. Leave the bootstrap overlay and start the edge.** The script waits by
   itself until the edge is ready.
- **6. Your admin.** The script asks for a user name (Enter takes your Unix user; it is
   lower-cased) and a first password, hidden, twice. It creates that master user through the
   admin API (signed in as the `config-cli` service account) with the realm role `admin`, a
   permanent password and the pending actions "configure TOTP" and "register a passkey". The
   password goes only to Keycloak on this PC, never into a command line, a file or a log.
   Without a terminal it stops before it reads anything. A name that exists already is refused
   (choose another, or delete it in the console); if the role cannot be set, the new user is
   deleted again. A run that finds such an admin asks nothing and changes nothing.
- **7. Your part, in the browser, then retire the bootstrap.** The script stops and says what
   to do: sign in at `http://localhost:8090/admin/` as that admin with the password you typed,
   enrol TOTP and add a passkey, sign out and sign in once more. Press Enter when done. The
   script then checks through the admin API that one master user with the realm role `admin`
   has both a TOTP and a passkey credential. Only then it deletes `temp-admin` and the
   `bootstrap-admin-password` file; otherwise it refuses and says what is missing.

Last, the publish (unnumbered). Started by `bin/edge apply`, the script ends and the apply
goes on to publish `auth.hallucinai.de`; started by hand, it ends unpublished and its last line
names `edge apply`, which publishes once the catalog declares it. There is no Cloudflare step
of yours any more.

#### Is go-live finished? `--check`

```sh
keycloak/go-live --check
```

It reads only go-live's durable trace, so Keycloak need not run: it is done when
`db-password` and `config-cli-secret` exist in `~/.secrets/keycloak/`,
`bootstrap-admin-password` does not (only step 7 deletes it, after it deleted
`temp-admin`), the `keycloak-db` volume exists, and every client secret file that
`compose.yaml` names exists (`docker compose config` needs no daemon; a missing file gives
exit 1 and the line `the secret of client <id> is missing (...); run keycloak/go-live`). It is also the setup check of `edge apply
--closed` for the unit `keycloak`. It asks nothing, prints no
secret and creates or changes no file, directory, volume or container. A
bootstrap file deleted by hand reads as done; a full `keycloak/go-live` run still
checks `temp-admin` through the admin API.

| Exit | Meaning |
| --- | --- |
| 0 | it is set up |
| 1 | it is not set up; one line names what is missing |
| 2 | it cannot tell, because Docker or Compose did not answer or answered something it cannot read |

#### Fallback: the steps by hand

If the script cannot be used, the same steps by hand, in this order (they do not read the
marker `stopped/keycloak`: check `ls ~/.local/state/edge/stopped` first and do not start a
switched-off Keycloak):

- **1. Backup.** Unless edge-backup ([docs/backup.md](docs/backup.md)) is set up on this PC, decide once whether to
   continue without the Keycloak backup; stop here if you do not accept that.
- **2. Secrets.** Create the three files as in [Keycloak secrets](#keycloak-secrets).
- **2b. Client secrets.** Create `~/.secrets/keycloak/clients/<clientId>` for each client
   secret `compose.yaml` names, the same way (`openssl rand -hex 32`, mode 0644).
- **3. First start**, with the temporary bootstrap admin `temp-admin`:
   `docker compose --profile keycloak --file compose.yaml --file keycloak/bootstrap.compose.yaml up -d keycloak`
- **4a. Master on the admin entrypoint**, with the overlay:

  ```sh
  docker compose --profile keycloak --file compose.yaml --file keycloak/bootstrap.compose.yaml exec keycloak /opt/edge/admin-frontend.sh
  ```

- **4b. First apply** of the realms, with the overlay:

  ```sh
  docker compose --profile keycloak --file compose.yaml --file keycloak/bootstrap.compose.yaml run --rm keycloak-config-cli
  ```

  If the apply fails after it applied `master.yaml`, a second run with the
  overlay gets a 401: master now demands TOTP for `temp-admin`'s password
  grant. Re-run it without the overlay, `docker compose --profile keycloak run
  --rm keycloak-config-cli`, because the `config-cli` client exists by then.
- **5. Leave the overlay and start the edge** as in
  [docs/stack.md](docs/stack.md) (Start, readiness, stop): `docker compose --profile keycloak up -d
  keycloak` recreates Keycloak without the overlay, and `edge apply --closed` recreates a running Traefik whose
  `traefik.yml` or compose definition changed; a Traefik that is not running it leaves stopped,
  so start it first with `docker compose up -d --no-recreate socket-proxy traefik`. Wait until
  Keycloak answers the master realm on 8090.
- **6. Create your admin by hand.** Sign in at `http://localhost:8090/admin/` as `temp-admin`
  (password in the bootstrap file; enrol TOTP when asked), add a user in the master realm, give
  it the realm role `admin` and a password that is not temporary.
- **7. Retire the bootstrap.** Sign in as that user, enrol TOTP and add a passkey, then delete
  `temp-admin` in the admin console and run `rm ~/.secrets/keycloak/bootstrap-admin-password`.

Last, **`bin/edge apply`** (unnumbered), as above.

### Patch upgrade

1. **Back up the database.** The dump holds signing keys and credential
   hashes; edge-backup keeps it encrypted (see [docs/backup.md](docs/backup.md)):

   ```sh
   uv run edge-backup backup
   ```

   It must end with exit 0 before you go on.
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version
   and digest), and of keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe** (below). It runs beside the live edge, which stays up.
4. **Start** as in [docs/stack.md](docs/stack.md) (Start, readiness, stop) and wait
   until Keycloak answers the master realm on 8090; Keycloak migrates its database on
   the first start of the new version.

### Keycloak probe

`probe/keycloak/probe.py` proves the service on a throwaway compose project
(`edge-kc-probe`) built from this checkout, with throwaway secrets in the
scratch directory, and always removes it again, volumes included. It runs beside
the live edge: its Traefik listens on two free loopback ports of its own (never
8081 or 8090) and routes only its own project. It stands in for Cloudflare by
sending `Host: auth.hallucinai.de` and `X-Forwarded-Proto: https` to its edge
entrypoint (and the `Forwarded` header Cloudflare's rule sets, for EV2). The test relying party
`probe-rp` and its client exist only there.

```sh
flock /tmp/probe-stack.lock \
  timeout --kill-after=50 540 python3 probe/keycloak/probe.py --scratch "$(mktemp -d)"
```

It prints one `PROOF <n> held:` line per proof and ends with `PROBE PASSED`
(exit 0) or `PROBE FAILED: <what>` (exit 1, also when SIGTERM or Ctrl-C
stopped it: the teardown still runs and finishes; under the `timeout` wrapper above
Ctrl-C never reaches the probe, so stop it with `kill -TERM` on the probe); exit 2 means a refused
argument (`--scratch` must be a new or empty directory). The lock is held for at most 590 seconds: at 540 `timeout` stops the
probe, which still tears its stack down (exit 124), and 50 seconds later it
kills the probe and its teardown (exit 137). After an exit 137 or a
`teardown: stack edge-kc-probe FAILED` line, remove it under the lock, from a directory
without a compose file; Compose finds the project's containers, networks and
volumes by its name, and the lock is held for at most 330 seconds:

```sh
flock /tmp/probe-stack.lock \
  timeout --kill-after=30 300 docker compose --project-name edge-kc-probe down --volumes --remove-orphans
```

The proofs: the public issuer, the themed login and account pages in German
and English and the path allow-list (1, 2a), the master issuer and the
forced second factor on the admin entrypoint, for the console and for a password grant (2b), a failing undefined variable, a fresh apply, an unchanged export after a
second apply that resets only the pinned console changes to declared objects (the 11 pinned lines) and keeps every other console
change, and a changed YAML that applies and deletes no undeclared top-level object and no person, with a seeded member untouched
(3a–3d),
the `groups` claim and the `agent-service` audience (4), a disabled user refused
on refresh (5), a back-channel logout reaching the test RP (6), none of the
credentials it made or received (secret files, passwords, the TOTP seed, the RP
secret, every token of every token response, and the admin console's PKCE
verifiers, authorization codes and session cookies) in the `Config.Env` of any
container it ran (the bootstrap Keycloak and every keycloak-config-cli run
included), in any compose, apply or service log output, or in its own output
(7), the realm settings as decided, with admin events that carry no client
representation (8), a sign-in and a wrong password that store exactly one LOGIN and one
LOGIN_ERROR event, both with exactly the address of the `Forwarded` header the probe sends the way
Cloudflare's rule sets it, though a forged `X-Forwarded-For` rides along, one more wrong password
from an IPv6 address that stores that address in brackets, and a logout that stores none (EV2; the probe does
not prove that Cloudflare sets the header, the tofu test and the live check do), and the
songmaker, hopin and presentator clients: a PKCE sign-in with the
scratch secret that ends in an ID token with the member's group (`/apps/songmaker/admin`,
`/apps/hopin`, `/apps/presentator/admin`), an exact callback, the password grant,
`client_credentials` (hopin only) and the `offline_access` scope refused (a client has no optional
scope), and an end-session page for its registered logout address only (10). Proof 10 also holds
the agent workload shape (#183): songmaker's and presentator's `client_credentials` token carries
the realm role `agent-workload`, the `agent-service` audience, `client_id` and `azp` equal to the
client, no group, and the realm's access token lifespan (300 s, with no client override); a
person's token from the same login has the audience and groups and never the role;
`agent-service` keeps every flow off. Proof 7 also looks for each client secret and its
tokens.

## The agent service image

`agent-service/image/` is the recipe of the agent service's image (agent-providers
`v0.4.0`, Claude Code `2.1.302`). Every byte that enters it is pinned: both base images by digest,
the Python packages by the hashes of `requirements.txt`, the Claude CLI by its sha256. A changed
or vanished artifact fails the build. The image is never pulled from a registry, because its name
could be anyone's on a public one (`pull_policy: never`). Build it on the PC, from the repository root:

```sh
docker build -f agent-service/image/Dockerfile -t edge-agent-service:0.4.0-2.1.302 agent-service/image
```

A release bump runs `python3 scripts/agent_service_lock.py <tag>` (it needs `git` and `uv`), the
only writer of `agent-service/image/requirements.txt`, and changes the CLI pin in the Dockerfile
and the name above together. `probe/agent-service/image.py` builds without cache and starts the
entrypoint from the clean image.

## The agent service

`agent-service` (profile `agent-service`) is that image with `agent-service/service.toml`: one
container, one worker, the networks of `x-agent-attachments`, no published port. It runs as
`1000:1000` with `init`, a read-only root, no capability, `no-new-privileges`, no core dump and
`restart: unless-stopped`; `/tmp` (its `HOME`) and the working root `/run/agent-service/cli` are
size-bounded tmpfs mounts owned by 1000 with mode 0700 (the service refuses to start on a working
root that is not owned by its user with exactly that mode), all `nosuid,nodev,noexec`. The unit
`agent-service` of `fleet.yaml` starts it with `agent-egress`: `hal apply` brings both up, and a
missing image or login directory stops the start (`pull_policy: never`,
`create_host_path: false`), so apply reports the unit partial and never on.

**The login.** The service runs the Claude CLI under the operator's subscription, with the one
token that `claude setup-token` prints, in one file inside a read-only directory bind:

```
~/.secrets/agent-service/              0700
~/.secrets/agent-service/claude/       0700, uid 1000   (bound at /run/agent-service/login/claude)
~/.secrets/agent-service/claude/token  0600, uid 1000, one line
```

Make it by hand, once. The token is pasted only here, never on a command line or in an issue:

```sh
install -d -m 0700 ~/.secrets/agent-service ~/.secrets/agent-service/claude
claude setup-token    # prints the token once
( umask 077; cat > ~/.secrets/agent-service/claude/token )    # paste the line, Enter, Ctrl-D
```

The file is a regular file of at most 16 KiB with one line, the token: no JSON, no API key
(`sk-ant-api…`), nothing group or others can touch. The service refuses to start otherwise, naming
`profiles.claude.token_file` and the path, never the content. `home secrets` lists the entry
`agent-service-claude`.

**Why 1000:1000.** The service's reader refuses a login file that group or others can touch, so the
container user must own the directory and the file. 1000 is the operator's uid on this PC
(`id -u`), so his own `claude setup-token` writes a file the container can read, and the renewal
below works as written. This runbook assumes the operator is uid 1000; another uid needs its own
renewal procedure, not covered here. It is a deployment choice, not the reader's requirement.

**Renewal.** Write the new line beside the old one and swap the name; the next run opens the file
anew, without a restart:

```sh
( umask 077; cat > ~/.secrets/agent-service/claude/token.new )    # paste the line, Enter, Ctrl-D
mv -T ~/.secrets/agent-service/claude/token.new ~/.secrets/agent-service/claude/token
```

The bind is the directory, never the file: a file bind (or a compose `secrets:` file) would keep
the old inode, and the rename would never reach the running container. A missing directory fails
the start (`create_host_path: false`) instead of becoming a root-owned one; the directory itself
stays for ever, so never replace or re-create it.

**A change reaches the running service** only by recreating it. After `service.toml`, `compose.yaml`
or the image changed in the live checkout (`git -C ~/git/edge pull --ff-only`; the image built as
above),

```sh
docker compose --profile agent-service up -d --force-recreate agent-service
```

recreates it, which ends every run in flight.

**An app's side.** An app reaches the service by joining the `agent` network, whose name is fixed
(`hal_agent`, so the project's rename does not move it), as an external network:

```yaml
services:
  app:
    networks: [default, agent]
networks:
  agent: {external: true, name: hal_agent}
```

and calls `http://agent-service:8080` (the port the service listens on; the smoke and the unit probe
use it). No CA mount is needed: the service is plain http on an isolated internal network, and no host
process can reach it, because the network has no host address and publishes no port.

**The smoke.** `agent-smoke` (profile `apply`, so `up` never starts it) is the service image running
`agent-service/smoke.py` on the `agent` network alone, with no port and no secret. It reads the
person's token and then the workload's from stdin (two lines, nothing else supplies one), starts one
run and prints each event's type and the answer; no token appears in its output. The identity is the
`songmaker` client's code flow with PKCE (ruled in edge#184, D6): you sign in as yourself and copy the
address of the browser's error page, because `songmaker.hallucinai.de` has no DNS yet and the code
sits in that address. The same client's service account is the workload. Needs only `python3`, `docker`
and the client's secret file; run it in the live checkout, after `hal apply` and with the image built
(above). First, the person must be in the group `/ki-abo` (`subscription_group` of `service.toml`):
if you are not, add yourself once in the admin console on port 8090 (user management, not a realm
change). The block prints the sign-in address, reads your pasted address without echo, and writes the
two tokens into a file of mode 0600 that is deleted at the end:

```sh
cd ~/git/edge
( umask 077; python3 -I -c "$(cat <<'PY'
import base64, getpass, hashlib, json, secrets, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

OIDC = "https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect"
CLIENT = "songmaker"
REDIRECT = "https://songmaker.hallucinai.de/auth/callback"
client_secret = (Path.home() / ".secrets/keycloak/clients/songmaker").read_text().strip()

def token(form):
    body = urllib.parse.urlencode({**form, "client_id": CLIENT, "client_secret": client_secret})
    try:
        with urllib.request.urlopen(f"{OIDC}/token", body.encode(), timeout=30) as answer:
            return json.load(answer)["access_token"]
    except urllib.error.HTTPError as refusal:
        sys.exit(f"the token endpoint answered {refusal.code}")

verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
print("Open this address and sign in; the page then fails, copy its address:", file=sys.stderr)
print(f"{OIDC}/auth?" + urllib.parse.urlencode({
    "client_id": CLIENT, "redirect_uri": REDIRECT, "response_type": "code", "scope": "openid",
    "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
}), file=sys.stderr)
pasted = getpass.getpass("Pasted address (not shown): ", stream=sys.stderr)
query = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
if query.get("state") != [state] or "code" not in query:
    sys.exit("the address holds no code for this sign-in")
person = token({"grant_type": "authorization_code", "code": query["code"][0],
                "redirect_uri": REDIRECT, "code_verifier": verifier})
print(person)
print(token({"grant_type": "client_credentials"}))
PY
)" > ~/.secrets/agent-service/smoke-tokens )
```

The workload token lives five minutes at most (the service refuses a longer one), so run the next
block at once; `unauthorized` means the tokens expired, so repeat the first block. `forbidden` means
the person is not in `/ki-abo` or the two tokens do not share an `azp`. This second block runs, in
this order: the recreate of the service (a fresh cgroup), the idle memory, the paid turn (L2), the
memory peak right after it (L7), the cancelled turn (L4), the checks of L3, L4 and L6, and the
shred. Run it in the same shell, from `~/git/edge`:

```sh
smoke_run() {
  local tokens=~/.secrets/agent-service/smoke-tokens held=~/.secrets/agent-service/smoke-cancel-output
  docker compose --profile agent-service up -d --force-recreate agent-service
  for _ in $(seq 120); do
    docker compose logs agent-service 2>&1 | grep -q 'Uvicorn running' && break
    sleep 1
  done
  if ! docker compose logs agent-service 2>&1 | grep -q 'Uvicorn running'; then
    echo "the service is not listening after 120 s; read: docker compose logs agent-service" >&2
    shred -u "$tokens"; return 1
  fi
  cgroup() { docker compose exec -T agent-service cat "/sys/fs/cgroup/$1"; }
  clis() {
    local count
    count=$(docker compose exec -T agent-service sh -c \
      'file=$(mktemp) && cat /proc/[0-9]*/cmdline > "$file" && tr "\0" "\n" < "$file" | grep -c "[c]laude"; status=$?; rm -f "$file"; test "$status" -le 1') \
      && [[ $count =~ ^[0-9]+$ ]] && echo "$count" || echo "SAMPLE FAILED"
  }
  echo "idle memory.current: $(cgroup memory.current)"
  first=$(docker compose run --rm -T agent-smoke < "$tokens"); status=$?
  echo "$first"; echo "the smoke exited (0): $status"
  echo "memory.peak after the turn: $(cgroup memory.peak)"
  echo "CLI processes before the cancelled turn (0): $(clis)"
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker compose run --rm -T agent-smoke --cancel < "$tokens" > "$held" &
  smoke=$!
  for _ in $(seq 60); do grep -q 'holding the stream' "$held" && break; sleep 0.5; done
  echo "CLI processes while the stream is held open (1 or more): $(clis)"
  wait "$smoke"; status=$?; echo "the cancelling smoke exited (0): $status"
  second=$(cat "$held"); echo "$second"
  sleep 5
  echo "CLI processes left after the cancel (0): $(clis)"
  echo "ERROR lines since the cancel (0):" \
    "$(docker compose logs --since "$since" agent-service | grep -c ERROR)"
  secrets=(-f "$tokens" -f ~/.secrets/agent-service/claude/token -f ~/.secrets/keycloak/clients/songmaker)
  echo "secrets in docker inspect (0):" \
    "$(docker inspect "$(docker compose ps -q agent-service)" | grep -c -F "${secrets[@]}")"
  echo "secrets in the service log (0):" "$(docker compose logs agent-service | grep -c -F "${secrets[@]}")"
  echo "secrets in the smoke output (0):" "$(printf '%s\n%s\n' "$first" "$second" | grep -c -F "${secrets[@]}")"
  shred -u "$tokens"; rm -f "$held"
}
smoke_run
```

Each `(0)` is the expected figure; `grep -c` prints 0 and exits 1 when nothing matches, which is the
good answer, and a sample that failed prints `SAMPLE FAILED`, never 0. What the output proves (edge#214):

- **L2, the paid turn.** `first` holds `event: run_started`, `event: assistant_text`, the answer and
  `event: succeeded` (the smoke exited 0, printed right after it). `run_started` after admission also shows the issuer's
  keys were fetched through `agent-egress`, the service's only route out (L3).
- **L4, the cancel (an observation, not a pass condition).** `--cancel` starts a long answer, holds
  the stream open for three seconds after `run_started`, reads for half a second and then closes it.
  The smoke exits 1 with "L4 is not proven" when a `succeeded`, `failed` or `cancelled` frame was
  waiting by then or the service had closed the stream, because then no run was left to cancel;
  that exit is recorded beside the counts, not read as a verdict. The block counts the processes
  whose command line holds `claude` before the turn (0), while the stream is held open (1 or more: a
  CLI child of the run exists) and five seconds after the smoke exited (0: the service ended it), and
  counts the log's `ERROR` lines since the turn (0, no quarantine). The hold and the sample are a few
  seconds apart, so a count of 0 during the hold means the child was not up yet: repeat the block.
- **L6, no token anywhere.** The three searches look for the person's and the workload's token, the
  login token and the `songmaker` client secret in `docker inspect`, the service log and the
  smoke output.
- **L7, memory.** Report both figures: `memory.current` of the idle, freshly recreated service, and
  `memory.peak` right after the turn. The second is the peak of a fresh container across its one
  turn, an upper bound of the answer phase (it includes the start). They feed agent-providers 12r.

**L5, the egress cells against the live container.** `docker compose exec` into the running service
only reads: nothing is written, nothing is started. The program below drives the cells E6, E7
(docker0, the internet bridge and the LAN address unreachable; no gateway on `agent` and `egress`;
an unused address on each unreachable; no default route), E8, E9 and E10's resolution part of #189,
as `probe/agent-service/egress.py` does, and prints `held` or `FAILED` per cell (exit 1 on any
`FAILED`). The host looks up the addresses, because the container has no DNS and no Docker:

```sh
project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' \
  "$(docker compose ps -q agent-service)")
network() { docker network ls -q --filter "label=com.docker.compose.project=$project" \
  --filter "label=com.docker.compose.network=$1"; }
gateways() { docker network inspect "$1" | python3 -I -c 'import json, sys
configs = json.load(sys.stdin)[0]["IPAM"]["Config"] or []
print(" ".join(config["Gateway"] for config in configs if config.get("Gateway")))'; }
provider=$(getent ahostsv4 api.anthropic.com | awk 'NR==1 {print $1}')
docker0=$(gateways bridge)
internet=$(gateways "$(network internet)")
lan=$(python3 -I -c 'import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
    route.connect(("1.1.1.1", 53)); print(route.getsockname()[0])')
unused() { docker network inspect "$(network "$1")" | python3 -I -c 'import ipaddress, json, sys
network = json.load(sys.stdin)[0]
owned = {ipaddress.ip_interface(member["IPv4Address"]).ip for member in network["Containers"].values()}
subnet = ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
print(next(host for host in subnet.hosts() if host != subnet.network_address + 1 and host not in owned))'; }
docker compose exec -T agent-service python -I - "$provider" "$docker0" "$internet" "$lan" \
  "$(unused agent)" "$(unused egress)" "$(gateways "$(network agent)")" "$(gateways "$(network egress)")" <<'PY'
import errno, socket, sys

UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL}
PUBLIC_V4, PUBLIC_V6 = "1.1.1.1", "2606:4700:4700::1111"
provider, docker0, internet, lan, agent_free, egress_free, agent_gateways, egress_gateways = sys.argv[1:9]
failed = False

def report(cell, held, detail):
    global failed
    failed = failed or not held
    print(f"{cell} {'held' if held else 'FAILED'}: {detail}")

def tcp(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as attempt:
        attempt.settimeout(8)
        return attempt.connect_ex((address, port))

def udp(address, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as attempt:
        try:
            attempt.connect((address, port))
        except OSError as error:
            return error.errno
    return 0

def name(code):
    return errno.errorcode.get(code, str(code))

codes = {target: tcp(target, 443) for target in (PUBLIC_V4, provider)}
report("E6", all(code in UNREACHABLE for code in codes.values()),
       "TCP to " + ", ".join(f"{target}:443 {name(code)}" for target, code in codes.items()))

with open("/proc/net/route") as table:
    defaults = [row.split()[0] for row in table.read().splitlines()[1:] if row.split()[1] == "00000000"]
targets = {"docker0": docker0, "the internet bridge": internet, "the LAN address": lan,
           "an unused address on agent": agent_free, "an unused address on egress": egress_free}
codes = {label: tcp(target, 22) for label, target in targets.items()}
report("E7", not defaults and not agent_gateways and not egress_gateways
       and all(code in UNREACHABLE for code in codes.values()),
       f"default routes {defaults}; gateways of agent '{agent_gateways}' and egress '{egress_gateways}'; TCP to "
       + ", ".join(f"{label} {targets[label]}:22 {name(code)}" for label, code in codes.items()))

with open("/proc/net/if_inet6") as table:
    interfaces = sorted({row.split()[-1] for row in table if row.strip()})
code = tcp(PUBLIC_V6, 443)
report("E8", interfaces == ["lo"] and code in UNREACHABLE,
       f"IPv6 interfaces {interfaces}; TCP to [{PUBLIC_V6}]:443 {name(code)}")

codes = {port: udp(PUBLIC_V4, port) for port in (443, 53)}
report("E9", all(code in UNREACHABLE for code in codes.values()),
       "UDP to " + ", ".join(f"{PUBLIC_V4}:{port} {name(code)}" for port, code in codes.items()))

def resolved(host):
    try:
        return sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443)})
    except socket.gaierror:
        return []

outside, gateway = resolved("example.com"), resolved("agent-egress")
report("E10", outside == [] and gateway == ["172.31.252.10"],
       f"example.com resolves to {outside}; agent-egress to {gateway}")
sys.exit(1 if failed else 0)
PY
```

E7 holds when the default routes are `[]`, both gateway lists are empty and each of the five TCP
attempts ends in `ENETUNREACH`, `EHOSTUNREACH` or `EADDRNOTAVAIL`; `ECONNREFUSED` or a timeout means
the address is reachable, and it prints `FAILED`.

E12, the gateway's own listeners, is read from a throwaway container that shares the gateway's
network namespace and runs as nobody with no capability (it writes nothing); it takes the image
from the running service:

```sh
docker run --rm --network "container:$(docker compose ps -q agent-egress)" --read-only \
  --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python \
  "$(docker inspect --format '{{.Config.Image}}' "$(docker compose ps -q agent-service)")" -I -c '
import socket
for table in ("tcp", "udp", "tcp6", "udp6"):
    for row in open(f"/proc/net/{table}").read().splitlines()[1:]:
        fields = row.split()
        if table.startswith("tcp") and fields[3] != "0A":
            continue
        address, port = fields[1].split(":")
        raw = bytes.fromhex(address)
        if table.endswith("6"):
            shown = socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
        else:
            shown = socket.inet_ntoa(raw[::-1])
        print(table, shown, int(port, 16))'
```

It must print `tcp 172.31.252.10 443` and, besides that, only lines for Docker's embedded resolver
on `127.0.0.11` (which no other container reaches): no other TCP listener, no UDP socket on
`172.31.252.10` and no IPv6 listener or socket (the gateway has no IPv6 address, so `tcp6` and
`udp6` print nothing). E3-E5 and E10's packet windows need the probe's host capture and stay with
`egress.py`.

Proof: `flock -o /tmp/probe-stack.lock timeout --kill-after=50 1800 python3
probe/agent-service/unit.py --scratch DIR` (DIR must already exist, empty and writable, for example
from `mktemp -d`) starts the real service in a throwaway project with a
dummy login and drives the cells U1 to U11: it listens and answers 401 without tokens; one worker
under docker-init; uid, capabilities, `no-new-privileges`, core limit, non-dumpable, `/proc` and
the AppArmor profile; a process group killed with its descendants; the tmpfs ownership, modes and
`noexec`; a read-only login directory; a renewal seen at once with the start time unchanged; and
the three refusals (a working root of mode 0750, a missing login directory, a token at mode 0644);
and U15: with the directory but no token, `up -d` exits 0 and `hal apply`'s own observation finds
the service not running. U12 is `egress.py --target service`; CI holds the static facts (U13) in
`tests/test_agent_service_unit.py`. `unit.py --scratch DIR --cell smoke` drives U14: the
`agent-smoke` of the runbook against the service with a stand-in issuer, so the tokens verify and
the CLI meets the real upstream with the dummy login (`run_started`, then `failed`, no token in any
output). L4 is proven only live, with the real login; the probe has no L4 cell.

## Operator steps A8 (going live)

The operator does these himself, in this order; an agent never applies them.
Each step must hold before the next one starts.

1. **Presentator's Access app.** Check that `presentator.hallucinai.de` keeps
   its own Access app (its people, plus the `/sources/*` bypass) and still
   answers 401 to a request without a session.
2. **DNS and tunnel lines.** They come from the catalog through the publish
   of `bin/edge apply` (`edge external apply`, see
   [docs/external.md](docs/external.md)); `auth` goes live as
   `public`. There is no Zero Trust for now (operator, 20.10.2026), so no
   wildcard Access app and no bypass app exist; the wildcard `*` protection is
   X8b on #30, which turns `auth` into a bypass then.
