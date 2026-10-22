# Keycloak (auth)

Reference and explanation for the operator and for an engineer changing the Keycloak service. The
code is the truth: `compose.yaml` (service, labels, pins, secrets), `keycloak/master.yaml` and
`keycloak/realm.yaml` (the realms), `keycloak/go-live` (`--help` lists its steps) and
`probe/keycloak/` (what the service must keep doing). The platform picture is
[architecture.md](architecture.md).

## In plain words

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
`keycloak/go-live` (see [Go-live](#go-live)), and `bin/edge apply`
publishes the public door afterwards. The sections below are the reference.

## Doors, issuers and configuration

The one login for the apps: Keycloak with its own Postgres (`keycloak-db`,
volume `keycloak-db`), both on the `keycloak` network (`172.31.253.0/24`, which
`KC_PROXY_TRUSTED_ADDRESSES` names). Traefik reaches Keycloak on port 8080 in
two ways; the routing rules are the labels of the `keycloak` service in `compose.yaml`:

| Entrypoint | Host | Admits |
|---|---|---|
| `edge` (`127.0.0.1:8081`, the tunnel target) | `auth.<EDGE_DOMAIN>` | the `hallucinai` realm, its resources and the versioned static resources only; everything else, `/resources/master/…` included, is Traefik's 404, except the bare `/`, which redirects (302) to the realm's account page |
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
clients are in `realm.yaml`, each secret the file `~/.secrets/keycloak/clients/<clientId>`
(`keycloak/go-live` makes it; presentator's one client serves `presentator` and
`presentator-talk`); the other app clients arrive at K13/K14.

songmaker and presentator are also the workload identity of the agent service (A1d, #183): their
service accounts are the only users `realm.yaml` lists, each holding the realm role
`agent-workload` and no group, and their default scopes add the `agent-service` audience. Whoever
holds such an app's client secret can therefore get a workload token for that app, which is the
design; the secret stays the app's own file. hopin has no service account.

### Theme and language

Keycloak's login and account pages use the realm theme `hallucinai`
(`loginTheme` and `accountTheme` in `realm.yaml`). Its files sit in
`keycloak/themes/hallucinai/{login,account}`; compose mounts the directory
read-only into the `keycloak` service, so a change of look changes theme files
only, never the realm. The email and admin themes, the master realm and the
admin console stay Keycloak's own.

The themes give both pages the look of hallucinai.de, always dark, with the
violet main button of songmaker's vocabulary (`docs/design/keycloak-theme.html`
is the picture): the wordmark, hallucinai's colours and shapes over
Keycloak's PatternFly, Oswald and Open Sans carried in the theme (songmaker's
font files and their licences, so no page asks a third party), the favicon,
and Impressum and Datenschutz in the sign-in card. They are CSS, `footer.ftl`
and message texts on top of `keycloak.v2` and `keycloak.v3`, never a copy of
Keycloak's `template.ftl`. A Keycloak update re-checks `content.json` (a copy
of keycloak.v3's navigation list) and the PatternFly selectors at both
widths. The
account console hides its "Gruppen" page (its own `content.json`) and the
sentences under titles and sign-in methods; its "Anwendungen" lists only apps
a person has used, because the app clients carry `alwaysDisplayInConsole:
false` in `realm.yaml`. What Keycloak does not let a theme change stays
Keycloak's: the fields and paths the realm offers (no registration, no
"Passwort vergessen?", no mail: the operator resets passwords in the admin
console), the account console's layout and its pages, the browser's passkey
window and the code page's content.

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

## Secrets

No secret is in any file of this repository or in any container's `Config.Env`. Compose mounts
the files from `${HOME}/.secrets/keycloak/` (the database password, the password of the tool that
applies the configuration, and the temporary admin's password); Postgres reads its password through
`POSTGRES_PASSWORD_FILE`, and `keycloak/entrypoint.sh` exports the others from `/run/secrets` into
the process and `exec`s Keycloak or keycloak-config-cli. Each confidential app client has one more
file, `~/.secrets/keycloak/clients/<clientId>`; `compose.yaml` names them as top-level secrets.
The files are 0644 inside their 0700 directories, because the container users must read them.
`keycloak/go-live` creates the three base files only when none exists; when only some exist it
refuses and names the missing ones, except that a `bootstrap-admin-password` that step 7 retired
counts as done. Each missing client file is created, so a later app that adds one secret to
`compose.yaml` gets only its file at the next go-live. It never overwrites a file.

`keycloak/secret-check/secret_check.py` keeps secrets out of the repository's YAML (the file's
header and `tests/test_secret_check.py` hold its rules); CI runs it. After changing the script's
dependencies run `uv lock --script keycloak/secret-check/secret_check.py` (not `uv lock`). The
quality gate (`quality / hygiene`) additionally scans the whole history for literal-looking
secrets.

## Start and apply

