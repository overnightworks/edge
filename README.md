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
| `step-ca/` | the internal CA: its `ca.json` and `step-ca/init`, the operator's tool for its keys (see [Interne CA](#interne-ca)) |
| `traefik-mcp.yml`, `mcp/dynamic/` | the second Traefik for the MCP origins: its static configuration and one router file per app (see [traefik-mcp](#traefik-mcp)) |
| `probe/step-ca/` | the probe of the internal CA and of `traefik-mcp`, re-run for every step-ca or Traefik upgrade |
| `agent-service/egress/` | the third Traefik, `agent-egress`: the agent service's SNI-allowlisting gateway, its static file and the one file of allowed names (see [agent-egress](#agent-egress)) |
| `agent-service/service.toml` | the agent service's configuration, a read-only bind of the `agent-service` service (see [The agent service](#the-agent-service)) |
| `probe/agent-service/` | the probes of the image, of the egress gateway and its packet windows, and of the service unit, re-run for every Traefik, Docker or agent-providers upgrade |
| `tunnel/` | the cloudflared unit, its drop-ins (`20-nonroot.conf`, the boot skip `30-edge-global-stop.conf`) and the stop-only polkit rule, with `@HOME@`, `@OPERATOR_USER@` and `@TUNNEL_ID@`; edge ships them, overnightworks/home-tools#9 installs them (see [Notschalter](#notschalter-edge-stop-und-edge-start)) |
| `fleet.yaml` | the service catalog: which units of this repository run (`services.<unit>.run.enabled`), and the OpenTofu inputs (see [Dienste an und aus](#dienste-an-und-aus)) |
| `src/edge/`, `external/` | the `edge` command (`edge apply`, which is `edge apply --closed` and then the publish, `edge stop` and `edge start`, `edge external plan` and `apply`) and the pinned OpenTofu root it plans (see [Dienste an und aus](#dienste-an-und-aus), [Externe Dienste als Code](#externe-dienste-als-code)) |
| `src/home_tools/`, `docs/home.md`, `bin/home` | the `home` workstation tool (German, see [docs/home.md](docs/home.md)); `bin/home` is a symlink to the one launcher `bin/edge` |
| `src/hal/`, `bin/hal` | the `hal` door of the workstation (see [Running hal](#running-hal)); `bin/hal` is a symlink to the launcher `bin/edge` |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see below) |
| `src/edge_backup/`, `backup/apps.toml`, `systemd/` | the `edge-backup` runner, the declared apps, its user timers (see below) |
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

## Running edge

`~/git/edge/bin/edge` runs the `edge` command of the checkout it lives in, from
any directory: it syncs that checkout's locked dependencies (`uv sync --locked`,
never rewriting `uv.lock`) and then becomes `.venv/bin/edge`, from the same
environment the systemd units call. A failed sync ends with exit 2 and names the
checkout.
It is not on PATH: type `hal` (see [Running hal](#running-hal)).

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
form are implemented in `src/hal/grammar.py`.

### Exception E: where the five stack commands deviate

Until slice H5a2 of overnightworks/edge#207 (after H5e) classifies the stack's outcomes in their
owners, `hal apply`, `stop`, `start`, `offline` and `online` end with 0, or with 3 for every
outcome of the stack that is not "applied or nothing to do". They deviate from the table above in
exactly three ways:

- **E1:** a guard refusal decided by the stack ends with 3, not 4: no terminal, the apply lock is
  held, `yes` declined, the unit is disabled or not set up, a barrier is unproven, or the checkout
  is not the live one or not a clean `main`.
- **E2:** a Ctrl-C that the stack catches ends by what the stack then observes, not with 130.
  `apply`, `stop` and `start` print their `interrupted …` line and end with 3. `offline` drops the
  interrupt and reports the tunnel as observed: 3 with its `partial …` line while the tunnel still
  runs, 0 when it did stop, the marker is recorded and the boot drop-in is present. A Ctrl-C the
  stack does not catch ends with 130.
- **E3:** the stack's lines keep their form `edge <verb>: …`, not `error:`/`next:`, and name `edge`
  commands until H5e renames them; a number in such a line (for example `exit 1` in
  `edge apply: the publish did not end clean (exit 1)`) is edge's, not hal's process exit. Type
  instead:

  | The line names | Type instead |
  |---|---|
  | `edge stop <unit>` / `edge start <unit>` | `hal stop <unit>` / `hal start <unit>` |
  | `edge stop --all` | `hal offline` |
  | `edge start --all` | `hal online` |
  | `edge apply [--closed]` | `hal apply [--closed]` |
  | `edge external check --hosts` | `~/git/edge/bin/edge external check --hosts` (no hal verb until H5c) |

`hal` is on PATH; `edge` is not. `~/git/edge/bin/edge` keeps working.

## Configure

The first `edge apply --closed` writes `.env` itself: `EDGE_DOMAIN` is `fleet.yaml`'s
`cloudflare.zone` and `DOCKER_GID` the id of this PC's `docker` group (Keycloak's secrets are files,
see [Keycloak](#keycloak-auth)). `.env` is local and never committed. A `.env` that differs, or
lacks one of the two keys, is named and left alone: delete it and `edge apply --closed` writes it
again.

## Start, readiness, stop

```sh
# Starts a core (socket-proxy, traefik) that is not running; edge apply never starts it.
# `--no-recreate` leaves a running core service as it is, for the apply below to follow.
docker compose up -d --no-recreate socket-proxy traefik
# Keycloak and its database sit behind the profile `keycloak`: a plain `up -d` starts neither.
# `edge apply` (its local part, `--closed`) switches them on or off by
# `services.keycloak.run.enabled` in fleet.yaml; when it switches them on and
# `keycloak/go-live` is not done, it asks and runs it in your terminal.
# fleet.yaml is edited through a pull request and reaches the live checkout by the pull below
# (until S2.4c); a local edit leaves the tree dirty, and the apply refuses it, naming the path.
git -C ~/git/edge pull --ff-only
edge apply
# Traefik reads traefik.yml (its entrypoints) only when it starts. When traefik.yml changed
# since it was last applied, or the compose definition of socket-proxy or traefik changed
# (compose.yaml, an image pin, EDGE_DOMAIN in .env), the same `edge apply` recreates
# the running core services concerned, socket-proxy first and traefik after it, proves every
# barrier again and only then says so, ending in "the core is current"; a stopped one is named
# and not started, so start it with the `up` above first. A core container edge's failed
# recreate left created and never started is no stopped one: applied.json says so, and the
# next apply recreates it again; one made by `docker compose create` is a stopped one.
# traefik-mcp follows traefik-mcp.yml and its own compose definition the same way, after the
# core: while step-ca is on it alone is recreated (step-ca keeps its container and certificate);
# while step-ca is off or switched off it is only named and nothing starts.
```

Switching Keycloak off (`enabled: false` through a pull request, `git -C ~/git/edge pull
--ff-only`, then `edge apply`) stops and removes
`keycloak-db` and `keycloak` and keeps the volume `keycloak-db`, `~/.secrets/keycloak` and
the `keycloak` network; switching it on again uses the same database. Removing the entry
from `fleet.yaml` is not "off": apply then leaves Keycloak as it is. Whether `auth` and
`127.0.0.1:8090` answer depends on Traefik, which `edge apply` recreates only for a changed
`traefik.yml` or compose definition and never starts.

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

Stop: `docker compose --profile keycloak --profile probe down`. A `down` without
`--profile keycloak` leaves Keycloak and its database running, and one without
`--profile probe` leaves a running probe `hello` behind. Neither removes a volume; never add `--volumes`,
because that deletes the Keycloak database.

After a reboot, socket-proxy, traefik, keycloak-db and keycloak come back on
their own (`restart: unless-stopped`), also without the profile on the command line;
`hello` never does.

What the entrypoint answers without a route:

- unknown host or invalid name: `404`;
- HTTP/1.1 request without a `Host` header: `400` (Go's HTTP server rejects it);
- HTTP/1.0 request without a `Host` header: `404`.

## The probe profile

`hello` (traefik/whoami, labelled `edge.name=hello`) lives under the compose
profile `probe`, so a plain `docker compose up -d` never starts it.
`hello-mcp` (the same image on `mcp-backends`, the MCP origin `hello.mcp.internal`) lives there too.

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

## Externe Dienste als Code

Cloudflare wird mit OpenTofu als Code verwaltet (`external/`; GitHub folgt mit edge#30 X2/X3);
wo diese Schicht liegt, steht in [`docs/architecture.md`](docs/architecture.md).
Der State liegt nie im Checkout, sondern verschluesselt in
`~/.secrets/opentofu/fleet.tfstate`.

`edge external plan` zeigt, was von den Live-Diensten abweicht (Exit 0 keine
Aenderung, 1 Aenderungen offen, 2 nicht moeglich). Es liest zwei Dateien (erwartet
wird je 0600, geprueft wird das hier nicht) und gibt sie nur an das Kind `tofu` weiter
(die eine Ausnahme steht bei `check --hosts` unten), dessen Umgebung eine
Allowlist ist (kein `TF_LOG*`, `TF_CLI_ARGS*`, `TF_ENCRYPTION`, `GITHUB_*`,
`GH_*`, Proxy aus der Shell):

| Datei | Variable im Kind |
|---|---|
| `~/.secrets/cloudflare/fleet-read` | `CLOUDFLARE_API_TOKEN` |
| `~/.secrets/opentofu/state-passphrase` | `TF_VAR_state_passphrase` |

Die GitHub-Tokens (`~/.secrets/github/fleet-read`, `fleet-admin`) bleiben
bekannt, werden aber nicht gelesen und nicht weitergegeben, bis edge#30 X2/X3
die GitHub-Einstellungen als Code bauen. Die weiteren Token-Dateien der Schicht
folgen mit X5b (home-tools#10).
`plan` verweigert ein `tofu`, dessen Version nicht der Pin in
`external/main.tf` entspricht.

```bash
bin/edge external plan
```

`edge external check --hosts` liest dagegen nur `~/.secrets/cloudflare/fleet-read`
und sendet es im `Authorization`-Header an `api.cloudflare.com`: kein `tofu`,
kein GitHub-Token, keine State-Passphrase, kein Schreib-Token, kein Terminal
und keine Abfrage (das Token steht in keiner URL, in keiner Ausgabe und in
keinem Fehlertext; Weiterleitungen und Proxy aus der Shell werden nicht
befolgt). Es liest Zone, DNS-Records, Access-Apps, Access-Gruppen und den
Tunnel und vergleicht sie mit den Hosts aus `fleet.yaml` (aus dem Checkout,
in dem es laeuft): fehlende oder falsche Records und Apps, falsche Policies,
unerwartete Apps an oeffentlichen Hosts sowie Records und Apps, die niemand
deklariert hat (`apply` loescht sie nie; er endet auf demselben Check). Exit 0 keine Befunde, 1 Befunde,
2 nicht pruefbar, 130 unterbrochen (nur vor der Ausgabe: sobald sie beginnt,
gilt der Exit-Code des Befunds); ohne Access auf dem Konto ist das nur
dann ein Befund, wenn ein Host `bypass` oder `protected` ist (meldet die erste
Seite der Apps-Abfrage "Access aus", entfaellt die Gruppen-Abfrage; kommt
"Access aus" erst auf einer spaeteren Apps-Seite oder bei der Gruppen-Abfrage,
nachdem die Apps geantwortet haben, widerspricht sich die Abfrage: Exit 2). Mit
`--json` ist stdout genau eine Liste von `{problem, subject, detail, next}` (bei
Exit 2 leer, die Ursache auf stderr).

Zusaetzlich fragt der Check jeden deklarierten Host mit Record einmal mit
`GET /` ueber HTTPS, ohne Anmeldedaten, ohne Cookies und ohne Weiterleitungen
(10 s je blockierender Operation; die Wildcard unter `edge-host-check.<zone>`,
nur solange Access an ist; der Name ist im Katalog reserviert). `answer-wrong`:
die Antwort hat kein `cf-ray`, ein oeffentlicher oder `bypass`-Host leitet auf
Access um, oder die geschuetzte Wildcard leitet nicht auf Access um (oder ist
eine Challenge). `unreachable`: die Anfrage scheiterte (DNS, TLS, Verbindung,
Zeitueberschreitung, fehlerhafte Antwort). Eine Challenge auf einem
oeffentlichen oder `bypass`-Host besteht. Ein 5xx mit `cf-ray` besteht: ob der
Dienst lebt, sagt `ready`, nicht dieser Check. Als Access-Umleitung zaehlt nur
Cloudflares kanonische Access-URL; jede andere Schreibweise des Ziels gilt als
keine. Kein Befund zitiert Header, Ziel oder Fehlertext.

```bash
bin/edge external check --hosts [--json]
```

`edge external apply` schreibt die Aenderungen auf die Live-Dienste, mit den
Schreib-Token `~/.secrets/cloudflare/fleet-write` (dieselbe
Allowlist-Umgebung, dieselbe State-Passphrase). Es laeuft nur aus `~/git/edge` als Haupt-Worktree auf
einem sauberen `main`, sonst Exit 2 mit dem Grund (`plan` laeuft aus jedem
Checkout), und nur an einem Terminal (stdin und stdout; sonst
Exit 1, bevor `tofu` aufgerufen wird), plant in eine Datei, zeigt den Plan
und wendet genau diese Datei erst nach der Eingabe `yes` an (`y`, `Yes`,
leer: Abbruch, Exit 1). Hat der Plan Aenderungen, fragt `apply` vor dem
`yes` jeden aktivierten Host mit `ready` (`fleet.yaml`) nach diesem Pfad auf
seinem `target` (`GET`, `Host` = FQDN, kein Redirect, 10 s, kein Retry); nur
2xx zaehlt. Antwortet einer nicht, Exit 1 vor dem `yes`: jeder
nicht antwortende Host steht mit Pfad, Ziel und Status oder Fehlerart auf stderr,
`tofu apply` laeuft nie, die Plan-Datei wird geloescht, kein Host-Check folgt.
Das gilt auch fuer eine ganz andere Aenderung, solange so ein Host nicht antwortet
(gestoppt mit `edge stop`, abgestuerzt, startet noch); `enabled: false` nimmt
den Host aus der Pruefung. Ein Plan ohne Aenderungen endet mit Exit 0 ohne
Frage. Das ist ein Schutz gegen versehentliche und nicht-interaktive Laeufe
(CI, Skript, `-auto-approve`), keine Identitaetspruefung: ein Agent kann ein
pty oeffnen und `yes` tippen; erst D14 (eigener Nutzer fuer Agenten) macht
daraus "nur der Betreiber". Die Plan-Datei liegt verschluesselt (X1a) in
`~/.secrets/opentofu/plans/` (0700) und wird nach Erfolg, Abbruch, Fehler und
SIGINT/SIGTERM geloescht; eine nach SIGKILL uebrig gebliebene Datei loescht und
nennt der naechste `edge external plan`- oder `apply`-Aufruf (`check` raeumt nicht auf). Strg-C oder SIGTERM waehrend
`apply` geht genau einmal als SIGINT an `tofu`; der Wrapper wartet, bis `tofu`
sauber beendet ist, weitere Strg-C werden ignoriert, und die Meldung verweist
auf `edge external check --hosts`, um den Live-Zustand zu pruefen (dann laeuft
die Pruefung nicht von selbst: nichts Ungeprueftes wird behauptet).

Jeder `apply`, der etwas geaendert haben kann oder nichts zu tun fand, endet
auf demselben Host-Check wie `edge external check --hosts` (Ausgabe wie dort,
ohne `--json`; er liest `~/.secrets/cloudflare/fleet-read`, das `apply` vor dem
Plan prueft: fehlt es oder ist es leer, Exit 2 ohne `tofu`; das Schreib-Token
erreicht den Check nie). Der Check vergleicht den tatsaechlichen Zustand mit
der Deklaration, nicht mit dem Plan: ein geplanter Fix entschuldigt nichts, und
ein undeklarierter Record laesst auch einen sonst fertigen `apply` mit Exit 1
enden (`apply` loescht ihn nie). Eine stderr-Zeile sagt, was geschah, z. B.
`applied; the host check has findings, see above`. Exit 0 angewendet (oder
nichts zu tun) und der Check meldet nichts, 1 verweigert, abgebrochen,
fehlgeschlagen oder die Pruefung der Hosts meldet etwas oder laeuft nicht,
2 nicht planbar (nie nach einem Apply).

```bash
bin/edge external apply
```

`apply` schreibt auch die Zeilen des Tunnels aus dem Katalog: eine je Host, die
404-Regel zuletzt. Die Konfiguration des Tunnels gehoert ganz dem Katalog: eine
Zeile von anderswo zeigt `plan` als entfernt, und sie verschwindet nach `yes`
(anders als undeklarierte Records und Apps). Das gilt, **solange Cloudflare den
Tunnel verwaltet (`config_src = cloudflare`)**. Solange er `local` ist, bedient
cloudflared `/etc/cloudflared/config.yml`, und die Zeilen, die `apply` schreibt,
bedienen nichts. Der Wechsel von `local` auf `cloudflare` ist einseitig (laut
einer Aussage der cloudflared-Maintainer). Den Wechsel macht der Betreiber
(overnightworks/edge#168); dieser Absatz verspricht nicht, dass `apply` ihn
ausloest.

`apply` setzt auch HSTS der Zone (`cloudflare_zone_setting.hsts`, `security_header`):
`max-age=31536000; includeSubDomains; preload`, auf dem Apex und jeder Subdomain. Die
Einstellung allein veroeffentlicht nichts; die Preload-Liste der Browser kennt die Domain erst
nach dem Absenden des Formulars (Schritt 4, nicht umkehrbar fuer Monate). Die Schritte danach:

0. Der Betreiber gibt im Cloudflare-Dashboard beiden Tokens (`fleet-read`: `Zone › Zone Settings › Read`;
   `fleet-write`: `Zone › Zone Settings › Edit`) das Recht dazu, vor `edge apply`. Das Aendern der
   Rechte behaelt den Wert des Tokens, an den Dateien aendert sich nichts.
1. `edge apply`; die Publish-Phase wendet die Einstellung an. Danach meldet `edge external plan`
   "No changes".
2. `curl -sI https://hallucinai.de/ | grep -ci '^strict-transport-security'` liefert 1, ebenso fuer
   `www` und `auth` (Gross-/Kleinschreibung egal). Sind es zwei, verwerfen zuerst die
   Ursprungsdienste ihre eigene (in den Items von webauth und Keycloak).
3. `https://hstspreload.org/api/v2/preloadable?domain=hallucinai.de` meldet keinen Fehler.
4. Der Betreiber sendet das Formular auf hstspreload.org ab.

Zum Zuruecknehmen vor Schritt 4 (dem Absenden) setzt du `enabled = false` und wendest es an; den Block zu loeschen ist nicht belegt als Zuruecksetzen der Einstellung bei Cloudflare. Nach dem Absenden reicht `enabled = false` nicht, denn die Domain bleibt auf den Preload-Listen der Browser, bis sie entfernt ist: dazu den Header ohne `preload` ausliefern, das Formular auf https://hstspreload.org/removal/ absenden und fuer die Monate bis zur Entfernung gueltiges HTTPS auf jeder Subdomain halten; Browser, die den Header gesehen haben, behalten ihn bis zum Ablauf seines max-age.

`apply` setzt ausserdem eine Request-Header-Transform-Regel der Zone
(`cloudflare_ruleset.client_address`, Phase `http_request_late_transform`): `Forwarded` ist die
Adresse, die Cloudflare sah (`for=<IPv4>;proto=https`, bei IPv6 `for="[<IPv6>]";proto=https`),
ein vom Besucher gesendetes `Forwarded` wird ueberschrieben. Keycloak liest nur diesen Header
(`KC_PROXY_HEADERS=forwarded`) und speichert in seinen Ereignissen (Anmeldungen und
fehlgeschlagene Anmeldungen, 90 Tage) deshalb die Adresse des Besuchers, nie einen vom Client
gefaelschten `X-Forwarded-For`. Apps, die `X-Forwarded-For` loggen, bleiben faelschbar; jede liest
`Forwarded` oder `Cf-Connecting-IP` selbst.

Bei IPv6 speichert Keycloak die Adresse mit den Klammern des Headers (`[2001:db8::1]`).

Die Reihenfolge der Umstellung: erst `hal apply`, dann Keycloak. `hal apply` erstellt Keycloak bei
einer geaenderten Umgebung nie neu (`up -d --no-recreate`); der Betreiber startet es erst, wenn
die Regel bei Cloudflare steht: `docker compose --profile keycloak up -d --no-deps keycloak`, in
`~/git/edge`, und nur wenn `hal apply` sauber endete **und** `edge external plan` "No changes"
meldet. So gibt es kein Fenster: Keycloak liest `Forwarded` erst, wenn davor die Regel steht. Ein
Keycloak auf `forwarded` ohne Regel davor wuerde ein vom Besucher gesendetes `Forwarded`
vertrauen.

Die Token-Rechte, vor `hal apply` im Cloudflare-Dashboard: `Zone › Transform Rules › Read`
(`fleet-read`) und `› Edit` (`fleet-write`). `Account › Account Rulesets › Read` ist nicht
gemessen: es kommt nur dazu, wenn ein 403 es nennt; ob es noetig war, steht dann auf #190.

Scheitert die Publish-Phase, bleibt Keycloak auf `xforwarded`: nicht neu erstellen, das Ergebnis
auf #190 melden. Bekannte Ursachen und der naechste Schritt:

- Cloudflare lehnt den Header ab: Option (b) oder hinnehmen, der Betreiber entscheidet auf #190.
- Cloudflare lehnt die `/0`-Listen ab: den genauen Fehlertext auf #190 schreiben.
- In der Phase besteht schon ein Ruleset: importieren und seine Regeln vorher in
  `cloudflare_ruleset.client_address` uebernehmen; der Plan darf keine Regel entfernen; sonst auf
  #190 melden.
- 403: ein fehlendes Recht (siehe oben).

Danach prueft der Betreiber (L1): eine fehlgeschlagene Anmeldung durch den Tunnel mit gefaelschtem
`X-Forwarded-For` **und** gefaelschtem `Forwarded`:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'X-Forwarded-For: 203.0.113.9, 192.0.2.7' -H 'Forwarded: for=203.0.113.9' \
  -d grant_type=password -d client_id=nobody -d username=nobody -d password=wrong \
  https://auth.hallucinai.de/realms/hallucinai/protocol/openid-connect/token
```

Das Ereignis in der Admin-Konsole (Admin-Eingang, Events) traegt seine echte Adresse.

## Dienste an und aus

`fleet.yaml` ist der Katalog. Ein Eintrag mit `run` und ohne `repo` ist eine Einheit
dieses Repositorys; sein Schluessel ist der Name des Compose-Profils (`docs/naming.md`):

```yaml
services:
  step-ca:
    run:
      enabled: true         # false schaltet das Profil `step-ca` ab
      setup: step-ca/init   # optional: der Befehl, der den Schluessel macht
```

Das blosse `edge apply` fuehrt zwei Teile nacheinander aus: zuerst den lokalen Teil
(`edge apply --closed`, alles bis zum Ende dieses Abschnitts), dann, nur wenn der mit Exit 0 endete,
die Publish-Phase (`edge external apply`, [Externe Dienste als Code](#externe-dienste-als-code)).
Jeder Teil fragt sein eigenes `yes` und nur, wenn er etwas zu aendern hat; gibt es nirgends etwas
zu aendern, fragt `edge apply` gar nichts. Endet der lokale Teil nicht mit 0 (verweigert,
abgelehnt, nicht planbar, teilweise), steht eine Zeile "nothing published", es laeuft kein tofu,
und der Exit ist der des lokalen Teils. `edge apply --closed` bleibt der lokale Teil allein.
Ein Ctrl-C vor der Publish-Phase endet mit Exit 1 und "interrupted, nothing published". Waehrend
der Publish-Phase gelten deren eigene Unterbrechungsregeln
([Externe Dienste als Code](#externe-dienste-als-code)) und ihr Exit bleibt, auch 0, sobald sie
ihr Urteil festgehalten hat; nur eine Unterbrechung, die ohne festgehaltenen Exit aus der
Publish-Phase herauskommt, endet mit Exit 1 und der Zeile "run `edge external check --hosts`". Ein
SIGTERM (`kill`) vor der Publish-Phase beendet den Prozess wie bei `--closed`: nichts wird
veroeffentlicht.

`edge apply --closed` bringt jede Einheit in den Zustand, den `enabled` sagt. An:
`docker compose --profile <Einheit> up -d` fuer genau die Dienste des Profils. Aus: `stop`, dann
`rm -f` derselben Dienste. Es nimmt nie `down`, nie `-v`, nie ein Volume weg (R1). Den Kern
(`socket-proxy`, `traefik`) erstellt es vor allen Einheiten neu (`up -d --no-deps
--force-recreate <Dienst>`, im selben Plan, mit demselben `yes`): einen Dienst, dessen
Compose-Definition sich geaendert hat (`docker compose config --hash` weicht vom Label
`com.docker.compose.config-hash` seines Containers ab), und `traefik` auch, wenn `traefik.yml`
sich seit dem letzten vollstaendigen Lauf geaendert hat oder dafuer kein Eintrag in
`~/.local/state/edge/applied.json` steht. Ein neu erstellter `socket-proxy` zieht `traefik` nach,
`socket-proxy` zuerst. Vor dem Neuerstellen leert es den Eintrag; erst wenn jeder Dienst mit
dem, wovon er abhaengt, laeuft und nach jedem Neuerstellen jede aktive Sperre bewiesen ist,
schreibt es ihn neu und meldet am Ende "the core is current"; scheitert ein Schritt,
ist der Lauf teilweise (Exit 1), keine Einheit wird geschaltet und der naechste Lauf versucht es
wieder ("traefik: recreate (the last recreate did not finish)"), auch wenn `traefik.yml`
inzwischen wieder auf dem alten Stand ist. Einen gestoppten Kerndienst
nennt es nur ("traefik: traefik.yml changed; it is not running and is not started") und startet es nie,
auch nicht, wenn er erst zwischen Plan und `yes` gestoppt wurde: dann ist der Lauf teilweise
("traefik was stopped meanwhile; it is not started", Exit 1) und keine Einheit wird geschaltet; einen, dessen
Container sein eigenes gescheitertes Neuerstellen nur erstellt und nie gestartet hat (so steht
es in `applied.json`), erstellt der naechste Lauf wieder neu ("socket-proxy: recreate (the last recreate did not finish)"). Nach dem Kern folgt
`traefik-mcp` ebenso `traefik-mcp.yml` und seiner Compose-Definition: ist `step-ca` an (aktiviert und
nicht abgeschaltet), erstellt der Lauf nur `traefik-mcp` neu (`up -d --no-deps --force-recreate
traefik-mcp`, `step-ca` behaelt Container und Zertifikat) und schreibt den Eintrag, sobald es laeuft;
ist `step-ca` aus oder abgeschaltet, nennt er es nur ("traefik-mcp: traefik-mcp.yml changed; step-ca
is off, so it is not started") und startet nichts. Schaltet ein Lauf `step-ca` an, schreibt er den
Eintrag von `traefik-mcp`, sobald die Einheit laeuft, damit der naechste Lauf nichts aendert.
Laeuft ein Dienst einer Einheit, die nicht ausgeschaltet wird, auf einem anderen Image, als
`compose.yaml` jetzt nennt (etwa nachdem ein Pull `step-ca` oder `keycloak-db` anhebt), nennt der
Plan ihn nach den Zeilen der Einheiten ("step-ca: step-ca runs <laufend>, compose.yaml names
<konfiguriert>; edge apply never swaps the image of a running unit, ..."), tauscht das Image nie
und fragt dafuer allein nichts: der Lauf bleibt `nothing to change`, das Upgrade ist deins (Block
6; Keycloak erst mit Dump, K11b). Der Befehl zeigt den Plan, laeuft nur an einem Terminal und
wendet ihn erst nach der Eingabe `yes` an (alles andere: Abbruch, Exit 1). Danach liest er die
Container noch einmal und meldet nur, was er sieht; ein zweiter Lauf meldet `nothing to change`.
Er laeuft nur aus dem Live-Checkout `~/git/edge` als Haupt-Worktree; aus einem Lane-Worktree oder
einer anderen Kopie verweigert er mit Exit 2 und nennt den Grund (die andere Kopie, "a linked
worktree" oder "no git checkout"), denn sonst haengte er das Live-Projekt an diesen Baum. Danach
prueft er, in dieser Reihenfolge und jeweils mit nur der ersten Zeile, die verweigert: kein anderes
`edge apply --closed` laeuft (sonst Exit 1; `edge stop` und `edge start` warten nie darauf), HEAD
steht auf `main` (sonst Exit 2 mit Zweig oder kurzem Hash), der Baum ist sauber (sonst Exit 2,
jeder geaenderte oder unverfolgte Pfad auf eigener Zeile; ignorierte wie `.env` zaehlen nicht) und
`.env` stimmt. Fehlt `.env`, schreibt `edge apply --closed` sie selbst (`EDGE_DOMAIN` aus
`cloudflare.zone` in `fleet.yaml`, `DOCKER_GID` aus der Gruppe `docker` dieses PCs) und sagt es in
einer Zeile; steht eine abweichende da, fehlt ihr einer der beiden Schluessel oder ist sie nicht
lesbar, haelt er mit Exit 2 an, nennt Schluessel, den Wert in der Datei und den abgeleiteten Wert
und laesst die Datei unberuehrt: du loeschst sie, `edge apply --closed` schreibt sie neu. `fleet.yaml` aenderst
du per Pull Request; in den Live-Checkout kommt er bis S2.4c mit `git -C ~/git/edge pull
--ff-only`. Git laeuft dabei ohne Hooks, ohne fsmonitor und ohne deine globale Konfiguration;
"ignoriert" heisst nur, was die `.gitignore` des Repositorys sagt, nicht deine globale
`~/.config/git/ignore`.

Danach holt er `main` von `origin` (`git fetch --no-tags --no-recurse-submodules
--write-fetch-head origin main`) und haelt sich ab da an genau diesen Hash. Nur dieser Abruf
liest deine globale Konfiguration, damit er sich wie dein `git pull` ueber deinen
`gh`-Credential-Helper anmeldet; edge liest, uebergibt und druckt nie ein Credential, fragt nie
nach einem Passwort und verweigert eine URL mit Passwort darin. Er schreibt nur Objekte,
`FETCH_HEAD` und `origin/main`; HEAD, Index und Baum bleiben, wie sie sind. Die erste Zeile nennt
die URL ("fetched main from https://github.com/..."; was vor einem `@` steht, als `<redacted>`).
Scheitert der Abruf (kein Netz, kein `origin`, Anmeldung verweigert, Zeitlimit von 120 s), endet
er mit Exit 2 und der letzten Fehlerzeile von git ("cannot fetch main from origin, ...; nothing
changed"). Hat der Checkout Commits, die `origin/main` fehlen, oder ist er daneben gewachsen,
endet er mit Exit 2 und nennt beide Hashes ("nothing reset"). Gleich: "live checkout current
(<Hash>)", dann der Plan wie oben. Dahinter zeigt er jeden wartenden Commit mit kurzem Hash und
Betreff, dann dessen Dateien nach Wirkung, gelesen aus den Binds der aufgeloesten `compose.yaml`:

```text
edge apply --closed: 2 commits wait on origin main:
edge apply --closed:   4e1f0c2 Route the new app
edge apply --closed:   9b7d3aa Pin traefik
edge apply --closed: their files, by what an update does with them:
edge apply --closed:   live once updated:
edge apply --closed:     A dynamic/app.yml
edge apply --closed:   recreated by edge apply:
edge apply --closed:     M compose.yaml
edge apply --closed:   stops for `uv sync`:
edge apply --closed:     M uv.lock
edge apply --closed:   not applied by edge apply (block 6):
edge apply --closed:     M keycloak/realm.yaml
edge apply --closed:   other:
edge apply --closed:     M README.md
edge apply --closed: the live checkout is 2 commits behind; to bring them: git -C ~/git/edge merge --ff-only 4e1f0c2...
edge apply --closed: the plan below applies HEAD 1a2b3c4, without the waiting commits
```

"live once updated" sind die Verzeichnisse, die Kern und Follower binden (`dynamic/`,
`mcp/dynamic/`): Traefik liest sie, sobald sie im Checkout stehen. "recreated by edge apply" sind
ihre Datei-Binds (`traefik.yml`, `traefik-mcp.yml`) und `compose.yaml`. "stops for `uv sync`"
sind `pyproject.toml` und `uv.lock`. "not applied by edge apply (block 6)" sind die Binds der
Einheiten (`keycloak/`, `step-ca/ca.json`). Der Plan danach wendet HEAD an, nie die wartenden
Commits; sie bringst du bis S2.4c mit der gezeigten `merge --ff-only`-Zeile selbst herein und
rufst `edge apply --closed` noch einmal auf.

Eine Einheit mit `setup` wird erst angeschaltet, wenn `<setup> --check` Exit 0 gibt. Eine im Katalog
eingeschaltete Einheit mit `setup`, die laeuft, wird bei jedem Apply geprueft: ist sie nicht eingerichtet,
bekommt sie die Setup-Frage und sperrt das Publizieren;
eine per `edge stop` ausgeschaltete, die noch laeuft und kein Setup hat, wird abgewiesen (`edge stop` noch einmal).
Fehlt das Setup, fragt `edge apply --closed` vor dem Plan eigens ("`<Einheit>` is not set up: ... Type 'yes'
to run `<setup>` now"; nur das genaue `yes` zaehlt, ein `no` bricht den ganzen Lauf ab, Exit 1,
nichts geaendert). Nach `yes` laeuft das Setup in deinem Terminal (`step-ca/init` oder
`keycloak/go-live`, ohne Argumente, ohne Zeitlimit, Ausgabe ungefiltert); die Schluessel macht
allein dieser Befehl, `edge apply` sieht sie nie. Dann prueft `--check` noch einmal, die Zustaende
werden neu gelesen und der Plan folgt mit seinem eigenen `yes`. Ein Setup, das scheitert oder per
Ctrl-C unterbrochen wird, schaltet nichts (Exit 1; "keys may already exist"); was ein Setup
gemacht hat, bleibt auch nach einem `no` zum Plan (Loeschen bleibt Handarbeit). Ohne Terminal
laeuft nie ein Setup.

"An" heisst: jeder Dienst der Einheit hat seinen Container laufen (oder ein Einmal-Dienst ist
fertig), nicht, dass er schon gesund ist und antwortet; die Gesundheit zeigt `docker compose ps`.

Exit 0 angewendet und beobachtet oder nichts zu tun, 1 verweigert (auch: ein anderes Apply
laeuft), abgebrochen, fehlgeschlagen oder nur teilweise angewendet, 2 nicht planbar (nicht aus dem
Live-Checkout `~/git/edge` gestartet, nicht auf `main`, Baum nicht sauber, `.env` abweichend, unlesbar oder nicht schreibbar, keine Gruppe `docker`,
`cloudflare.zone` ungueltig, git nicht befragbar, `main` nicht von `origin` abrufbar, Checkout vor oder neben `origin/main`, Katalog ungueltig, Compose unlesbar, `traefik.yml` oder `traefik-mcp.yml` fehlt oder
ist unlesbar, dieses auch, wenn `step-ca` aus oder abgeschaltet ist, ein laufender Container einer
Einheit, dessen `docker compose ps`-Zeile kein Image nennt, ein Setup-Check, der es nicht sagen
kann, ein Setup, das nicht starten kann). Das blosse `edge apply` (siehe unten) gibt den
Exit des lokalen Teils unveraendert zurueck, solange der nicht 0 ist; danach den der Publish-Phase,
deren Exit 2 zu 1 wird. Ein Exit 2 von `edge apply` heisst darum: der lokale Teil konnte nicht
fertig planen, es wurde nichts veroeffentlicht, und ein Setup, das schon lief, kann Aenderungen
hinterlassen haben (ein Setup laeuft vor dem erneuten Pruefen und Planen, beide koennen noch mit
Exit 2 enden). Ueber `bin/edge` kann Exit 2 auch vom `uv sync` des Starters kommen, bevor `edge`
beginnt; seine Zeile nennt das Sync.

Der Setup-Vertrag: `<setup> --check` fragt nichts, druckt kein Geheimnis, legt nichts an und
braucht keinen laufenden Dienst. Exit 0 eingerichtet, 1 nicht eingerichtet (eine Zeile sagt, was
fehlt), 2 es kann es nicht sagen (Docker oder Compose antworten nicht).

### Notschalter: `edge stop` und `edge start`

`edge stop <Einheit>` schaltet eine Einheit sofort aus und haelt sie aus: Es schreibt zuerst den
Schalter, dann `docker compose --profile <Einheit> stop` fuer genau die Dienste der Einheit, liest
die Container noch einmal und meldet "switched off" nur, wenn nichts mehr laeuft. Container und
Volumes bleiben (kein `rm`, kein `down`, nie `socket-proxy` oder `traefik`). Es braucht kein
Terminal und kein `yes`: Agenten duerfen es aufrufen (R4, D12). `edge start <Einheit>` hebt den
Schalter auf: nur an einem Terminal und nach dem exakten `yes` (der Betreiber; bis D14 eine Regel,
keine Wand). Es loescht den Schalter erst, wenn die Einheit laeuft; eine im Katalog abgeschaltete
Einheit (`enabled: false`) verweigert es. Hat die Einheit ein `run.setup`, fragt `edge start` zuerst
dessen `--check`, noch vor der Rueckfrage: Ist die Einheit nicht eingerichtet, verweigert es mit
Exit 1 (bei unklarer Antwort Exit 2) und nennt das Setup; Schalter und Sperre bleiben.

Der Schalter ist eine Datei pro Einheit, `~/.local/state/edge/stopped/<Einheit>`, mit der Zeit des
Ausschaltens (ISO 8601). `edge apply --closed` startet eine ausgeschaltete Einheit nie und
loescht ihren Schalter nie ("stays stopped"); das Ende des Schalters ist allein `edge start`. Ein
`stop`, das waehrend der Rueckfrage von `apply` kommt, gewinnt: `apply` liest den Schalter nach dem
`yes` noch einmal (`switch.lock`). Exit 0 ausgeschaltet oder schon so, 1 teilweise (der Schalter
ist notiert, aber ein Container laeuft noch, oder er liess sich nicht notieren) oder verweigert,
2 nicht planbar (unbekannte Einheit, Katalog oder Compose).

Die Sperre vor Traefik: Hat die Einheit einen Dienst mit `traefik.enable` (jede Go-ParseBool-Schreibweise von wahr) und
einem `edge.name`, schreibt `edge stop` vor dem `stop` die Datei `dynamic/switch-<Einheit>.yml` (flach,
atomar, von git ignoriert). Sie haengt pro Host ein Router mit hoechster Prioritaet nur an den
Eingang `edge`, und Traefik antwortet `404 page not found`; der Eingang `admin` auf `127.0.0.1:8090`
bleibt offen. `edge stop` meldet Erfolg erst, wenn Traefik fuer jeden Host (innerhalb von 15 s) 404
mit dem Header `X-Edge-Barrier: <Einheit>` antwortet, sonst Exit 1 (teilweise; Schalter und Sperre bleiben). `edge start` entfernt die Sperre
erst, wenn die Einheit laeuft, prueft, dass der Host den Header `X-Edge-Barrier` nicht mehr setzt, und loescht den
Schalter zuletzt. Fand `edge stop` an einer schon ausgeschalteten Einheit die Sperre fehlend und stellt
sie wieder her, sagt es "its barrier was missing and stands again". `edge apply` schreibt und entfernt die Sperre nie; nur nach einem Neuerstellen von Traefik rendert es jede aktive Sperre neu und beweist sie, bevor es Erfolg meldet. Eine Einheit, deren Sperre ohne Schalter steht, bleibt bei `edge apply` aus. Sie ist eine Datei im Checkout und gilt
nach einem Neustart von Traefik und des PCs weiter. Der Beweis auf einem Wegwerf-Stack (braucht
`127.0.0.1:8081` frei, also bei ausgeschaltetem Live-Traefik):
`flock /tmp/probe-stack.lock timeout --kill-after=30 900 python3 probe/switches/probe.py --scratch "$(mktemp -d)"`.

Ein `stopped/`-Verzeichnis, das nicht 0700 ist, setzt `edge stop` auf 0700 zurueck und notiert den
Schalter; nur ein Zustandsverzeichnis, das es nicht schreiben kann, gibt Exit 1.

Ueberleben eines Neustarts: Jeder lang laufende Dienst hat `restart: unless-stopped`; Docker
startet einen ausdruecklich gestoppten Container auch nach einem Neustart nicht. Der Schalter
bleibt auf der Platte. Eine ausgeschaltete Einheit bleibt also ohne Boot-Dienst aus.

Der globale Schalter: `edge stop --all` nimmt den PC sofort vom Internet. Es schreibt zuerst
`~/.local/state/edge/global-stop` (eine eigene Datei neben `stopped/`; ein zweites Stop behaelt die
erste Zeit), ohne auf `switch.lock` zu warten, stoppt dann `cloudflared.service`, schreibt den
Schalter unter `switch.lock` noch einmal (ein `start`, der ihn inzwischen loeschte, macht ihn nicht
rueckgaengig; scheitert dieses Schreiben und fehlt der Schalter danach, meldet es "partial: the
global switch could not be recorded", Exit 1) und meldet Erfolg erst, wenn `systemctl is-active`
den Tunnel als beendet (`inactive` oder `failed`) zeigt; ein Fehler des Stopps allein aendert das
nicht. Die Einheiten laufen lokal weiter
und ihre Schalter bleiben. An einem Terminal fragt das System selbst nach dem Passwort; ohne
Terminal (Agent) laeuft `systemctl --no-ask-password`. Exit 1 heisst teilweise: "partial", wenn
der Tunnel noch laeuft (Schalter bleibt; `sudo systemctl stop cloudflared`; eine zweite Zeile
`stop failed: <Grund>` nennt den Fehler des Stopps, etwa `cannot run systemctl (timed out)`), und "incomplete", wenn
das Boot-Drop-in `30-edge-global-stop.conf` noch fehlt (ein Neustart oeffnete den Tunnel wieder;
das bringt overnightworks/home-tools#9). `edge start --all` braucht ein Terminal und das exakte
`yes` (Agenten nie, R4). Es beweist zuerst die Sperre jeder noch ausgeschalteten Einheit (eine
Zeile je Sperre, bei Bedarf neu geschrieben, 404 mit `X-Edge-Barrier` auf `127.0.0.1:8081`), holt
die Berechtigung des Systems (`pkcheck`) noch vor dem Loeschen des Schalters, liest ihn unter der
Sperre noch einmal (ein `stop` waehrend der Rueckfrage gewinnt), loescht ihn, startet den Tunnel
und prueft, dass er aktiv ist. Kann eine Sperre nicht bewiesen werden, bleibt der Tunnel zu; schlaegt
die Berechtigung oder der Start fehl, steht der Schalter wieder und der Exit ist 1. Nur der globale
Schalter faellt: Einheiten-Schalter und Sperren bleiben. Die Pruefung des Hosts davor ist noch nicht
gebaut ("host check not built yet (#50 SW3)"). edge liest nie `/etc/cloudflared`, aendert den Tunnel
nicht und ruft nie `sudo` auf. Fuer den Wegwerf-Beweis nehmen `EDGE_TUNNEL_UNIT` und
`EDGE_TUNNEL_MANAGER=user` eine Wegwerf-Einheit des Benutzer-Managers statt `cloudflared.service`.

Was `tunnel/` ist: der Inhalt, den overnightworks/home-tools#9 mit einem `sudo` nach `/etc`
installiert; edge selbst installiert nichts. `cloudflared.service` ist die Einheit mit
`/usr/bin/cloudflared`, `20-nonroot.conf` laesst sie ohne root laufen, `30-edge-global-stop.conf`
startet sie nicht, solange `~/.local/state/edge/global-stop` existiert, und
`polkit/10-edge-cloudflared-stop.rules` erlaubt dem Betreiber nur das **Stoppen** von
`cloudflared.service` ohne Passwort, auch ueber SSH; `start` fragt weiter. Ein blosses
`systemctl stop cloudflared` haelt daher nicht: Nach einem Neustart laeuft der Tunnel wieder, denn
nur der Schalter ueberlebt ihn. Der Notschalter ist `edge stop --all`.

Regel R4: Agenten rufen `edge apply` und `edge start` nie auf; `edge stop`
duerfen sie. Die Terminalpruefung und das `yes` halten
versehentliche und nicht-interaktive Laeufe auf; sie erkennen den Betreiber nicht (D14).

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

A weekly encrypted backup of the declared apps (Songmaker and Keycloak) into a
local restic repository, and a monthly test that restores the last successful backup's snapshot
and proves it is good.

**What it protects against:** accidental deletion and corruption. The
repository `~/backups/restic` sits on the same disk as the data, so it does
**not** protect against losing the disk. A second disk or an off-site copy is
a later slice.

`backup/apps.toml` declares each app as a validated record (compose project,
database service, dump kind, database user and name, the Postgres major, the
audio volume with the table that names its files, and what the restore test
asks the restored database). There is no hook language. Audio is optional and
all or nothing: Keycloak is a DB-only app and mounts no volume. The database
container and the volume are found by their `com.docker.compose.*` labels;
exactly one of each must match.

restic runs from the official image, pinned by version and digest
(`restic/restic:0.19.1@sha256:…`, see `src/edge_backup/settings.py`), with no
network, no capabilities and a read-only root, as your own user. Backup and
restore test check first that the pinned restic image and the restore image
of each app's Postgres major are present. `docker image prune -a` removes them; the next run pulls them again
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
| `edge-backup.timer` | Sunday 03:00, `Persistent=true` | dump, count, back up; then `forget --keep-weekly 4 --keep-monthly 3 --prune` and `check`, only when every app backed up |
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
audio files counted just before the scan) and, for an app with audio, `audio/`
(the volume, read live). No service is stopped or started, so the backup is
crash-consistent: a file written during the scan may be missing or partial. The
restore test catches exactly that, so such a backup never counts as good.

Keycloak's dump holds its realm signing keys, the credential hashes and the
client secrets. In the repository it exists only encrypted (restic). In plain
text it exists in exactly three places, each mode 0700: the staging directory
during a backup and the restore test's directory during a restore test (both
removed when the run ends), and the target of `edge-backup restore keycloak
--target`, which you delete yourself. If Keycloak is off or its database is down, the weekly run
waits 10 minutes for it, reports keycloak as failed and still backs up
Songmaker. A failed run also skips retention and the repository check for every app.

### The restore test

It restores the last successful backup's snapshot into a private directory under
`~/.cache/edge-backup` and checks:

- the restored audio file count equals the manifest's;
- `pg_restore --list` reads the dump;
- the dump loads with `pg_restore --exit-on-error --single-transaction` into a
  throwaway PostgreSQL container without network (removed afterwards) of the
  major the app's database runs: 16 for Songmaker, 17 for Keycloak, each image
  pinned by version and digest in `src/edge_backup/settings.py` (so the test
  proves the version the app really runs; an older server cannot read a newer
  archive; the pin of major 17 equals `keycloak-db` in `compose.yaml`, a test
  fails when they differ). The server's diagnostics are discarded because
  PostgreSQL quotes row values in them: a failure shows the step and the exit
  code only;
- the row count of the app's table (`songs`, Keycloak's `realm`) answers; for
  Keycloak (`require_rows = true` in `backup/apps.toml`) it is above zero;
- for an app with audio: the restored audio file count equals the manifest's,
  and every audio file the restored database references
  (`generations.mp3_path`) exists in the copy with a size above zero.

It never touches the live database or volume.

Backup and restore test stage their plaintext (the dump, the restored copy) in
`~/.cache/edge-backup` and remove it when they end, on success or failure. A
run killed outright (power cut, out of memory, systemd's stop timeout) cannot,
so its plaintext stays there until the next backup or restore test, which
removes it before anything else. That sweep removes only directories a run
marked as its own; anything else in that directory stays.

### Status

`~/.local/state/edge/backup-status.json`, written atomically at every phase.
The run's verdict stands at the top; each app (its name, which is also its
restic tag) has its own last good snapshot and times:

```json
{"schema": 2, "state": "failed", "phase": "readiness", "failing_phase": "readiness",
 "failing_app": "radio", "exit_category": "configuration",
 "last_attempt": "2026-10-18T01:00:00+00:00",
 "apps": {
   "radio": {"last_snapshot_id": "9a0e…",
             "last_successful_backup": "2026-10-11T01:12:40+00:00",
             "last_successful_restore_test": "2026-10-04T03:09:02+00:00"},
   "songmaker": {"last_snapshot_id": "4f1c…",
                 "last_successful_backup": "2026-10-18T01:12:40+00:00",
                 "last_successful_restore_test": "2026-10-04T03:09:02+00:00"}}}
```

`state` is `running`, `ok` or `failed`. A `running` that outlasts the timer's
6-hour limit means the run died: treat it as failed. `exit_category` is one of
`configuration`, `not-ready`, `command-failed`, `verification-failed`,
`interrupted`, `internal`. `failing_app` names the app whose step failed and is
`null` for a failure that belongs to none (the repository, a status failure).
The file holds no output, command line, path or file name.

An app that fails does not stop the others: every other app is still backed
up, and each app's snapshot is recorded right after its own `restic backup`
exits 0, so earlier successes stay. A run with any failure prunes nothing.
When the backup succeeded but prune or check failed, the apps' backup times
have moved on and `state` is `failed`. An app removed from `apps.toml` keeps
its entry; nothing deletes one automatically, and renaming an app makes it a
new app.

A status file that cannot be read (mode 000, corrupt JSON, an unknown
`schema`) is never overwritten: backup, restore test and restore fail with one
German line and exit 1, and the file stays byte-identical. Past successes are
never replaced by nulls; repair or remove the file by hand to go on.

A schema-1 file (flat `last_snapshot_id` and times) is read into this shape.
Its id and times go to the declared app only when exactly one app is declared
and restic confirms the id in that app's tag scope. Otherwise they stay in an
`unattributed` entry, and `restore` asks for `--snapshot`. `restore` never
writes the file; the next backup or restore test writes it as schema 2.

The way back to an older edge-backup: it refuses a schema-2 file like any
unknown one. Move `backup-status.json` aside, then either
`edge-backup restore <app> --snapshot <ID> --target <Ordner>` or run the next
backup, which writes a fresh status.

`state` describes the latest run of either command, backup or restore test: a
failed Sunday backup followed two hours later by a passing restore test reads
`ok`, and a timer that never fires leaves the last `ok` standing. A reader
therefore also judges freshness per app: `last_successful_backup` no older than
about 8 days, `last_successful_restore_test` no older than about 5 weeks.

| Exit | Meaning |
|---|---|
| 0 | every phase succeeded |
| 1 | a run failed; one German line names why. The status says `failed`, except for an unreadable status file, which stays as it is |

### Restore one app

```sh
uv run edge-backup restore songmaker --target ~/restore-songmaker   # a new directory, not under ~/.cache/edge-backup
```

It restores the snapshot of that app's **last successful backup**
(`apps.<app>.last_snapshot_id` in the status) and prints its short ID, never a
newer snapshot that a failed run left behind (restic exit 3 leaves a partial
one). `restore` refuses with one German line and exit 1, names the newest
snapshot and says how to choose it, in two cases: no successful backup of that
app is recorded yet, or the recorded snapshot is missing in the repository.
The restore test verifies each app against its own last good snapshot and
refuses the same way; a failed `check --read-data` gives no app a new time.

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

### Restore Keycloak

Only you restore Keycloak, and the block below overwrites only Keycloak's
database. It stops `keycloak` only (Traefik and every other service keep
running), loads the dump into `keycloak-db`, and starts `keycloak` again. First
restore the snapshot as above, `uv run edge-backup restore keycloak --target
~/restore-keycloak`; the dump holds keys, so the block deletes that directory
after a successful `docker start`. Paste the block as a whole: it stops at the
first failing command and never starts Keycloak after a failed load. If the
load fails, `pg_restore`'s messages can quote rows of the dump, so they are
discarded and the session runs with server error logging off (`keycloak` is the
image's superuser, which may set that); only the exit code is printed. Delete
`~/restore-keycloak` once you are done. The block acts on the compose project `edge`;
`EDGE_COMPOSE_PROJECT` names another, which is how it was driven against a throwaway stack.

```sh
(
set -euo pipefail
umask 077
restored=~/restore-keycloak
container() {
  docker ps --quiet --filter label=com.docker.compose.project="${EDGE_COMPOSE_PROJECT:-edge}" \
    --filter label=com.docker.compose.service="$1" \
    --filter label=com.docker.compose.oneoff=False
}
db=$(container keycloak-db)
kc=$(container keycloak)
[ "$(wc -w <<<"$db")" -eq 1 ] \
  || { echo "need exactly one running keycloak-db, found: ${db:-none}" >&2; exit 1; }
[ "$(wc -w <<<"$kc")" -eq 1 ] \
  || { echo "need exactly one running keycloak, found: ${kc:-none}" >&2; exit 1; }
docker stop "$kc"
docker exec -i -e PGOPTIONS='-c log_min_messages=panic -c log_min_error_statement=panic' \
  "$db" pg_restore --username keycloak --dbname keycloak \
  --clean --if-exists --single-transaction --exit-on-error \
  < "$restored/db/db.dump" 2>/dev/null \
  || { code=$?; echo "pg_restore failed with exit $code; keycloak stays stopped, start it with: docker start $kc" >&2; exit 1; }
docker start "$kc"
rm -rf "$restored"
)
```

A failed load changed nothing (`--single-transaction`); `docker start` with the
printed ID brings the old Keycloak back.

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

Start the stack as in [Start, readiness, stop](#start-readiness-stop) (Keycloak with
`edge apply`), Traefik included, and wait until the readiness check prints `ready`. A realm change
is then three steps, in this order:

1. **Back up, and test the restore.** The apply deletes no person, but it overwrites a console edit
   to a field the YAML lists and resets the console changes to declared objects named above, so there is a snapshot to go
   back to:

   ```sh
   uv run edge-backup backup
   uv run edge-backup restore-test
   ```

   Both must end with exit 0. The way back is [Restore Keycloak](#restore-keycloak).
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
   [edge-backup](#edge-backup) is not set up on this PC (no password file with mode 600, or no
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

- **1. Backup.** Unless [edge-backup](#edge-backup) is set up on this PC, decide once whether to
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
  [Start, readiness, stop](#start-readiness-stop): `docker compose --profile keycloak up -d
  keycloak` recreates Keycloak without the overlay, and `edge apply --closed` recreates a running Traefik whose
  `traefik.yml` or compose definition changed; a Traefik that is not running it leaves stopped,
  so start it first with `docker compose up -d --no-recreate socket-proxy traefik`. Wait until
  the readiness check prints `ready`.
- **6. Create your admin by hand.** Sign in at `http://localhost:8090/admin/` as `temp-admin`
  (password in the bootstrap file; enrol TOTP when asked), add a user in the master realm, give
  it the realm role `admin` and a password that is not temporary.
- **7. Retire the bootstrap.** Sign in as that user, enrol TOTP and add a passkey, then delete
  `temp-admin` in the admin console and run `rm ~/.secrets/keycloak/bootstrap-admin-password`.

Last, **`bin/edge apply`** (unnumbered), as above.

### Patch upgrade

1. **Back up the database.** The dump holds signing keys and credential
   hashes; edge-backup keeps it encrypted (see [edge-backup](#edge-backup)):

   ```sh
   uv run edge-backup backup
   ```

   It must end with exit 0 before you go on.
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version
   and digest), and of keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe** (below). It runs beside the live edge, which stays up.
4. **Start** as in [Start, readiness, stop](#start-readiness-stop) and wait
   until the readiness check prints `ready`; Keycloak migrates its database on
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

## Interne CA

`step-ca` is the certificate authority for the MCP origins: the servers behind
`https://<app>.mcp.internal` that the agent service calls over https only. It listens on
`step-ca:9000` on the internal network `mcp-ca` (no route out, no published port), has one ACME provisioner that accepts only
`*.mcp.internal` through TLS-ALPN-01, and issues 24-hour certificates. `step-ca/ca.json` is its one
configuration. `traefik-mcp` fetches its certificates from it (below).

The catalog line is set (`services.step-ca.run.enabled: true` in `fleet.yaml`). Go live, once, as
the operator (uid 1000): pull `~/git/edge` and run `edge apply`
([Dienste an und aus](#dienste-an-und-aus)). `edge apply` makes no key material itself: while
`step-ca/init --check` fails it asks separately and, on `yes`, runs `step-ca/init` in your terminal,
re-checks, and then shows a fresh plan.

`step-ca/init` makes the key material in one-off containers without a network and prints no
secret. It writes `~/.secrets/step-ca/` (directory 0700): `root_ca_key` (0600, never mounted into a
running container), `root_ca.crt` (0644) and `password` (0644, protects the root and the
intermediate key), and puts the intermediate and a copy of the root certificate into the
`step-ca` volume, and creates the empty `traefik-mcp-acme` volume, so `edge apply` never creates one.
A second run changes nothing; after an interruption it completes what is
missing and never overwrites a key. Back up `~/.secrets/step-ca/` and the `step-ca` volume. The
root certificate is what other services trust.

### traefik-mcp

`traefik-mcp` is a second Traefik, the gateway to the MCP origins. It has no host networking and
no Docker API, and listens on 443 inside three internal networks, none of which has a route out:
`mcp-ca` (step-ca and `traefik-mcp`), `mcp` (`traefik-mcp`, later the agent service) and
`mcp-backends` (`traefik-mcp` and the apps' MCP servers). It is the only container on all three.
It answers to `<app>.mcp.internal` on `mcp-ca` and `mcp`, gets a 24-hour certificate per name from
step-ca over TLS-ALPN-01 (trusting only the CA's root, `~/.secrets/step-ca/root_ca.crt`) and renews
it by itself. `traefik-mcp.yml` is its static configuration; `mcp/dynamic/` holds one router file
per app (`hello.yml` is the probe's). The certificates live in the volume `traefik-mcp-acme`,
which is not backed up because they are issued again. Traefik reads `traefik-mcp.yml` only when it
starts: while step-ca is on, `edge apply --closed` recreates `traefik-mcp` alone when that file or
its compose definition changed, and step-ca keeps its container and certificate; while step-ca is
off or switched off it names the change and starts nothing. `mcp/dynamic/` is watched and needs no
recreate.

A new app adds its name to the `x-mcp-origins` list in `compose.yaml`, a router file under
`mcp/dynamic/`, and puts its MCP server on `mcp-backends`. The name is `<stem>.mcp.internal`
(`docs/naming.md`).

The first go-live is described under [Interne CA](#interne-ca). `step-ca`, `traefik-mcp` and its helper `traefik-mcp-acme-owner` sit behind
the one switch, the compose profile `step-ca`, so a plain `docker compose up -d` starts none of
them. `traefik-mcp` waits for the CA to listen before it starts Traefik, so the order of the two
does not matter, also after a restart of the Docker daemon. Switch it off: `enabled: false`
through a pull request, the same pull, and `edge apply --closed`; the containers go, the volumes `step-ca` and `traefik-mcp-acme` stay, and
switching on again serves the same intermediate. Removing the volumes (`docker volume rm
<project>_traefik-mcp-acme`, `<project>_step-ca`) stays a manual command.

Proof: `flock /tmp/probe-stack.lock timeout --kill-after=30 2400 python3 probe/step-ca/probe.py
--scratch DIR` starts a throwaway project under a throwaway `HOME` and holds thirteen proofs. Proofs 1
to 5 are the CA's (init modes and silence, the ACME directory verifies only with the probe root, the
volume holds no root key, a restart keeps the intermediate, a partial state is completed, also with
a root key owned by another user). Proofs 6 to 9 are the gateway's: a client on `mcp` reaches
`https://hello.mcp.internal` only with the root and is served a 24-hour certificate from the
intermediate; plain http gets no answer from the backend; the three networks split as ruled; the
host gains no listener (`ss -ltn` and `edge-check` unchanged); and a certificate from a 20-minute
CA copy is renewed within 12 minutes without a restart. Proof 10 runs `edge apply --closed` on,
off and on in a copy of the repository: both volumes and the same intermediate survive, and a
repeated apply changes nothing. Proof 11 drives the emergency switch through the cells A, B and E
crossed with `edge stop`, `edge start` and `edge apply --closed`: the marker survives apply and
the off, nothing is started past it, and `edge start` at a terminal ends it. Proof 12 lets
`edge apply --closed` ask for `step-ca/init`, run it and switch the unit on. Proof 13 lets
`traefik-mcp` follow `traefik-mcp.yml` and its definition: recreated alone while step-ca is on,
with step-ca's container, intermediate and served certificate kept; named only while step-ca is
switched off or disabled; recorded at a switch-on, so the next apply changes nothing. The run
takes about 12 minutes, 10 of them proof 9's renewal wait, within the 2400 s timeout.
Every call runs from a throwaway container on the network a client would sit on.

The switch is one for the CA and the gateway together (`enabled: false` through a pull request,
`git -C ~/git/edge pull --ff-only`, then `edge apply --closed`). `docker volume rm <project>_step-ca` and deleting `~/.secrets/step-ca/`
throw the CA away; they stay manual commands, and the gateway must be off first: it holds
certificates from this CA.

### agent-egress

`agent-egress` is a third Traefik, the one way out of the agent service: a TLS passthrough that
reads the SNI of the ClientHello and dials only `api.anthropic.com:443` (the provider CLI) and
`auth.hallucinai.de:443` (the JWKS fetch). It holds no key and has no API, ping or metrics. Any
other name, or none, matches no router and `sniStrict` ends the handshake in the gateway;
`agent-service/egress/dynamic/allowed.yml` is the one owner of the two names. The service finds the
gateway through `extra_hosts` (compose.yaml's `x-agent-attachments`), not through DNS: the networks
`agent`, `egress` and `mcp` are internal and isolated (`gateway_mode_ipv4: isolated`), so a member
has no route out, no host address and no DNS forwarding. Only `agent-egress` sits on `internet`, a
plain NAT bridge, and listens on `172.31.252.10:443` of `egress` alone. Like `traefik-mcp` it is
a follower: its static file is bound at Traefik's static-config path, so `edge apply --closed`
recreates it alone while the unit `agent-service` of `fleet.yaml` is on; no plain
`docker compose up -d` starts it.

Proof: `flock -o /tmp/probe-stack.lock timeout --kill-after=50 1800 python3
probe/agent-service/egress.py --scratch DIR [--target service]` (DIR must already exist, empty and
writable, for example from `mktemp -d`) starts a throwaway project on a
free /24 (never the live `edge_egress` subnet) with a hardened stand-in for the service, or with
the real `agent-service` and a dummy login in the scratch HOME, drives the cells E1 to E10, E12 and
E13 from it (E11 and E14 with the service), and removes everything. E3 to E5 (a refused name dials
nothing) and E10 (a query from the member leaves no trace on the host) are observed through the
packet windows of `probe/agent-service/capture.py`, each with a positive control; a window that
dropped a frame, held a frame it could not decode, or ended before its control, is not driven and
fails the run. E7 also tries an address in each network's subnet that no member owns
(`EHOSTUNREACH`). E11 starts the Claude CLI inside the service with a dummy token: it reaches the
real API through the gateway and ends with an authentication failure. CI holds the static facts
(E16) in `tests/test_agent_egress.py`.

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

## Rollback

```sh
docker compose --profile '*' down
```

This removes the containers of every profile (the core, Keycloak, step-ca, the agent service
and its gateway) and the project's networks;
the listeners on `127.0.0.1:8081` and `127.0.0.1:8090` disappear. It keeps the
volumes: `keycloak-db` holds every Keycloak user, credential and session and
survives the rollback, so a later start finds them again. Never run
`down --volumes`. The socket directory volume is tmpfs and holds nothing; remove
its definition alone with `docker volume rm edge_socket-proxy-run`. Then revert
the commit.

## Operator steps A8 (going live)

The operator does these himself, in this order; an agent never applies them.
Each step must hold before the next one starts.

1. **Presentator's Access app.** Check that `presentator.hallucinai.de` keeps
   its own Access app (its people, plus the `/sources/*` bypass) and still
   answers 401 to a request without a session.
2. **DNS and tunnel lines.** They come from the catalog through the publish
   of `bin/edge apply` (`edge external apply`, see
   [Externe Dienste als Code](#externe-dienste-als-code)); `auth` goes live as
   `public`. There is no Zero Trust for now (operator, 20.10.2026), so no
   wildcard Access app and no bypass app exist; the wildcard `*` protection is
   X8b on #30, which turns `auth` into a bypass then.
