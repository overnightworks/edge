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
| `cloudflared-config.example.yml` | the tunnel ingress template the operator applies in A8 |
| `tunnel/` | the cloudflared unit, its drop-ins (`20-nonroot.conf`, the boot skip `30-edge-global-stop.conf`) and the stop-only polkit rule, with `@HOME@`, `@OPERATOR_USER@` and `@TUNNEL_ID@`; edge ships them, overnightworks/home-tools#9 installs them (see [Notschalter](#notschalter-edge-stop-und-edge-start)) |
| `fleet.yaml` | the service catalog: which units of this repository run (`services.<unit>.run.enabled`), and the OpenTofu inputs (see [Dienste an und aus](#dienste-an-und-aus)) |
| `src/edge/`, `external/` | the `edge` command (`edge apply --closed`, `edge stop` and `edge start`, `edge external plan` and `apply`) and the pinned OpenTofu root it plans (see [Dienste an und aus](#dienste-an-und-aus), [Externe Dienste als Code](#externe-dienste-als-code)) |
| `src/home_tools/`, `docs/home.md`, `bin/home` | the `home` workstation tool (German, see [docs/home.md](docs/home.md)); `bin/home` is a symlink to the one launcher `bin/edge` |
| `src/edge_check/`, `edge-check.toml` | the `edge-check` alarm and its allow-list (see below) |
| `src/edge_backup/`, `backup/apps.toml`, `systemd/` | the `edge-backup` runner, the declared apps, its user timers (see below) |
| `tests/`, `pyproject.toml`, `uv.lock` | the tests of `edge-check`, `edge-backup`, the probe, the secret check, `keycloak/go-live`, `step-ca/init`, `edge apply` (`tests/test_edge_apply.py`), the external edge tests (`tests/test_edge_external.py`) and the OpenTofu tests under `external/tests/`, run only through `python -m edge.external.tofu_test`, with their locked dependencies |
| `.github/workflows/ci.yml` | the static checks (lint, tests, compose renders, no published ports, tunnel template, no secret in any file); CI never starts containers |
| [`docs/naming.md`](docs/naming.md) | the naming rule: how every overnightworks repo, tool, package and unit is named |

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

## Running edge

`~/git/edge/bin/edge` runs the `edge` command of the checkout it lives in, from
any directory: it syncs that checkout's locked dependencies (`uv sync --locked`,
never rewriting `uv.lock`) and then becomes `.venv/bin/edge`, from the same
environment the systemd units call. A failed sync ends with exit 2 and names the
checkout.
Slice S2b of overnightworks/edge#100 puts it on PATH as `edge`.

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
# `edge apply --closed` switches them on or off by `services.keycloak.run.enabled` in
# fleet.yaml; while `keycloak/go-live` is not done it asks and runs it in your terminal.
# fleet.yaml is edited through a pull request and reaches the live checkout by the pull below
# (until S2.4c); a local edit leaves the tree dirty, and the apply refuses it, naming the path.
git -C ~/git/edge pull --ff-only
edge apply --closed
# Traefik reads traefik.yml (its entrypoints) only when it starts. When traefik.yml changed
# since it was last applied, or the compose definition of socket-proxy or traefik changed
# (compose.yaml, an image pin, EDGE_DOMAIN in .env), the same `edge apply --closed` recreates
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
--ff-only`, then `edge apply --closed`) stops and removes
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

Cloudflare und GitHub werden mit OpenTofu als Code verwaltet (`external/`);
wo diese Schicht liegt, steht in [`docs/architecture.md`](docs/architecture.md).
Der State liegt nie im Checkout, sondern verschluesselt in
`~/.secrets/opentofu/fleet.tfstate`.

`edge external plan` zeigt, was von den Live-Diensten abweicht (Exit 0 keine
Aenderung, 1 Aenderungen offen, 2 nicht moeglich). Es liest drei Dateien (erwartet
wird je 0600, geprueft wird das hier nicht) und gibt sie nur an das Kind `tofu` weiter
(die eine Ausnahme steht bei `check --hosts` unten), dessen Umgebung eine
Allowlist ist (kein `TF_LOG*`, `TF_CLI_ARGS*`, `TF_ENCRYPTION`, `GITHUB_*`,
`GH_*`, Proxy aus der Shell):

| Datei | Variable im Kind |
|---|---|
| `~/.secrets/cloudflare/fleet-read` | `CLOUDFLARE_API_TOKEN` |
| `~/.secrets/github/fleet-read` | `GITHUB_TOKEN` |
| `~/.secrets/opentofu/state-passphrase` | `TF_VAR_state_passphrase` |

Die weiteren Token-Dateien der Schicht folgen mit X5b (home-tools#10).
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
Schreib-Tokens `~/.secrets/cloudflare/fleet-write` und
`~/.secrets/github/fleet-admin` (dieselbe Allowlist-Umgebung, dieselbe
State-Passphrase). Es laeuft nur aus `~/git/edge` als Haupt-Worktree auf
einem sauberen `main`, sonst Exit 2 mit dem Grund (`plan` laeuft aus jedem
Checkout), und nur an einem Terminal (stdin und stdout; sonst
Exit 1, bevor `tofu` aufgerufen wird), plant in eine Datei, zeigt den Plan
und wendet genau diese Datei erst nach der Eingabe `yes` an (`y`, `Yes`,
leer: Abbruch, Exit 1). Ein Plan ohne Aenderungen endet mit Exit 0 ohne
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

## Dienste an und aus

`fleet.yaml` ist der Katalog. Ein Eintrag mit `run` und ohne `repo` ist eine Einheit
dieses Repositorys; sein Schluessel ist der Name des Compose-Profils (`docs/naming.md`):

```yaml
services:
  step-ca:
    run:
      enabled: false        # true schaltet das Profil `step-ca` an
      setup: step-ca/init   # optional: der Befehl, der den Schluessel macht
```

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

Eine Einheit mit `setup` wird erst angeschaltet, wenn `<setup> --check` Exit 0 gibt. Fehlt das
Setup, fragt `edge apply --closed` vor dem Plan eigens ("`<Einheit>` is not set up: ... Type 'yes'
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
kann, ein Setup, das nicht starten kann). Ein blosses `edge apply` endet mit Exit 2: die
Publish-Phase gibt es noch nicht.

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

A weekly encrypted backup of the declared apps (Songmaker; Keycloak joins with its go-live) into a
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
--target`, which you delete yourself. While Keycloak is off, `backup/apps.toml` does not declare
it, so the weekly run and the restore test cover Songmaker only. Once Keycloak is declared and its
database is down, the weekly run waits 10 minutes for it, reports keycloak as failed and still
backs up Songmaker.

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
  Keycloak (its record, which the go-live change (#118) adds back to `backup/apps.toml`, sets `require_rows = true`) it is above zero;
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
`~/restore-keycloak` once you are done.

```sh
(
set -euo pipefail
umask 077
restored=~/restore-keycloak
container() {
  docker ps --quiet --filter label=com.docker.compose.project=edge \
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

Going live needs you in two places only: in the browser, to create your own
admin with a second factor (a machine must not hold that), and in Cloudflare, to
open the public door. Everything else is `keycloak/go-live` (see
[Keycloak go-live](#keycloak-go-live)). The sections below are the reference.

### Doors, issuers and configuration

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
only in the admin console; no file lists a person. The songmaker, hopin and presentator
clients are in `realm.yaml` before the go-live, each secret the file
`~/.secrets/keycloak/clients/<clientId>` (the go-live makes it; presentator's one client serves
`presentator` and `presentator-talk`); the other app clients
arrive at K13/K14, after K3c.

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

> **Managed in full.** Groups are created and renamed only in `realm.yaml`.
> The apply deletes every top-level group, client, client scope and flow that
> an earlier apply created and the YAML no longer lists, and below a listed
> group it deletes **every** subgroup the YAML lacks, whoever created it: a
> group made in the admin console under `/apps` or `/ki-abo` (say
> `/apps/newtool`) disappears on the next apply, **with every membership in
> it** (the probe's proof 3c removes `/apps/probe-console` with its one member).
> Removing or renaming a group in `realm.yaml` deletes it the same way.
> Renaming a group in the admin console first is safe only if no apply runs
> before the matching YAML change lands. A console change inside a declared
> object goes the same way: a protocol mapper added to the scope `groups`, a
> realm role added to `service-account-config-cli`, a step added to
> `browser-mfa-forms`, and a top-level flow made in the console in master. A
> console-made client, identity provider, component, realm role, and a mapper
> on `agent-service` survive. Each such removal shows around an apply: the
> realm observer (`realm-observer`, `src/edge/realm/observe.py`) reads the
> realms before and after, and `edge.realm` compares the two reads and prints
> one line per change with its realm.

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
`edge apply --closed`), Traefik included, and wait until the readiness check prints `ready`. Then apply
`keycloak/*.yaml`:

```sh
docker compose --profile keycloak run --rm keycloak-config-cli
```

The apply signs in as the `config-cli` service client and fails loudly on a
`$(env:…)` variable that is not set (`IMPORT_VARSUBSTITUTION_UNDEFINEDISERROR`).
It skips a realm whose file has not changed since the last apply; add
`--env IMPORT_CACHE_ENABLED=false` to force it.

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
marker. Once the catalog switches Keycloak on and while go-live is not done, `edge apply --closed` asks and runs it in your terminal on yes (`--check` first).

- **1. Backup.** It asks whether to continue without the Keycloak backup only while
   [edge-backup](#edge-backup) is not set up on this PC (no password file with mode 600, or no
   initialised repository). There is no default; the answer is yours. Nothing is asked once
   edge-backup is set up (the change that switches Keycloak on (#118) adds its record to `backup/apps.toml`, so its next
   weekly run covers Keycloak; the check does not prove a
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

Last, **your part, in Cloudflare** (unnumbered). The script ends by printing the steps (A8, steps
3 to 5, for `auth.hallucinai.de`) and the check:

```sh
curl -s https://auth.hallucinai.de/realms/hallucinai/.well-known/openid-configuration \
  | grep -o '"issuer":"[^"]*"'
# "issuer":"https://auth.hallucinai.de/realms/hallucinai"
```

The operator's own browser is no proof: it holds an Access session and
passes the wildcard app either way.

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

Last, **public login in Cloudflare** (unnumbered), as in the Cloudflare part above.

### Patch upgrade

1. **Back up the database.** The dump holds signing keys and credential
   hashes; edge-backup keeps it encrypted (see [edge-backup](#edge-backup)):

   ```sh
   uv run edge-backup backup
   ```

   It must end with exit 0 before you go on.
2. **Bump the pin** of `quay.io/keycloak/keycloak` in `compose.yaml` (version
   and digest), and of keycloak-config-cli if a build for the new version exists.
3. **Re-run the probe** (below). It needs 8081 and 8090, so stop the live edge
   first with `docker compose --profile keycloak --profile probe down` (volumes stay).
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

The proofs: the public issuer, the themed login and account pages in German
and English and the path allow-list (1, 2a), the master issuer and the
forced second factor on 8090, for the console and for a password grant (2b), a failing undefined variable, a fresh apply, an unchanged export after a
cache-off second apply and a changed YAML that applies and deletes a
console-made subgroup it does not list, with a seeded member untouched (3a–3d),
the `groups` claim and the `agent-service` audience (4), a disabled user refused
on refresh (5), a back-channel logout reaching the test RP (6), none of the
credentials it made or received (secret files, passwords, the TOTP seed, the RP
secret, every token of every token response, and the admin console's PKCE
verifiers, authorization codes and session cookies) in the `Config.Env` of any
container it ran (the bootstrap Keycloak and every keycloak-config-cli run
included), in any compose, apply or service log output, or in its own output
(7), the realm settings as decided, with admin events that carry no client
representation (8), and the songmaker, hopin and presentator clients: a PKCE sign-in with the
scratch secret that ends in an ID token with the member's group (`/apps/songmaker/admin`,
`/apps/hopin`, `/apps/presentator/admin`), an exact callback, the password grant,
`client_credentials` and the `offline_access` scope refused (a client has no optional scope), and
an end-session page for its registered logout address only (10). Proof 7 also looks for each client
secret and its tokens.

## Interne CA

`step-ca` is the certificate authority for the MCP origins: the servers behind
`https://<app>.mcp.internal` that the agent service calls over https only. It listens on
`step-ca:9000` on the internal network `mcp-ca` (no route out, no published port), has one ACME provisioner that accepts only
`*.mcp.internal` through TLS-ALPN-01, and issues 24-hour certificates. `step-ca/ca.json` is its one
configuration. `traefik-mcp` fetches its certificates from it (below).

Go live, once, as the operator (uid 1000): run `step-ca/init`, set `services.step-ca.run.enabled:
true` in `fleet.yaml` (a pull request), then run `edge apply --closed`
([Dienste an und aus](#dienste-an-und-aus)). `edge apply` makes no key material itself: while
`step-ca/init --check` fails it asks separately and, on `yes`, runs `step-ca/init` in your terminal,
re-checks, and then shows the plan.

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

Switch it on: set `enabled: true` in `fleet.yaml` through a pull request, pull it with
`git -C ~/git/edge pull --ff-only`, then run `edge apply --closed`, which asks
for `step-ca/init` first when it is missing. `step-ca`, `traefik-mcp` and its helper `traefik-mcp-acme-owner` sit behind
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

## Rollback

```sh
docker compose --profile keycloak --profile probe down
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
   and the `auth` line in step 4; the go-live's Cloudflare step returns here): create a second
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