Start the stack as in [stack.md](stack.md) (Start, readiness, stop; Keycloak comes with `edge
apply`), and wait until Keycloak answers the master realm on 8090. A realm change reaches it
through `hal apply`: merge the change to `keycloak/realm.yaml` or `keycloak/master.yaml`, update the
live checkout, run `hal apply`.

`hal apply` runs the realm step after the unit switches, when Keycloak is on, set up and running
and an input changed: `keycloak/realm.yaml`, `keycloak/master.yaml`, `keycloak/entrypoint.sh` or
config-cli's resolved definition (the image pin and the `IMPORT_*` switches) differ from what `applied.json` recorded
after the last step, or Keycloak's own import-checksum attributes differ from the recorded ones (a
restored database, a go-live import). Unchanged inputs mean the step does nothing: no backup and
no run. A rotated client secret alone does not trigger it, because Compose's definition hash
ignores the content of a secret file; change a realm file or wait for the next realm change.

The plan names the step in one line. After your yes it takes a Keycloak-only backup tagged
`pre-apply` (the last ten are kept) and restore-tests that snapshot, observes both realms, runs
config-cli, observes again and prints one line per change, with the seconds the backup and the
restore test took. A failed backup or restore test applies nothing and ends the run partial with
exit 1, as does a Keycloak that should run and cannot be read ("realm: Keycloak could not be read;
not checked"). `hal backup` runs in a process group of its own, so Ctrl-C does not reach it. A step
that outlives its time or is interrupted sends it SIGTERM, as a systemd stop does, and waits two
minutes for its cleanup, which a further Ctrl-C does not shorten; only then it kills the group. A
failure after config-cli started ends the run with exit 1 and names the snapshot; the way back is
[Restore Keycloak](backup.md#restore-keycloak), and the next `hal apply` repeats the step. A console
change to a declared object that the apply resets is printed as "reset to the file" with the
snapshot ID, exit 0. Keycloak that is switched off or disabled is never started by the step: the
plan says its realm files wait. Keycloak that starts in the same run is checked by the next `hal
apply`.

The apply signs in as the `config-cli` service client and fails loudly on a `$(env:…)` variable
that is not set (`IMPORT_VARSUBSTITUTION_UNDEFINEDISERROR`). Whatever exists only in Keycloak
stays; deleting a group, client or role the YAML no longer lists is done in the admin console, with
your yes, after a backup. After the change lines the step prints one line per group, client,
client scope, realm role, top-level flow and identity provider that the files do not declare
(`realm: only in Keycloak: hallucinai group /console-made`), or `realm: nothing only in Keycloak among groups, clients, client scopes, realm roles, top-level flows and
identity providers`, and one line naming the kinds it cannot read (`defaultGroups`, `workflows`
and the like, as the realm files name them). Keycloak's
own objects are left out: flows by their `builtIn` flag, the rest by a list pinned to Keycloak
26.8.1 in `src/edge/realm/reconcile.py`, which the probe's proof 3a keeps true in both
directions: a fresh import holds nothing only in Keycloak, and holds every pinned entry. The list appears
only when the step runs and never changes its outcome. A console change to a declared object is
reset by the next apply that runs the step, not by every apply.

## Go-live

`keycloak/go-live`, run from the checkout, as the operator, takes Keycloak live once: secrets,
first start and apply, your own admin with a second factor, and the retirement of the temporary
admin. An agent never runs it against the live project and never holds an admin credential.
`keycloak/go-live --help` lists its steps, each skipped when already done, so it can be run again
after any interruption; a failure ends with one line naming the step and exit code 1, and it never
prints a secret. Your own part is in the browser (step 7): a machine must not hold your second
factor. `edge apply` publishes `auth.<EDGE_DOMAIN>` afterwards, once the catalog declares it;
`fleet.yaml` names `keycloak/go-live` as Keycloak's setup, so `edge apply` asks and runs it in
your terminal on yes while it is not done.

While `~/.local/state/edge/stopped/keycloak` exists (`edge stop keycloak`), go-live refuses and
starts nothing; `edge start keycloak` ends the switch. `keycloak/go-live --check` asks nothing,
changes nothing and ignores the marker: its exit codes are in `--help`. It reads go-live's durable
trace, not Keycloak, and it is also the setup check of `edge apply --closed` for the unit
`keycloak`.

## Patch upgrade

1. **Back up the database** with `hal backup` (the dump holds signing keys and
   credential hashes; the backup keeps it encrypted, see [backup.md](backup.md)). It must end
   with exit 0.
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version and digest), and of
   keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe.** It runs beside the live edge, which stays up:
   [probe/keycloak/README.md](../probe/keycloak/README.md).
4. **Start** as in [stack.md](stack.md) and wait until Keycloak answers the master realm on 8090;
   Keycloak migrates its database on the first start of the new version.

## Probe

`probe/keycloak/probe.py` proves the service on a throwaway compose project (`edge-kc-probe`)
built from this checkout; [probe/keycloak/README.md](../probe/keycloak/README.md) holds the
command, the modes, the proofs, the exit codes, how to stop it and how to remove a stack it left.
