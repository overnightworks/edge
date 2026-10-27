# The stack: run, apply, stop

Reference for the operator and for an engineer changing the stack commands. It names the commands and
the contracts they keep; the commands' own `--help` and `src/edge/`, `src/hal/` hold the rest. The
picture of the platform, and the state table that owns every guard of `stop`, `start` and `apply`, is
[architecture.md](architecture.md) (section 8). The sections "Dienste an und aus" and "Notschalter"
are German, the rest English.

## Running edge

`~/git/edge/bin/edge` runs the `edge` command of the checkout it lives in, from any directory: it syncs
that checkout's locked dependencies (`uv sync --locked`, never rewriting `uv.lock`) and then becomes
`.venv/bin/edge`, from the same environment the systemd units call. A failed sync ends with exit 2 and
names the checkout. It is not on PATH: the operator types `hal` (`hal --help` lists its commands).

## Configure

The first `edge apply --closed` writes `.env` itself: `EDGE_DOMAIN` is `fleet.yaml`'s `cloudflare.zone`
and `DOCKER_GID` the id of this PC's `docker` group (Keycloak's secrets are files). `.env` is local and
never committed. A `.env` that differs, or lacks one of the two keys, is named and left alone: delete it
and `edge apply --closed` writes it again.

## Start, readiness, stop

- **Start.** `docker compose up -d --no-recreate socket-proxy traefik` starts a core service that is not
  running. `edge apply` never starts one; it only names a stopped one. Keycloak and its database sit
  behind the compose profile `keycloak`, so a plain `up -d` starts neither; `edge apply` switches them
  by `services.keycloak.run.enabled` in `fleet.yaml`. `fleet.yaml` changes by pull request and reaches
  the live checkout by `git -C ~/git/edge pull --ff-only` (until S2.4c); a local edit leaves the tree
  dirty and the apply refuses it, naming the path.
- **The legal unit.** `legal` (nginx, profile `legal`, switched by `services.legal.run.enabled`) serves
  the imprint, the privacy notice and the terms from `legal/built/` for `Host: <zone>` on `127.0.0.1:8081`; the stylesheet and appkit's
  tokens and fonts (`legal/assets/appkit-<version>/`, which `hal apply` writes from the installed appkit) answer under `/_rechtliches/`, every other path with 404. The catalog gives
  it the apex as its host, which the Cloudflare redirect leaves to it for the three pages, `/favicon.ico` (answered 404) and
  `/_rechtliches/` (`cloudflare.redirect.serve`). The pages are built by `hal apply` from `legal/frame` and `legal/sections` (the plan names each page that is
  new or changes) and go live when it has written them; before the first build they answer 404. The stylesheet goes live once the live checkout is
  pulled; a changed `legal/nginx/default.conf` needs the container restarted with `hal stop legal` and
  `hal start legal`.
- **Readiness.** The stack is ready when `127.0.0.1:8081` and `127.0.0.1:8090` listen, an unknown host
  gets 404 on 8081, and Keycloak answers the master realm on 8090; Keycloak's start takes
  longest. No command checks these four facts together (a named gap on overnightworks/edge#222).
  What proves what today: `hal apply` observes the containers running; `docker compose ps` shows
  their health; the publish phase asks each declared host's `ready` path, only for a plan with
  changes; `keycloak/go-live` probes 8081 and 8090.
- **Stop.** `docker compose --profile keycloak --profile legal --profile probe down`. Without `--profile keycloak`
  Keycloak and its database keep running, without `--profile probe` a running probe `hello` stays. It
  removes no volume; never add `--volumes`, because that deletes the Keycloak database.
- **After a reboot** socket-proxy, traefik, keycloak-db and keycloak come back on their own
  (`restart: unless-stopped`), also without the profile on the command line; `hello` never does.
- **Switching Keycloak off** (`enabled: false` through a pull request, the pull, `edge apply`) stops and
  removes `keycloak-db` and `keycloak` and keeps the volume `keycloak-db`, `~/.secrets/keycloak` and the
  `keycloak` network; switching it on again uses the same database. Removing the entry from `fleet.yaml`
  is not "off": apply then leaves Keycloak as it is. Whether `auth` and `127.0.0.1:8090` answer depends
  on Traefik, which `edge apply` recreates only for a changed `traefik.yml` or compose definition and
  never starts.
- **Switching an app off at once** (`hal stop <app>`, no terminal) records the marker, then writes and
  proves the barrier over the app's catalog hosts, then stops the containers of its Compose project
  (file-free; volumes, checkout and record stay). `hal apply` never starts a stopped app: it names it
  and fetches nothing, refuses the whole run while a container of it still runs, and a stop that
  arrives during the `yes`, the build, the start or the health wait wins: the app is not started, or
  what started is removed, and apply ends partial. `hal start <app>` (terminal, `yes`) starts the
  stopped containers, waits for health, removes the barrier and clears the marker; `hal online` proves
  the barriers of apps as of units.

What the entrypoint answers without a route: an unknown host or invalid name gets `404`; an HTTP/1.1
request without a `Host` header gets `400` (Go's HTTP server rejects it); an HTTP/1.0 one gets `404`.

## The probe profile

`hello` (traefik/whoami, labelled `edge.name=hello`) and `hello-mcp` (the same image on `mcp-backends`,
the MCP origin `hello.mcp.internal`) live under the compose profile `probe`, so a plain
`docker compose up -d` never starts them. Start one with `docker compose --profile probe up -d hello`;
retire it with `stop` and `rm -f` under the same profile. Afterwards `hello.<EDGE_DOMAIN>` answers 404.

## Rollback

`docker compose --profile '*' down` removes the containers of every profile (the core, Keycloak,
step-ca, the agent service and its gateway) and the project's networks; the listeners on
`127.0.0.1:8081` and `127.0.0.1:8090` disappear. It keeps the volumes: `keycloak-db` holds every
Keycloak user, credential and session and survives the rollback. Never run `down --volumes`. The
socket directory volume is tmpfs and holds nothing; remove its definition alone with
`docker volume rm edge_socket-proxy-run`. Then revert the commit.

## Exception E: where the five stack commands and `hal setup` deviate

`hal`'s exit codes are in `src/hal/grammar.py`, its error form in `src/hal_output/__init__.py`, and `hal --help` shows both. Until slice H5a2 of
overnightworks/edge#207 (after H5e) classifies the stack's outcomes in their owners, `hal apply`,
`stop`, `start`, `offline` and `online` end with 0, or with 3 for every outcome of the stack that is
not "applied or nothing to do". They deviate in exactly three ways:

- **E1:** a guard refusal decided by the stack ends with 3, not 4: no terminal, the apply lock is held,
  `yes` declined, the unit is disabled or not set up, a barrier is unproven, or the checkout is not the
  live one or not a clean `main`.
- **E2:** a Ctrl-C that the stack catches ends by what the stack then observes, not with 130. `apply`,
  `stop` and `start` print their `interrupted …` line and end with 3. `offline` drops the interrupt and
  reports the tunnel as observed: 3 with its `partial …` line while the tunnel still runs, 0 when it did
  stop, the marker is recorded and the boot drop-in is present. A Ctrl-C the stack does not catch ends
  with 130.
- **E3:** the stack's lines keep their form `edge <verb>: …`, not `error:`/`next:`, and name `edge`
  commands until H5e renames them; a number in such a line is edge's, not hal's process exit. Type
  `hal stop <unit or app>` / `hal start <unit or app>` for `edge stop|start <unit or app>`, `hal offline` for
  `edge stop --all`, `hal online` for `edge start --all`, `hal apply [--closed]` for
  `edge apply [--closed]`, and `hal check hosts` for the host check.

`hal setup` deviates in one way:

- **E4:** `hal setup` prints four rows and a verdict, and a stop as an `error:`/`next:` pair
  ([docs/home.md](home.md), `hal setup`); it ends with 0 when nothing failed and with 3 for a failed
  step or a refused system step (`home setup` keeps 1). Ansible's own stream and its BECOME prompt
  stay until the password step of #296.

## Dienste an und aus

`fleet.yaml` ist der Katalog. Ein Eintrag mit `run` und ohne `repo` ist eine Einheit dieses
Repositorys; sein Schluessel ist der Name des Compose-Profils (`docs/naming.md`). `run.enabled: false`
schaltet das Profil ab; `run.setup` nennt optional den Befehl, der den Schluessel macht (`step-ca/init`,
`keycloak/go-live`); `run.build` nennt das Rezeptverzeichnis des einen gebauten Images der Einheit
(`agent-service/image`), das `hal apply` in einem Recreate tauscht. Wer die Aenderung in welchem Zustand darf, steht in der Zustandstabelle von
[architecture.md](architecture.md) (Abschnitt 8) und wird hier nicht wiederholt.

**Zwei Teile.** Das blosse `edge apply` fuehrt den lokalen Teil (`edge apply --closed`) aus und, nur
wenn der mit Exit 0 endete, die Publish-Phase (`edge external apply`, [external.md](external.md)). Jeder
Teil fragt sein eigenes `yes` und nur, wenn er etwas zu aendern hat; gibt es nirgends etwas zu aendern,
fragt `edge apply` nichts. Endet der lokale Teil nicht mit 0, steht eine Zeile "nothing published", es
laeuft kein tofu, und der Exit ist der des lokalen Teils. Ein Ctrl-C (SIGINT) vor der Publish-Phase
endet mit Exit 1 und "interrupted, nothing published"; waehrend der Publish-Phase gelten deren
Unterbrechungsregeln, und ihr Exit bleibt, sobald sie ihr Urteil festgehalten hat. Kommt eine
Unterbrechung ohne festgehaltenen Exit aus ihr heraus, endet es mit Exit 1 und der Zeile
"run `hal check hosts`". Ein SIGTERM (`kill`) vor der Publish-Phase beendet den Prozess
wie bei `--closed`: nichts wird veroeffentlicht.

**Der lokale Teil.** Er bringt jede Einheit in den Zustand, den `enabled` sagt. An:
`docker compose --profile <Einheit> up -d` fuer genau die Dienste des Profils. Aus: `stop`, dann `rm -f`
derselben Dienste; nie `down`, nie `-v`, nie ein Volume (R1). Er zeigt den Plan, laeuft nur an einem
Terminal und wendet ihn erst nach der Eingabe `yes` an (alles andere: Abbruch, Exit 1); danach liest er
die Container noch einmal und meldet nur, was er sieht. Ein zweiter Lauf meldet `nothing to change`.
"An" heisst: jeder Dienst der Einheit hat seinen Container laufen (oder ein Einmal-Dienst ist fertig),
nicht, dass er gesund ist; die Gesundheit zeigt `docker compose ps`.

**Wo und woraus er laeuft.** Nur aus dem Live-Checkout `~/git/edge` als Haupt-Worktree (sonst Exit 2
mit dem Grund), sonst haengte er das Live-Projekt an einen fremden Baum. Dann, jeweils mit nur der
ersten verweigernden Zeile: kein anderes `edge apply --closed` laeuft (Exit 1; `stop` und `start`
warten nie darauf), HEAD steht auf `main` (Exit 2), der Baum ist sauber (Exit 2, jeder geaenderte oder
unverfolgte Pfad auf eigener Zeile; ignorierte wie `.env` zaehlen nicht, "ignoriert" ist nur, was die
`.gitignore` des Repositorys sagt) und `.env` stimmt (siehe Configure). Git laeuft dabei ohne Hooks,
ohne fsmonitor und ohne die globale Konfiguration.

**Der Abruf.** Danach holt er `main` von `origin` und haelt sich ab da an genau diesen Hash. Nur dieser
Abruf liest die globale Git-Konfiguration, damit er sich wie `git pull` ueber den `gh`-Credential-Helper
anmeldet; edge liest, uebergibt und druckt nie ein Credential und verweigert eine URL mit Passwort. Er
schreibt nur Objekte, `FETCH_HEAD` und `origin/main`; HEAD, Index und Baum bleiben. Scheitert er (kein
Netz, kein `origin`, Anmeldung, 120 s), oder hat der Checkout Commits, die `origin/main` fehlen, oder
ist er daneben gewachsen, endet es mit Exit 2 und aendert nichts. Wartende Commits zeigt er mit Hash
und Betreff und ihre Dateien nach Wirkung: live sobald aktualisiert (`dynamic/`, `mcp/dynamic/`),
von `edge apply` neu erstellt (`traefik.yml`, `traefik-mcp.yml`, `compose.yaml`), Halt fuer `uv sync`
(`pyproject.toml`, `uv.lock`), von `edge apply` nicht angewendet (Binds der Einheiten: `keycloak/`,
`legal/`, `step-ca/ca.json`), sonstige. Der Plan wendet HEAD an, nie die wartenden Commits; die bringt der
Betreiber bis S2.4c mit `git -C ~/git/edge merge --ff-only <Hash>` selbst herein und ruft den Apply
noch einmal auf.

**Der Kern und die Follower.** Welche Dienste `edge apply --closed` neu erstellt, in welcher
Reihenfolge, und wie der Eintrag in `applied.json` gefuehrt wird:
[architecture.md](architecture.md), Abschnitt 8, "The applied record".

**Images.** Laeuft ein Dienst einer Einheit, die nicht ausgeschaltet wird, auf einem anderen Image, als
`compose.yaml` jetzt nennt, nennt der Plan es und tauscht es nie; der Lauf bleibt `nothing to change`.
Das Upgrade ist Sache des Betreibers (Keycloak erst mit Dump). Ausnahme ist der gebaute Dienst einer
Einheit mit `run.build` (`agent-service`): `hal apply` erzeugt ihn nach dem Ja des Plans neu, wenn
sein Image, seine Definition oder sein Tag-Inhalt abweicht; laufende Anfragen enden dabei.

**Setup.** Eine Einheit mit `setup` wird erst angeschaltet, wenn `<setup> --check` Exit 0 gibt. Der
Vertrag von `<setup> --check`: fragt nichts, druckt kein Geheimnis, legt nichts an, braucht keinen
laufenden Dienst; Exit 0 eingerichtet, 1 nicht eingerichtet (eine Zeile sagt, was fehlt), 2 es kann es
nicht sagen. Eine laufende, im Katalog eingeschaltete Einheit mit `setup` wird bei jedem Apply geprueft:
ist sie nicht eingerichtet, bekommt sie die Setup-Frage und sperrt das Publizieren. Die Frage kommt vor
dem Plan ("Type 'yes' to run `<setup>` now"; nur das genaue `yes` zaehlt, `no` bricht den ganzen Lauf
ab, Exit 1, nichts geaendert). Nach `yes` laeuft das Setup in deinem Terminal, ohne Argumente, ohne
Zeitlimit, Ausgabe ungefiltert; die Schluessel macht allein dieser Befehl, `edge apply` sieht sie nie.
Dann prueft `--check` noch einmal und der Plan folgt mit seinem eigenen `yes`. Ein Setup, das scheitert
oder per Ctrl-C unterbrochen wird, schaltet nichts (Exit 1); was es gemacht hat, bleibt auch nach einem
`no` zum Plan (Loeschen bleibt Handarbeit). Ohne Terminal laeuft nie ein Setup. Eine per `edge stop`
ausgeschaltete, noch laufende Einheit, die nicht eingerichtet ist, wird abgewiesen (`edge stop` noch
einmal).

**Exit.** 0 angewendet und beobachtet oder nichts zu tun; 1 verweigert (auch: ein anderes Apply laeuft),
abgebrochen, fehlgeschlagen oder teilweise; 2 nicht planbar (falscher Checkout oder Zweig, Baum nicht
sauber, `.env` abweichend oder unlesbar, keine Gruppe `docker`, `cloudflare.zone` ungueltig, git nicht
befragbar, Abruf gescheitert, Checkout vor oder neben `origin/main`, Katalog oder Compose ungueltig,
`traefik.yml` oder `traefik-mcp.yml` fehlt, ein Setup-Check, der es nicht sagen kann, u. a.). Das
blosse `edge apply` gibt den Exit des lokalen Teils unveraendert zurueck, solange der nicht 0 ist,
danach den der Publish-Phase, deren Exit 2 zu 1 wird. Ein Exit 2 von `edge apply` heisst also:
nichts wurde veroeffentlicht, und ein Setup, das schon lief, kann Aenderungen hinterlassen haben. Ueber `bin/edge`
kann Exit 2 auch vom `uv sync` des Starters kommen; seine Zeile nennt das Sync.

## Notschalter: `edge stop` und `edge start`

Die Befehle sind `hal stop|start <Einheit>` und `hal offline|online`; hinter ihnen laufen
`edge stop|start [--all]`. Welche Zustaende es gibt und wer wann darf: Zustandstabelle in
[architecture.md](architecture.md), Abschnitt 8. Hier steht, was die Befehle halten.

**Pro Einheit.** `edge stop <Einheit>` schreibt zuerst den Schalter, dann
`docker compose --profile <Einheit> stop` fuer genau die Dienste der Einheit, liest die Container noch
einmal und meldet "switched off" nur, wenn nichts mehr laeuft. Container und Volumes bleiben (kein `rm`,
kein `down`, nie `socket-proxy` oder `traefik`). Es braucht kein Terminal und kein `yes`; Agenten
duerfen es aufrufen (R4, D12). `edge start <Einheit>` hebt den Schalter auf: nur an einem Terminal und
nach dem exakten `yes` (der Betreiber; bis D14 eine Regel, keine Wand), loescht den Schalter erst, wenn
die Einheit laeuft, und verweigert eine im Katalog abgeschaltete. Hat die Einheit ein `run.setup`,
fragt es dessen `--check` vor der Rueckfrage; nicht eingerichtet: Exit 1 (unklar: 2), Schalter und
Sperre bleiben.

**Der Schalter** ist eine Datei pro Einheit, `~/.local/state/edge/stopped/<Einheit>`, mit der Zeit des
Ausschaltens (ISO 8601). `edge apply --closed` startet eine ausgeschaltete Einheit nie und loescht
ihren Schalter nie ("stays stopped"); sein Ende ist allein `edge start`. Ein `stop` waehrend der
Rueckfrage von `apply` gewinnt (`apply` liest den Schalter nach dem `yes` noch einmal, `switch.lock`).
Ein `stopped/`-Verzeichnis, das nicht 0700 ist, setzt `edge stop` auf 0700 zurueck. Exit 0 ausgeschaltet
oder schon so; 1 teilweise (Schalter notiert, aber ein Container laeuft noch, oder er liess sich nicht
notieren) oder verweigert; 2 nicht planbar.

**Die Sperre vor Traefik.** Was die Sperre ist, wie sie entsteht und was sie deckt:
[architecture.md](architecture.md), Abschnitt 8, "The ingress barrier". Hier nur, was dort fehlt:
der Eingang `admin` auf `127.0.0.1:8090` bleibt offen; `edge stop` meldet Erfolg erst, wenn
Traefik fuer jeden Host innerhalb von 15 s 404 mit dem Header `X-Edge-Barrier: <Einheit>`
antwortet, sonst Exit 1 (Schalter und Sperre bleiben); `edge start` prueft nach dem Entfernen,
dass der Header weg ist. `edge apply` schreibt oder entfernt nie eine Sperre: eine Einheit, deren Sperre steht, bleibt
aus, auch wenn kein Schalter notiert ist; `edge start` entfernt die Sperre erst, wenn die Einheit laeuft, und loescht den
Schalter zuletzt. Der Beweis auf einem Wegwerf-Stack ist `probe/switches/probe.py`.

**Neustart.** Jeder lang laufende Dienst hat `restart: unless-stopped`; Docker startet einen
ausdruecklich gestoppten Container auch nach einem Neustart nicht, und der Schalter bleibt auf der
Platte.

**Global.** `edge stop --all` nimmt den PC sofort vom Internet. Es schreibt zuerst
`~/.local/state/edge/global-stop` (ein zweites Stop behaelt die erste Zeit) ohne auf `switch.lock` zu
warten, stoppt `cloudflared.service`, schreibt den Schalter unter `switch.lock` noch einmal und meldet
Erfolg erst, wenn `systemctl is-active` den Tunnel als beendet (`inactive` oder `failed`) zeigt. Die
Einheiten laufen lokal weiter, ihre Schalter bleiben. Ohne Terminal laeuft `systemctl --no-ask-password`.
Exit 1 heisst teilweise: "partial", wenn der Tunnel noch laeuft (Schalter bleibt;
`systemctl stop cloudflared`), und "incomplete", wenn das Boot-Drop-in `30-edge-global-stop.conf`
fehlt (ein Neustart oeffnete den Tunnel wieder; `hal setup --apply` installiert es).
`edge start --all` braucht ein Terminal und das exakte `yes` (Agenten nie, R4). Es beweist zuerst die
Sperre jeder noch ausgeschalteten Einheit (bei Bedarf neu geschrieben), holt die Berechtigung des
Systems (`pkcheck`) noch vor dem Loeschen des Schalters, liest ihn unter der Sperre noch einmal, loescht
ihn, startet den Tunnel und prueft, dass er aktiv ist. Kann eine Sperre nicht bewiesen werden, bleibt
der Tunnel zu; schlaegt die Berechtigung oder der Start fehl, steht der Schalter wieder (Exit 1). Nur
der globale Schalter faellt. Die Pruefung des Hosts davor ist noch nicht gebaut ("host check not built
yet (#50 SW3)"). edge liest nie `/etc/cloudflared`, aendert den Tunnel nicht und ruft nie `sudo` auf.
`EDGE_TUNNEL_UNIT` und `EDGE_TUNNEL_MANAGER=user` setzen fuer den Wegwerf-Beweis eine Einheit des
Benutzer-Managers an die Stelle von `cloudflared.service`.

**`tunnel/`** ist der Inhalt, den der Systemschritt von `hal setup` mit `sudo` nach `/etc` installiert;
edge installiert nichts. `20-nonroot.conf` laesst `cloudflared.service` ohne root laufen,
`30-edge-global-stop.conf` startet es nicht, solange `global-stop` existiert, und
`polkit/10-edge-cloudflared-stop.rules` erlaubt dem Betreiber nur das **Stoppen** ohne Passwort, auch
ueber SSH; `start` fragt weiter. Ein blosses `systemctl stop cloudflared` haelt daher nicht: nach einem
Neustart laeuft der Tunnel wieder, denn nur der Schalter ueberlebt ihn. Der Notschalter ist
`edge stop --all`.

**Regel R4:** Agenten rufen `edge apply` und `edge start` nie auf; `edge stop` duerfen sie. Die
Terminalpruefung und das `yes` halten versehentliche und nicht-interaktive Laeufe auf; sie erkennen den
Betreiber nicht (D14).
