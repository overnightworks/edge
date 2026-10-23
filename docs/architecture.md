# Platform concept: from a bare PC to every service live behind the login (16.10.2026)

**Caller:** the operator, 16.10.2026:
- „nimm an ich muss es neu aufsetzen => welche Schritte muss ich wie machen? … was ein Script oder ein Tool machen kann soll es abnehmen … nichts doppelt und logisch getrennt … nach Ausführung sind dann all meine Dienste live hinter meiner Login-Seite und ich kann einfach User verwalten und Dienste an oder ausschalten“;
- „kann das nicht auch ein Script? zumindest durchleiten zu den Seiten?“;
- „all meine Tokens und Passwörter zentral verwaltet und auch checken, dass meine Seiten nur diese Keys haben“;
- „wie läuft das Deploy?“, then: „nicht einfach auf main, sondern wenn es ein Versions-Tag gibt … das den Deploy-Job triggert“;
- „Grafana sollte für die Plattform sein, also alle Dienste“ (ruled 16.10.2026).

**Read-only basis:**
- edge: README, `compose.yaml`, `keycloak/` (`master.yaml`, `realm.yaml`), `backup/`, `src/edge_backup/settings.py`, `systemd/`, CI;
- home-tools: README, `secrets_manifest.toml`, its file tree;
- songmaker: `scripts/auto-deploy.sh` and its system units, `docker-compose.yml`, `src/songmaker_cli/auth_api.py`;
- webauth: `src/webauth/oidc_login.py` (exists);
- presentator: `~/presentator-live` and its user units;
- hopin: README, Makefile, settings;
- issues edge#1, #4, #11, #13, #19 and home-tools#9, #10, #17, #21; hopin#25; presentator#196 and #197;
- the plan `ext-as-code.md` (OpenTofu, E3, its slices listed in the epic's body);
- live state: listeners, containers, timers, `/etc/cloudflared/config.yml`, `/etc/docker/daemon.json`, the operator's groups (`docker`, `sudo`), names in `~/.secrets` (names only), key names (not values) in the `.env` files.

No secret value was read. No write API was called.

**How to read it.** Each word in **bold caps** is one of the three places: **LOOK**, **PEOPLE**, **CHANGE**. "Today" means it exists and runs. "Planned" means a ruled plan or an open item. "Proposed" means this document proposes it and the operator decides (section 9).

---

## 1. The picture

> **The whole system on one page.** A solid box exists today. A dashed box is planned and not built yet. Thick arrows are the operator's three places: LOOK, PEOPLE and CHANGE.

```mermaid
flowchart TB
    classDef planned stroke-dasharray: 6 4
    classDef place fill:#fff4c2,stroke:#a07800,stroke-width:2px

    subgraph humans["People"]
        OP["Operator<br/>browser, phone, terminal"]
        VIS["Users and guests<br/>browser"]
    end

    subgraph ext["L3 External services: OpenTofu from fleet.yaml"]
        DNS["Cloudflare DNS<br/>hallucinai.de and *.hallucinai.de"]
        ACC["Cloudflare Access<br/>planned (X8b): * = operator only, Google + MFA<br/>today: no Zero Trust, auth goes live as public"]
        TUN["Cloudflare Tunnel<br/>ingress per D6 (planned, X6a #168)"]
        GH["GitHub overnightworks<br/>repos, rulesets, Actions, org secrets"]
        SC["SonarCloud<br/>projects by hand"]
    end

    subgraph pc["The PC"]
        subgraph l0["L0 PC and tools: home-tools"]
            HOME["hal setup / home check"]
            SEC["L1 secrets: ~/.secrets<br/>inventory: secrets_manifest.toml"]
            CFD["cloudflared.service"]
            AGT["Agent user (D14)<br/>no docker, no ~/.secrets"]
        end
        subgraph l2["L2 Platform: edge compose.yaml"]
            CAT["fleet.yaml<br/>the service catalog"]
            TRF["Traefik 127.0.0.1:8081<br/>the tunnel's only target"]
            ADM["Traefik 127.0.0.1:8090<br/>this PC only"]
            KC["Keycloak + its database<br/>auth.hallucinai.de: the login page"]
            CA["step-ca :9000, internal network mcp-ca<br/>internal CA for the MCP origins, *.mcp.internal"]
            TMC["traefik-mcp :443, no host port<br/>the gateway to the MCP origins, certificates from step-ca"]
            MCPN["internal networks:<br/>mcp-ca (step-ca, traefik-mcp)<br/>mcp (traefik-mcp, later the agent service; isolated)<br/>mcp-backends (traefik-mcp, the MCP servers)<br/>agent, egress (the agent service; isolated)<br/>internet, a NAT bridge: agent-egress alone"]
            BAK["hal backup<br/>restic weekly, restore test monthly<br/>second copy off this disk (D3)"]
            LOOK["Traffic-light page, Gatus<br/>is it OK now?"]
            MON["Prometheus, Grafana, Alertmanager<br/>why, since when? every service"]
            DEP["Deploy watcher per service<br/>follows the newest v* tag"]
        end
        subgraph l4["L4 Services: each repo owns its compose.yaml"]
            SONG["songmaker<br/>songmaker.hallucinai.de, own login until its cutover"]
            PRES["presentator<br/>offline until its legal pages"]
            HOP["hopin<br/>video calls"]
        end
        DATA[("L5 data<br/>volumes and ~/backups/restic")]
    end

    VIS --> DNS
    OP --> DNS
    DNS --> ACC --> TUN --> CFD
    CFD -->|"songmaker's host, today straight to :8080"| SONG
    CFD -->|"every other name"| TRF
    TRF -->|"container label edge.name"| KC & PRES & HOP & LOOK & MON
    SONG & PRES & HOP -.->|"check the ID themselves: webauth, OIDC, group /apps/name"| KC
    MON -.->|"metrics"| SONG & PRES & HOP & KC
    TMC -->|"ACME TLS-ALPN-01, 24 h certificates"| CA
    TMC --- MCPN

    OP ==>|"PEOPLE: Keycloak admin"| ADM
    ADM --> KC
    OP ==>|"LOOK"| LOOK
    LOOK -.->|"one push channel to the phone"| OP
    MON -.->|"alerts, the same channel"| OP
    OP ==>|"CHANGE: pull request, merge"| GH

    GH -->|"edge release sets a v* tag"| DEP
    DEP --> SONG & PRES & HOP
    CAT -->|"edge apply, run by the operator"| DEP
    CAT -->|"edge external apply, inside edge apply"| DNS & ACC & GH
    GH -->|"CI"| SC

    HOME -->|"installs"| CFD & l2 & AGT
    SEC -.->|"files, never values in repos"| CFD & KC & SONG & BAK & CA
    BAK -.->|"dumps"| KC & SONG
    BAK --> DATA

    class LOOK,DEP,CAT,HOP,MON,AGT planned
    class LOOK,ADM place
```

**What the picture says, in six sentences:**
1. **The way in.** Every name ends in one tunnel. Cloudflare Access is planned to block every name that is not explicitly opened (`*` = operator only, X8b on #30); the operator ruled on 20.10.2026 that Zero Trust is not needed for now and can be added at any time, so today `auth` goes live as `public` and the `*` protection waits for X8b. Traefik on loopback hands each name to the container with that `edge.name` label.
2. **The login.** Keycloak at `auth.hallucinai.de` is the one login page. Every app checks the ID itself through webauth and its group `/apps/<name>`. songmaker joins only through its own cutover (section 3); until then it keeps its own login. Tools without their own login will sit behind Access (X8b); no login-less tool is published before it.
3. **PEOPLE.** People and their groups exist only in the Keycloak admin, `http://localhost:8090/admin/`, on this PC (later via Tailscale).
4. **CHANGE.** Every change is a pull request. A release (`edge release` sets a `v*` tag) is deployed by the service's watcher; whether the service is then reachable depends on its state (section 8 state table). The platform (edge) and the external services change when the operator runs `edge apply`.
5. **LOOK.** One traffic-light page answers "is it OK now?" for every service, the backup, the drift checks and the secrets. Grafana answers "why, and since when?" for every service. Both send alerts through one push channel to the phone.
6. **Agents.** Today agents run as the operator's user and can do whatever he can. Every "an agent may not" in this document is a request until D14 gives them their own user.

---

## 2. The layers

Each layer has **one** owner. The order is the order of a setup: a layer needs only the layers above it.

| Layer | Holds | One source of truth | Bring up | Check | Stays manual, and why |
|---|---|---|---|---|---|
| **L0 PC and tools** | Ubuntu packages (Docker with loopback default, cloudflared from apt, gh, uv, restic, the pinned `tofu`, Tailscale); `/etc/docker/daemon.json`; edge's `tunnel/` (the cloudflared unit, its drop-ins, the stop-only polkit rule); linger; auto-login off; the AppArmor profile; the agent user (only with D14); the `home` and `aco` tools; the work clones in `~/git` | home-tools: the setup playbook (home-tools#9; Ansible recommended there; it installs edge's `tunnel/`) | `~/git/home-tools/bootstrap` (new, home-tools#9), then `hal setup --apply` (without `--apply`: dry run) | `home check` (daily timer, today) | OS install and disk encryption (the installer decides it); BIOS "power on after AC loss"; the first `gh auth login --web`, because cloning the private home-tools needs it |
| **L1 secrets** | every token, password and key; values only in `~/.secrets`; tool logins in their tools' stores. Directories 0700. Files 0600, except a file a container user must read: 0644 inside its 0700 directory (docs/keycloak.md). Today every agent can read all of it (D14) | `home-tools/src/home_tools/secrets_manifest.toml` (names, purpose, reader, scope, bindings, rotation; never a value) | `hal setup --apply` (its step „Geheimnisse“): walks every missing secret page by page (home-tools#10) | `home secrets`, part of `home check` (today: local view; the remote check is section 4) | creating a token at the provider: a human logs in and clicks. The wizard opens the page, takes the value hidden, verifies it, stores it |
| **L2 platform** | Traefik, socket proxy, Keycloak and its DB, step-ca (the internal CA for the MCP origins) and `traefik-mcp` (the second Traefik, the only container on the three internal networks `mcp-ca`, `mcp`, `mcp-backends`; no host port), `legal` (nginx serving the imprint, the privacy notice and the terms of every app from `legal/`, on its own internal network; no host in the catalog yet, so nothing is public), edge-backup timers, the deploy watchers, the LOOK page (Gatus: "is it OK now?"), the monitoring stack for every service (Prometheus, Grafana, Alertmanager: "why, since when?"; moves out of songmaker, ruled 16.10.2026, edge#4), one alert channel | edge: `compose.yaml`, `traefik.yml`, `traefik-mcp.yml` and `mcp/dynamic/`, `keycloak/*.yaml` (realm as code), `legal/` (pages, stylesheet, nginx config), `step-ca/ca.json`, `backup/apps.toml`, `systemd/` | `edge apply --closed` (start without publishing), then `edge apply` (publish), then `edge start --all` (open the tunnel; section 6 step 11). Proposed: one command for compose, realm apply, timers, services on/off, external | `edge check` (proposed: `edge-check` today, plus readiness, backup freshness, deploy state) | the Keycloak go-live once (edge#19: it creates the permanent admin from a typed name and password; TOTP and passkey in the browser) |
| **L3 external** | Cloudflare DNS, Access apps and policies, tunnel ingress (D6), the zone's HSTS setting; GitHub repo settings, rulesets, Actions policy, org secret names and reach | edge `fleet.yaml` + OpenTofu (`ext-as-code.md`, epic E3, its slices listed in the epic's body) | the publish phase of `edge apply` (operator, TTY, the word `yes`) | `edge external check` → `home check` | secret values, the tunnel object and its credential, Access identity providers, the Access group's one member, SonarCloud projects, CodeQL default setup, GitHub App installs, the registrar. Each has no provider or would put a secret into state |
| **L4 services** | songmaker, presentator, hopin, the agent service (its compose service and image recipe live in edge; switched on by its unit in `fleet.yaml`), later the aco board | each service repo's `compose.yaml` (containers) + its entry in `fleet.yaml` (where it runs, who may use it, on or off) | the deploy watcher (newest `v*` tag) after `edge apply` turned the service on | the LOOK page (each service's health URL); Grafana for its metrics | creating a new repo's SonarCloud project (one click) |
| **L5 data** | Docker volumes (songmaker DB and audio, Keycloak DB; `step-ca` ([internal-ca.md](internal-ca.md) names its backup) and `traefik-mcp-acme`, which is not: its certificates are issued again), the restic repository and its off-disk copy (D3), the encrypted OpenTofu state | edge `backup/apps.toml` | `hal restore …` (section 6) | the restore test (monthly), the clean-machine drill (section 6), and the backup's freshness on the LOOK page | the restic password, the OpenTofu passphrase and the disk's recovery key live in the password manager: without them no backup, no state and no disk can be read |
| **People** | users, passkeys, group memberships (`/apps/<name>`, `/services/agent-service/subscription`, `/services/agent-service/api`) | the Keycloak database (managed only in the admin console); the groups themselves are code in `keycloak/realm.yaml` | first-ever: create people in the admin console; re-setup: restore the Keycloak DB | Keycloak events (login anomalies) on the LOOK page | everything: people are never code (ruled) |

**Who calls whom (no step twice):**
- `hal setup` installs the tools and clones edge. It creates the agent user only after D14 is ordered; until then the user is D14's step, not setup's. It installs cloudflared disabled and never starts a service.
- `hal setup --apply` (its step „Geheimnisse“, the wizard) creates secrets. Nothing else writes into `~/.secrets`, except `keycloak/go-live` for the three Keycloak service passwords and one secret per confidential app client (edge#19 rule 2, edge#106), `step-ca/init` for `~/.secrets/step-ca/` (edge#38), `agent-service/setup` for `~/.secrets/agent-service/` (edge#234), which also creates `~/.secrets` itself and narrows it to 0700, as `secrets_view.LOOSEST_DIRECTORY_MODE` judges and `hal restore` on a re-setup.
- `edge apply` is **the one publisher**. `edge apply --closed` runs only the local part: the platform and the enabled services. `edge apply` without `--closed` runs the local part, then its publish phase: `edge external apply` and its check of each host against the catalog (built, edge#171). The two run one after the other and each asks its own `yes` only when it has changes (ruled 20.10.2026); with nothing to change `edge apply` asks nothing. A local part that does not end with exit 0 publishes nothing; the exit is then its own, and a publish exit 2 becomes 1, so a 2 from `edge apply` means the local part could not finish planning. It never creates a secret: when a service's setup is missing it asks **separately** ("step-ca has no keys yet. Create them now? (yes)") and then delegates to the existing owner, `step-ca/init` or `keycloak/go-live`, which keep the key generation, and the operator runs `edge apply` himself. For `--closed` this is built ([edge#69](https://github.com/overnightworks/edge/issues/69)): the question comes before the plan, the setup runs in the operator's terminal with its output uncaptured, `--check` runs again, and a fresh plan follows with its own `yes` (R2, ruled 16.10.2026, [edge#50](https://github.com/overnightworks/edge/issues/50)). `keycloak/go-live` never publishes.
- The deploy watcher deploys newer tags and nothing else.
- What `edge apply`, `edge start` and the watcher may do in each state: section 8 state table.
- `home check` only reads. It runs every checker: tidy, secrets, edge-check, external drift.

### The agent service (A1d) in the layers

The agent service is an L4 service: the one place that holds the subscription logins and runs the provider CLIs for every app ([agent-providers#18](https://github.com/overnightworks/agent-providers/issues/18)). It sits behind Keycloak (its client `agent-service`), reaches MCP servers only over https through `traefik-mcp` (edge#37 slice 2), and trusts the internal CA's root (section 4). It runs from edge's `compose.yaml` (the service `agent-service`, profile `agent-service`) with an image built on the PC; the unit `agent-service` of `fleet.yaml` starts it with its egress gateway. Six rulings of 16.10.2026 shape it. The library contract stays owned by agent-providers; each line here only states the ruling and links its owner.

1. **Closed environment.** Every CLI child is started by the library's one spawn owner with an explicit environment, an absolute binary and an explicit cwd, and inherits nothing from the host ([agent-providers#10](https://github.com/overnightworks/agent-providers/issues/10)).
2. **One private root.** Everything a child may write lives below the host's `cli_working_directory_root` (0700): the private credential homes, `TMPDIR` and the Codex sandbox directories; the library owns the layout below the root and the host owns the root ([agent-providers#10](https://github.com/overnightworks/agent-providers/issues/10), [agent-providers#49](https://github.com/overnightworks/agent-providers/issues/49); the host's owner of the root is Songmaker, [songmaker#1476](https://github.com/overnightworks/songmaker/issues/1476)).
3. **Sandbox policy from that root.** The host derives its AppArmor profile and Bubblewrap mounts from the root and allows writes only below it, for songmaker today and for the agent-service container in A1d, because a closed environment is not access control and hiding the operator's real `~/.claude` is a deployment matter of one read-only directory bind per profile ([agent-providers#10](https://github.com/overnightworks/agent-providers/issues/10), [agent-providers#18](https://github.com/overnightworks/agent-providers/issues/18)).
4. **MCP origins: https only, with step-ca** ([edge#37](https://github.com/overnightworks/edge/issues/37), option d).
5. **No provider text in logs.** Provider text never reaches a service or library log or an exception chain; the host receives a sealed excerpt and alone decides whether to write it down ([agent-providers#9](https://github.com/overnightworks/agent-providers/issues/9), [agent-providers#56](https://github.com/overnightworks/agent-providers/issues/56), [agent-providers#68](https://github.com/overnightworks/agent-providers/issues/68)).
6. **The library defines its contract, hosts adapt to it** ([agent-providers#49](https://github.com/overnightworks/agent-providers/issues/49)).

**What this means for the platform (A1d):**
- The realm carries the callers' identity (#183): the realm role `agent-workload` held only by the service accounts of the songmaker and presentator clients, whose default scopes add the `agent-service` audience, so an app's workload token has the same `azp` as its people's tokens. Whoever holds such a client secret can get that token, which is why the secret stays the app's own file.
- The container mounts only one read-only directory bind per profile (its login), its configuration file and two tmpfs mounts: `cli_working_directory_root` and `/tmp` (section 4).
- Its AppArmor/Bubblewrap policy permits writes only below that root.
- Its egress is exactly two names, through `agent-egress` ([edge#189](https://github.com/overnightworks/edge/issues/189)): an SNI-allowlisting TLS passthrough to the provider endpoint `api.anthropic.com` and the issuer host for the JWKS fetch. The service sits on internal, isolated networks with no route out, no host address and no DNS forwarding. MCP origins stay internal over `mcp`; LiteLLM joins with A2a.

---

## 3. The service catalog

### One file, one entry per repository

The catalog is **`fleet.yaml`**, the manifest that epic E3 already plans for OpenTofu. A second catalog would duplicate the repo list and the host names. E3 is being built slice by slice (state: #30); `fleet.yaml` is already this catalog, so there is no second one.

**Four changes to the E3 plan** (made once, when E3 is opened):
- **Location.** `fleet.yaml` moves from `external/` to the edge root, because L2 (deploy, LOOK) reads it too. OpenTofu reads `../fleet.yaml`.
- **A `run` block** on the entries that run on this PC. A library such as webauth has no `run` block, only `repo`.
- **Entries, not repositories, are the unit** (P1, P2, P14, ruled in [edge#50](https://github.com/overnightworks/edge/issues/50)). Platform units are entries keyed by their compose profile (`step-ca`, `keycloak`; socket-proxy and traefik are the always-on core), and repositories are entries with `repo`. One repository can carry several switchable units. `src/edge/catalog.py` owns the typed `run` schema (`enabled`, `setup`); `run.setup` names the key command behind its `--check` contract (edge#52 rule 8). Images, volumes and ports are never repeated in the catalog. `docs/naming.md` records that catalog keys equal compose profile names (P5).
- **The tunnel mode is decided once, by D6 here**. It replaces E3's D5. D6 is ruled („Cloudflare steuert“, 18.10.2026): Cloudflare manages the configuration, the JSON credential stays, and OpenTofu writes the ingress lines from the catalog. That is planned, not live, until X6a's live apply (#168, L2); the catalog owns the whole tunnel configuration (D6).
- **X8: go-live does not publish**. E3's X8 makes `keycloak/go-live` step 8 call `edge external apply`. That would publish inside the go-live, before the re-setup's later publish step (section 6, step 11). Changed: go-live ends unpublished; the publish phase of `edge apply` is the only publisher. X8's "apply refuses while `auth` does not answer" stays, now inside `edge apply`.

```yaml
services:
  hopin:
    repo: {default_branch: master}   # the release branch; `visibility` (GitHub settings, E3, X2) is planned
    run:
      enabled: true              # Dienst an/aus: the one switch
      # setup: step-ca/init      # only where a unit needs key material (step-ca, keycloak)
    hosts:
      - name: hopin              # hopin.hallucinai.de → container labelled edge.name=hopin
        exposure: bypass         # public, own login; the * Access app would otherwise cover it
        reason: "video calls; starting a call needs a login (hopin#25)"
        ready: /health           # optional: the path X8a asks before it publishes the host
        # target: http://127.0.0.1:8081   # optional; this is the default (Traefik)
```

Planned and not accepted by `catalog.py` yet: `run.login_group` (a group of `keycloak/realm.yaml`, checked by CI), `run.health` (the LOOK page's path) and `run.deploy: tags` (the watcher, section 5). An app's `run` takes only `enabled` (no `setup`), its `repo` only `default_branch`, and `github.org` names the organisation of its repository.

`src/edge/catalog.py` (`hosts()`) owns the host shape. `edge external plan` and `apply` refuse an invalid catalog before any token is read; `external/hosts.tf` reads the valid shape.

| Field | Type | Default | Rule |
|---|---|---|---|
| `name` | string, required | none | `@` (the zone's apex), `*` (the wildcard) or one lowercase DNS label, no dot; `edge-host-check` is reserved (the host check asks the wildcard there), also in a disabled entry |
| `exposure` | `public` \| `bypass` \| `protected`, required | none | `protected` only on `*`, and `*` only `protected`; `public` on a label is refused while `*` exists (it needs its own `bypass`); one FQDN once |
| `reason` | string | none | required and non-blank for `public` and `bypass` |
| `ready` | path | none | starts with one `/`; the publish guard of X8a |
| `target` | `http://127.0.0.1:<port>` or `http://localhost:<port>` | `http://127.0.0.1:8081` (Traefik) | never `:8090` (the admin entrypoint); another target only for a gap-35 host (songmaker's `songmaker`) |
| `deny_public` | list of paths | none | each lowercase and absolute (`[a-z0-9._~-]` segments), no query, no trailing slash. Cloudflare refuses these paths at its edge, before the tunnel, one rule per path for every host listing it, with their encoded, cased and multi-slash variants (`docs/external.md`); songmaker's one host lists `/auth/backchannel-logout` (#213) and `/metrics` (#342) |

The apex and `www` are no hosts but the catalog's `cloudflare.redirect` (validated next to the hosts by `src/edge/catalog.py`): `hosts` (`@` or labels, none of them a service host), `target` (an https URL) and `keep` (each a lowercase `path` with an https origin as `target`). Cloudflare answers 302 at its edge, for https only (`http://` first goes to https on the same host): a kept path, with or without a trailing slash, goes to its origin with the path kept, everything else to `target`, path and query dropped; old links keep no path (operator ruling of 18.10.2026). The names keep proxied records and have no tunnel line (`docs/external.md`). Keycloak's bare address already redirects to the account console, so `target` is `https://auth.hallucinai.de/`; `/impressum` and `/datenschutz` go to songmaker's pages until #110 serves them.

`run.enabled` decides both the units and the hosts: only `true` and `false` count: OpenTofu's `yamldecode` also reads YAML 1.1 words (`yes`, `no`, `on`, `off`, `y`, `n`, `True`) as booleans and PyYAML disagrees on some of them, so every other spelling, `no` and `"false"` included, is refused. A disabled entry's hosts are validated but not published.

### What the catalog drives, and what it must not duplicate

| Fact | Owner | The catalog… |
|---|---|---|
| Containers, images, ports, volumes, the `edge.name` label | the service repo's `compose.yaml` | names only the host. `edge check` reports an enabled host without a running container that carries its `edge.name`, a running `edge.name` that no enabled host lists, and a long-running service container whose restart policy is not `unless-stopped` (section 8: switches must survive a reboot). One-shot jobs are declared `jobs = [...]` in the entry (songmaker: `migrate`, `restart: "no"`) and are exempt |
| Keycloak groups | `keycloak/realm.yaml` | references `login_group`. A CI check in edge fails when a `login_group` is missing from `realm.yaml`, or when an `/apps/*` group has no entry |
| What to back up | `backup/apps.toml` | says nothing. A CI check fails when an `apps.toml` app has no catalog entry |
| DNS, Access app, tunnel line, repo settings | the catalog itself (rendered by OpenTofu) | is the owner |
| The LOOK page's service lights, Prometheus's scrape targets | the catalog | `edge apply` renders the Gatus endpoint list and the scrape list from the enabled entries |
| The deploy watchers | the catalog | `edge apply` installs one watcher per enabled entry with `deploy: tags` |
| The release branch | the catalog (`repo.default_branch`) | `edge release` tags that branch; hopin's is `master` |

**Why the catalog does not generate `realm.yaml`.** keycloak-config-cli adds and changes and deletes no undeclared top-level object or person: every `IMPORT_MANAGED_*` type is `no-delete` (`compose.yaml`), so a group missing from the YAML stays with its members (docs/keycloak.md). Even so, `realm.yaml` stays the group owner. Switching off never touches a group. Deleting a group is done in the admin console, after a backup, with the operator's yes.

### "Dienst an/aus" end to end

**Off** (example: hopin):
1. **CHANGE.** The operator, or an agent on his word, opens a PR in edge: `enabled: false`. CI checks the catalog rules. He merges.
2. **He runs `edge apply`.** It shows the plan and asks for `yes`:
   - hopin's watcher is removed;
   - hopin's services are stopped and removed, exactly the services the unit resolves to (P13, [edge#50](https://github.com/overnightworks/edge/issues/50)): never a project-wide `down`, which would also take shared services such as Traefik, and never `--volumes`, so the named volumes stay;
   - the LOOK page drops its light, Prometheus drops its target;
   - `edge external apply`: the DNS record, Access app and tunnel line go.
3. **Result (R1, ruled 16.10.2026, [edge#50](https://github.com/overnightworks/edge/issues/50)).** "Off" removes a service's containers and never its volumes, data mounts or keys; "on" again uses the same identity and data. `edge apply` never deletes a volume; removing one stays a manual command. hopin is in state E (section 8 state table): closed as soon as its container is gone, before the external part finishes.

**On:**
1. A PR sets `enabled: true`. He merges and runs `edge apply`.
2. `hal apply` fetches hopin into `~/srv/hopin` (`src/edge/deploy_checkout.py`), takes its newest release (`vYYYY.MM.DD.N` on origin's default branch, `src/edge/releases.py`) and shows tag and full sha in the plan. After the operator's `yes` (the gate: whoever can put a tag on a merged commit offers code to run here) it checks the tag out detached, builds, creates and starts the containers under the switch lock, waits for the container's health and only then records the last successful deployment (`~/.local/state/edge/deployed/<key>`: tag, sha, time). A new tag that fails stays: the run is partial and the fix is a newer tag. No output of Compose is ever printed: a failed step names step, tag and exit code (`src/edge/apps.py`, `src/edge/app_compose.py`). The app's `compose.yaml` is held to one contract before the `yes` (`src/edge/app_contract.py`, a pure check over the file at the tag; a failure means cannot plan, and every line names entry, tag, service and key, never a value): present at the root and self-contained (no `include`, no `extends`, no `profiles`, `name` absent or the key); each host of the entry has exactly one service labelled `edge.name: <host>` with `traefik.enable: "true"`, and that service has a `healthcheck`; no `ports`, no `network_mode: host`; `restart: unless-stopped` on every service; no volume entry without a source; a `$` where a value decides a rule is refused: in a label key, `edge.name` and `traefik.enable`; in any short-form volume entry (`source:target[:mode]`, which Compose splits only after interpolating, so write it in the long form), and a short-form entry whose first section is one letter is refused too (Compose reads it as a Windows drive, so write it in the long form); in a long-form volume's `type`, `target` and the `source` of a volume-type mount (a bind's `source` and `read_only` may interpolate); in a tmpfs mount; and in the healthcheck's `test` and `disable` of a labelled service. `traefik.enable` counts only as `true`. The contract is a tripwire for cooperative app heads: it binds the cases it lists, and the boundary that would stop a non-cooperative one is D14, which is not built. A one-shot (`restart: "no"`) and a `depends_on` that waits for health or completion are refused for now. After build and pull and before `create`, an image-declared `VOLUME` that no named volume, bind or tmpfs of its service covers ends the run partial with nothing started and the record unchanged. Every host of an app in `fleet.yaml` carries `ready`; the app planning refuses one without it before the `yes`.
3. The publish phase then publishes the name, behind its `ready` guard (X8's "protection before reachability"). hopin is in A, or in C under a global stop (section 8 state table).

**The realm apply (R3, ruled 16.10.2026; re-cut by the operator's „ja d“ and „ok“ on 21.10.2026, [edge#50](https://github.com/overnightworks/edge/issues/50), [edge#219](https://github.com/overnightworks/edge/issues/219))** adds and changes in the realm and deletes no undeclared top-level object and no person:
- it runs only when an input changed, and does nothing otherwise (R3, ruled 21.10.2026): the entry of `keycloak-config-cli` in `applied.json` is missing, unfinished or differs from the digests of `realm.yaml`, `master.yaml` and config-cli's resolved definition, or Keycloak's own import-checksum attributes differ from the ones recorded after the last apply (a restored database, a go-live import). A rotated client secret alone does not trigger it. Once it runs, config-cli reconciles for real (`IMPORT_CACHE_ENABLED=false`);
- every `IMPORT_MANAGED_*` type is `no-delete`, and a listed user keeps the realm roles and groups the console gave it (client roles are reset), so the tool deletes no undeclared group, client, role or top-level flow and no person, and the reset below is the only loss;
- a Keycloak-only backup (`hal backup keycloak --pre-apply --json`, the last ten pre-apply snapshots kept) and its restore test come before it, as a step inside `hal apply` (R2); a failure of either applies nothing;
- one line per change comes after it, and a reset-class removal is printed as „reset to the file“ with the snapshot ID, exit 0 (R4); the list of what exists only in Keycloak follows the change lines, only when the step runs, without changing its outcome (`src/edge/realm/declared.py` reads the declared identities, `reconcile.py` compares them with the observer's);
- deleting anything in Keycloak waits for the operator's yes.

What the file declares belongs to the file: a console change to a declared object is reset by the next apply that runs the realm step, together with what hangs off it (a step or subflow in a declared flow, a mapper on a declared scope or client, a default or optional scope on a declared client that lists its scopes, a client role on a listed service account, a console-enabled service account on a declared client, a flow binding override on a declared client). What the console creates on its own stays: people, their memberships, and the groups, clients, roles and top-level flows the file does not declare. No person is ever deleted (a service-account user that a console-enabled service account on a declared client created goes with that reset). A change to a declared object goes into the file; the blockquote in docs/keycloak.md after „Theme and language“ lists the measured examples, and the Keycloak probe's proofs 3c and 3d pin them.

**Today's switch** is different: "off" means removing a tunnel line by hand, as on 14.10.2026 for presentator. Planned with X6a (#168): the ingress comes from the catalog, so a line is no longer removed by hand. Before presentator is published, its own Access app (its people, plus the `/sources/*` bypass) must still answer 401 to a request without a session; the operator checks that himself. The wildcard `*` protection is X8b.

**Emergency switches are separate:** `edge stop hopin` takes it offline at once without a code change. Guards: section 8 state table.

### Hopin as the worked example: what it needs before its entry works

- **hopin#25 first:** login through webauth (`/apps/hopin`), Impressum and Datenschutz.
- **A `compose.yaml` and a Dockerfile** with `edge.name: hopin`, `restart: unless-stopped` and **no published port**. Today hopin has neither and runs `uv run hopin` on port 8080, which songmaker already uses. Traefik reaches the container by its bridge IP, so no host port is needed.
- **Its first release:** `edge release hopin` tags the green tip of `master`.
- **The catalog entry above;** then `edge apply`.
- **Its SonarCloud project** already exists: hopin has `sonar-project.properties`.

### songmaker: from its own login to the one login

**Today songmaker signs people in itself.** It uses webauth's password login (`src/songmaker_cli/auth_api.py`: setup, login, password change). Its users and password hashes live in songmaker's own database. `realm.yaml` holds songmaker's client before the go-live, beside `agent-service` and the hopin and presentator clients (#161). So deploying songmaker behind the platform changes nothing for its users. D4 ("Keycloak for every app user") needs a cutover of its own.

**The cutover item** (songmaker; neighbours edge#19 and webauth; after go-live):
1. **Client.** edge's `realm.yaml` holds the `songmaker` client (#108), with its redirect URI on `songmaker.hallucinai.de`. Its secret is the file `~/.secrets/keycloak/clients/songmaker`.
2. **Login.** songmaker switches to webauth's OIDC login (`webauth/src/webauth/oidc_login.py` exists) and checks the group `/apps/songmaker`.
3. **Accounts.** Each songmaker user row gains the Keycloak user ID beside its own ID. Songs and albums stay keyed by songmaker's own ID, so no data moves. The operator first creates each person in Keycloak. A one-time mapping then pairs the two by username. He confirms each pair; nothing is matched silently. The admin role comes from a Keycloak group; the exact rule is a line in the item's expectation list.
4. **Retire the old login.** After every user has signed in once through Keycloak, the password form goes and the stored password hashes are deleted (deletion: spoken yes).

**Done when:**
- the mapping ran first on a restored copy of songmaker's data, with the same result;
- every existing songmaker user signs in through `auth.hallucinai.de` and sees their own songs;
- a person without `/apps/songmaker` is refused;
- the old password login answers 404.

---

## 4. The secret inventory and its check

### One inventory

`secrets_manifest.toml` (home-tools) stays the one inventory. Values stay in `~/.secrets`. It gains five fields per entry:

| Field | Meaning | Example |
|---|---|---|
| `purpose` | one sentence: what breaks without it | "SonarCloud analysis in CI" |
| `scope` | the permissions it was created with, as declared | `["sonarcloud:analyze"]`; `["org:secrets:read", "repo:metadata:read"]` |
| `bound_as` | exactly where each app receives it: a compose secret file, or a named environment variable | `{app = "edge", file = "keycloak/db-password"}`; `{app = "presentator", env = "PRESENTATOR_SECRET_KEY"}` |
| `placed_at` | where a copy lives at a provider, by name | `github-org-secret:SONAR_TOKEN` |
| `max_age_days` | rotation rule (home-tools#10 line 15: a date only with provenance) | `365` |

Each app also gets one list of **plain keys**: the `.env` and `environment:` keys that are configuration, not secrets (example: `POSTGRES_USER`). Every key an app reads is then either a declared binding or a declared plain key. Nothing is judged by its name.

Today's fields stay: `name`, `kind`, `paths`, `read_by` (= consumer), `recreate`, `verify`, `retired_on`.

**The internal CA's files (edge#38).** `step-ca/init` creates them once; `src/home_tools/secrets_manifest.toml` names them, with `loosest_mode = 0o644` for the files a container user reads, so the daily `home check` judges them against that mode and never recommends `chmod 600`. Presence stays with each unit's own `--check`: every one of these entries is `optional`.

| File | Mode | Who reads it | In the backup |
|---|---|---|---|
| `~/.secrets/step-ca/root_ca_key` | 0600 | only `step-ca/init`, in a one-off container without a network; never mounted into the running CA | planned (Block 9) |
| `~/.secrets/step-ca/root_ca.crt` | 0644 | `traefik-mcp` (uid 65534, as the compose secret `step_ca_root_certificate`) and the agent service as the trusted root | planned (Block 9) |
| `~/.secrets/step-ca/password` | 0644 in the 0700 directory | the CA, through `/run/secrets/step_ca_password`; it protects the root and the intermediate key alike | planned (Block 9) |
| volume `step-ca`: `certs/`, `secrets/intermediate_ca_key`, `db/` | uid 1000 | the CA | planned (Block 9) |
| volume `traefik-mcp-acme`: `acme.json` (the issued certificates and their keys) | uid 65534 | `traefik-mcp` | no: they are issued again |

**The agent service's mounts (A1d).** Its container binds two things from the host and nothing else (the login directory and its `service.toml`), plus two tmpfs; the rulings are in section 2. It runs as `1000:1000`, the operator's uid on this PC, because its reader refuses a login file that group or others can touch.

| Mount | Holds | Mode | In the backup |
|---|---|---|---|
| one read-only directory bind per profile, `~/.secrets/agent-service/<profile>/` (the file inside is replaced, the directory never) | that profile's subscription login, one file | the container user only (0700 directory, 0600 file); no other profile and not the operator's `~/.claude` | no: the login is restored from its `~/.secrets` source ([agent-providers#18](https://github.com/overnightworks/agent-providers/issues/18)) |
| `./agent-service/service.toml` → `/etc/agent-service/service.toml`, read-only | the service's configuration | the checkout's | no: it is in the repository |
| `cli_working_directory_root`, a tmpfs | the private credential homes, `TMPDIR`, the Codex sandbox directories | exactly 0700, owned by the container user (the service refuses to start otherwise); the library owns the layout below it | no: rebuilt on start |
| `/tmp`, a tmpfs, also its `HOME` | nothing the service keeps | 0700, owned by the container user | no |

**One split keeps it single.** The manifest owns *what a secret is and where an app receives it*. `fleet.yaml` owns *where GitHub installs it and which repos see it*: the org secret name and its reach (E3, X5). The value is set by `home secrets --rotate` (home-tools#10 line 19). Its reach is checked by `edge external check`. home-tools#10 line 19's own "repo list drift" view is therefore dropped; the reach check lives once, in X5.

### The check: `home check` compares the inventory with reality

| Finding | Meaning |
|---|---|
| `missing` | the manifest names it, reality lacks it |
| `extra` / `undeclared` | reality has it, the manifest does not name it (for app keys: neither a binding nor a plain key) |
| `over-privileged` | reality grants more than `scope` declares |
| `stale` | older than `max_age_days`, expiring within 30 days, or unused for 90 days |
| `unverified` | the check cannot prove the deployed value equals the central one, and says so |

Every finding names the subject and a page to fix it, never a value.

**What the check can prove, and what not.**
- **A compose secret file is `bound_as`** when the container's mount source is exactly the declared `~/.secrets` path. That proves the central file is what the container gets, not that the app reads it: **consumption stays `unverified`** unless the app exposes a check (a health field naming the secret's fingerprint, never its value). The check reads only the mount path (`docker inspect` `.Mounts`), never the file.
- **An inline literal in compose is a finding.** It is reported by key name only, value redacted, and never printed or logged.
- **An environment variable is `unverified`,** always. Its value comes from a `.env` file or the shell. Metadata cannot show it equals `~/.secrets`. The fix is to move it to a compose secret file (gap row 21); then it becomes `bound`.
- **A copy at a provider is `unverified`,** always (GitHub org secrets, Actions secrets). GitHub shows only `updated_at`. The check shows that date beside the local file's date, as a hint, never as proof.

**Safe extraction, so no value is ever printed:**
- compose: the file is parsed by the check's own YAML reader, which keeps `secrets:` paths and `environment:` key names and drops every value at once. `docker compose config` is never run, with or without `--no-interpolate`: those flags stop variable resolution, they do not redact literals written in the file.
- `.env` files: a parser that keeps each key and drops its value at once.
- containers: `docker inspect` mount sources only; never `.Config.Env`, which holds values.

| Source | What can be read, read-only | Credential needed | Findings |
|---|---|---|---|
| **`~/.secrets` on disk** | names, modes (`stat` only; today) | none | missing, extra (today: `unknown-file`), loose mode (0600; 0644 only where the file's manifest entry declares it as its `loosest_mode`, inside a 0700 directory; `bound_as` is the planned decider for compose files) |
| **App secrets in compose** | the safe extraction above, for every enabled service: `secrets:` file paths, `environment:` keys, `.env` keys in the deploy checkout, mount sources | none | `bound_as`, `unverified`, `undeclared`, `missing`, `inline-literal` (key name only). **Today every key in songmaker's and presentator's `.env` is `undeclared`,** because the manifest declares none. Among them are 8 secrets: songmaker's `SESSION_SECRET`, `POSTGRES_PASSWORD`, `ADMIN_PASSWORD`, `GRAFANA_PASSWORD`, `HF_TOKEN`, `SMTP_PASSWORD`, `SONGMAKER_INTERNAL_TOKEN`, and presentator's `PRESENTATOR_SECRET_KEY`. A name rule (`*PASSWORD`, `*SECRET`, `*TOKEN`) would have missed the last one |
| **GitHub org Actions and Dependabot secrets** | `GET /orgs/{org}/actions/secrets` and `/dependabot/secrets`: names, `visibility`, `updated_at`; `…/{name}/repositories`. Repo secrets: `GET /repos/{o}/{r}/actions/secrets`. **Never values** | fine-grained read token: org Secrets read, Dependabot secrets read, repo Secrets read, Metadata (E3's `github/fleet-read`) | extra (presentator's repo `SONAR_TOKEN` beside the org secret, ext G5); over-privileged (visibility wider than declared); stale (`updated_at` is a dated rotation with provenance); equality always `unverified` |
| **GitHub tokens themselves** | a fine-grained PAT's expiry: the response header `github-authentication-token-expiration` of any call made with it. A classic token's scopes: the `X-OAuth-Scopes` header | the token itself | stale (expiry); over-privileged against `scope`. The `gh` login keeps `admin:org` until E3's D6 (removing `admin:org`) removes it after X3c (kept on purpose for edge#29); its live scopes are read from the `X-OAuth-Scopes` header of `gh api -i user`. Listing *all* tokens with org access needs a GitHub App (`GET /orgs/{org}/personal-access-tokens`); a PAT cannot, so "extra tokens" stays a yearly glance at the settings page, unless D8 adds the App |
| **SonarCloud** | `GET api/user_tokens/search`: token names, `createdAt`, `lastConnectionDate`, `expirationDate`; `api/projects/search?organization=overnightworks` | a SonarCloud user token (read; verify which permission the project search needs when building) | extra and unused tokens; a repo with `sonar-project.properties` but no project |
| **Cloudflare API tokens** | each token verifies itself: `GET /user/tokens/verify` (or `/accounts/{id}/tokens/verify`) returns status and `expires_on`. A list of all tokens needs the permission "API Tokens Read" | the token itself; for the full list, one more read permission (D8) | stale, expiring; extra only with the list permission |
| **Cloudflare tunnels and Access service tokens** | `GET /accounts/{id}/cfd_tunnel`; `GET /accounts/{id}/access/service_tokens` (names, `expires_at`) | `cloudflare/fleet-read` plus Tunnel read and Service Tokens read | extra tunnel (a second live tunnel is an open door); expiring service token |
| **Keycloak clients** | the client list, which clients are confidential, their redirect URIs. The apply deletes no client, so a console-made client stays and appears as drift at the next read | a **read-only** service account in `hallucinai` with `view-clients` and `view-realm`. It cannot change anything and is not an admin credential (D8) | extra client; a confidential client whose secret file is missing in `~/.secrets` |

**The checks stay narrow.** Each source is one small reader returning findings in edge-check's shape. GitHub and Cloudflare already come from `edge external check` (X5); only the token self-checks, SonarCloud, the compose scan and Keycloak are new. `home check` aggregates them, with notify-on-change as today.

---

## 5. Deploy: a version tag triggers it

**Today** there is no watcher: `hal apply` takes the newest release tag on origin's default branch at the next run, after its plan and the operator's `yes` (section 3, "On"). The model below is the target.

### The model, the same for every service

| Branch or tag | Role |
|---|---|
| the default branch (`main`; hopin: `master`) | the always-green integration branch; a landing never deploys by itself |
| `v*` tag | a release. It is immutable: org ruleset 25224012 "release tags are immutable" blocks deletion, update and force-push of `refs/tags/v*` in every repo (checked 16.10.2026) |

**What each deploy watcher does** (one per service, a systemd timer every 2 minutes):
1. **Find the newest release.** It fetches tags and takes the newest `v*` tag whose commit is on `origin/<default branch>` and that is not in the skip list (see rollback). Guards: section 8 state table.
2. **Gate it.** songmaker's gate, carried over as it is and named (`auto-deploy.sh` from line 147):
   - only check runs from GitHub Actions count;
   - every Actions job must end `success`, `skipped` or `neutral`, and at least one must end `success`;
   - the job `SonarCloud scan` is advisory: its result never blocks a deploy.
   `edge release` uses the same rule, so "green" means one thing.
3. **Back up first if the schema changes.** If the tag's database schema differs from the running one (see rollback), the watcher runs the app's backup before it deploys. That backup is the way back.
4. **Deploy it.** It accepts only a tag from step 1 (see "What a deploy accepts"). `git checkout --detach <tag>` in the deploy checkout `~/srv/<name>`, then `docker compose build`, then a busy check (songmaker: no running job), then `docker compose up -d --wait`. songmaker's `migrate` service runs `alembic upgrade head` as part of this (`docker-compose.yml` line 49).
5. **Book it.** After `--wait` succeeds it reads the live database's schema version and compares it with the tag's `schema:`. Only an equal version writes the deployed tag to its state; songmaker's `deployed.sha` rule carries over as `deployed.tag`. A migration that succeeded but a health check or schema comparison that failed leaves the state `migrated, not healthy` with the live schema recorded, never the old tag; the LOOK page shows it red. The state is backed up, so a re-setup knows which version wrote the data.
6. **Alert.** After 3 failed ticks in a row, the LOOK page turns red and an alert goes out (songmaker's counters, unchanged).

**What a deploy accepts, and who may write it (D14).**
- **Only a release tag.** The deploy input is a `v*` tag fetched from the forge (step 1), protected by ruleset 25224012. Never a local working tree, branch or path.
- **Root-owned, not agent-writable:** the `edge` CLI and the `home check` executable (installed by `hal setup` under `/usr/local`), the watcher units, and the deployment checkouts (`~/srv/<name>`, songmaker's auto-deploy checkout, the live edge checkout). The agent user cannot write them. Agents work in their own worktrees and reach a deploy only through a pushed, gated tag.
- **The proof:** `home check` fails when any path on a deploy path (those executables, units, checkouts and the paths the watchers read) is writable by the agent user or by a group it is in.

**The watcher's GitHub credential** becomes a read-only token: Contents read and Checks read on the service repos (`github/deploy-read` in the manifest). Today songmaker's watcher calls `gh api`, so it runs with the operator's `gh` login (`repo`, `workflow`). A read token is needed at all because three watchers polling every 2 minutes make 90 calls an hour, above the 60 an unauthenticated client gets.

**Rollback: only when the old code fits the data.**
- **Why a rule.** songmaker migrates its database on every deploy. Code from the tag before may not understand the new schema. A blind rollback can break the app or the data.
- **The schema is declared.** `edge release` writes the schema into the annotated tag message: `schema: <alembic head>`, or `schema: none` for a service without a database, and optionally `schema-min: <head>` for the oldest schema this code still reads. The compatibility evidence for a rollback is the live database's version (read as in step 5, not taken from the state file) lying between the target tag's `schema-min` (default: its `schema`) and its `schema`.
- **`edge rollback hopin`** deploys the tag before the current one only when that tag's declared schema fits the live database. It writes the bad tag into the skip list, `skipped` in `~/.local/state/edge/<service>.toml` (the watcher's state file). The watcher never deploys a skipped tag; it follows the newest tag that is not skipped. A newer tag deploys normally, and the entry stays until the operator removes it with `edge unskip <service> <tag>`. `edge restore` writes the same entry (below). While a skip is active, the LOOK page shows hopin yellow with "rolled back to vX, vY skipped".
- **When the live schema is outside that range, it refuses.** It says: "vY changed the database. Stop the service (`edge stop hopin`) and ask the operator." The way back is then `edge restore hopin --tag vX`. It restores the backup the watcher took before vY (step 3), then deploys the chosen tag, which must be schema-compatible with that data (the same range check as above). It writes the bad tag into the same skip list; a newer tag is still picked up. The full walk is the incident table's row "A bad release" (section 8).
- This is a kill switch (ruling 12), not a change: the tags stay as they are.
- In which states rollback and restore may run, and when the service opens again: section 8 state table. Who may run them: section 8's command table.

### Who sets the tag, and when

**The release step is the head's, delegated like webauth's releases today.** webauth tags `v*` and never moves a tag: a broken release gets the next patch tag. It cut v0.12.0 and v0.13.0 on 15.10.2026. The operator never tags by hand.

**Why not tag every landing.** songmaker had 95 first-parent commits on main in the last 14 days, about 7 a day. One tag per landing would be main-deploy under another name. One release step per landing would add 95 extra agent dispatches.

**The rhythm (proposed default, D5):**
- **One batch release per service per working session,** after the last landing of a session, or when the operator asks ("release songmaker"). That is about 1–2 a day for songmaker, against 7 landings.
- **A fix for something broken in production** gets its own release right after its landing.
- **One release implementation, not one per repo: `edge release <service>`.** It reads the service's repo and default branch from the catalog. It refuses unless that branch's tip passes the gate above. It picks the next version, writes the PR titles since the last tag and the `schema:` line into an annotated tag on that tip, and pushes it. The head runs it as a mechanical step (the cheapest routing row, low effort); no review, because the tag adds no code.
- **Version form for services:** `vYYYY.MM.DD.N` (example `v2026.10.16.1`). A service has no API consumers who need SemVer, and a date says at a glance how old the running version is. Libraries such as webauth keep SemVer and their own release. Both match `v*`.
- **aco's landing brief names the freed release.** "Landed, not released" is visible on the LOOK page as "the default branch is N landings ahead of the running tag", so nothing waits silently.

### What stays applied by the operator

- **edge itself** (L2): `edge apply` after his merge. It fast-forwards edge's live checkout, shows the plan and asks `yes`. Changing the gatekeeper, Keycloak or the backup is never automatic.
- **External** (L3): the publish phase of `edge apply`. Only the operator holds the write tokens (E3 rule 3).
- **Why not automatic.** Agents run as his user today, so any token on a timer would be an admin credential in an agent's reach. Automatic apply becomes possible only after D14.

### How each service joins

| Service | Today | Joins by |
|---|---|---|
| **songmaker** | system timer `songmaker-autodeploy.timer` (every 2 min) runs the 1,253-line `scripts/auto-deploy.sh` on `~/git/songmaker`, following `origin/main` once CI is green. The checkout is also the place `aco land` must not run (memory: land from `~/git/songmaker-land`) | the migration below |
| **presentator** | manual. `~/presentator-live` is a separate checkout at `1f0422c` and runs a locally built image (`presentator-adoption-instance:ap112-e04e837a`) on 127.0.0.1:8100. Two host user units run from that checkout: `presentator-speech` and `presentator-copresenter`; speech is crash-looping (stale socket file, seen 16.10.2026). Offline publicly since 14.10.2026 | its go-public item agent-presentator#197 adds a catalog entry with `deploy: tags` and its first `edge release`. It joins under §10 rule 1: the two host units and their unix sockets (its `compose.yaml:52-53`) and the `docker.sock` mount (`:51`) leave its release, so the watcher deploys the compose release and nothing on the host |
| **hopin** | not deployed; no container | section 3's example: a compose file, then its first tag |

Every service that holds people's data also offers the export and erase routes of `docs/people.md`.

### Migrating songmaker off main-deploy

1. **The first tag.** The head tags the commit in songmaker's `deployed.sha` as `v2026.MM.DD.1`, with its `schema:` line, so the switch deploys nothing new.
2. **Its watcher follows tags.** In `scripts/auto-deploy.sh`, the deploy target changes from "newest green commit on `origin/main`" to "newest green `v*` tag on `main`". Everything else stays: the gate, busy check, base-image rebuild, Prometheus reload, the counters, the alert. It gains the backup before a schema change. This is a songmaker item; its tests already pin the script.
3. **A deploy checkout of its own.** `~/srv/songmaker`, cloned fresh. The `.env` moves with it (later: its secrets move to `~/.secrets/songmaker/`, section 7). `install-autodeploy.sh` points the unit there.
4. **`~/git/songmaker` becomes a plain work clone again.** `aco land` there is harmless, `~/git/songmaker-land` can go, and the memory note about it is retired.
5. **Later, once presentator needs a watcher.** The tag watcher moves from songmaker into edge as the one generic `edge-deploy@<service>` unit. songmaker keeps its busy check as its own health URL (`/deploy/ready` answers whether a job runs). Building the generic watcher before a second service needs it would be building for an imagined caller.

### On a re-setup

Section 6 step 8 starts every enabled service at the tag its backup recorded; from there the watchers above take over (when they run: section 8 state table). There is no separate "install-autodeploy" per repo.

---

## 6. The re-setup runbook: from a bare PC to every service live

Every step is either **one command** or a **guided step**. A guided step means the tool:
1. says in one sentence what happens and why;
2. opens the page;
3. says exactly what to click;
4. waits for Enter;
5. verifies by itself.

That is the operator's „zumindest durchleiten zu den Seiten“, and edge#19 rule 7.

**Nothing opens before the data is back and checked.** The runbook stays in column C of the section 8 state table from the first `edge apply` (step 6; on a first-ever setup, step 8) until `edge start --all` ends step 11 (C → A). Otherwise a user could write into an empty instance, and the restore would then wipe that write.

**Two paths:**
- **First-ever:** nothing exists. New secrets, Keycloak go-live once, people created by hand.
- **Re-setup:** the PC died or is reinstalled. The secrets and the Keycloak DB come back from the backup: no new passwords, no new passkeys for anyone. This needs D3, a backup copy off this disk that includes `~/.secrets`, and its restore path below. **Without D3, a new disk means the first-ever path:** every person enrols again, and songmaker's data is gone.

**What the operator needs at hand:**
- the password manager: the restic password, the OpenTofu state passphrase, the disk's recovery key (D10), his Google login;
- his passkey device;
- the off-disk backup disk (D3).

| # | Step | Kind | First-ever | Re-setup | Done when (the tool verifies) |
|---|---|---|---|---|---|
| 1 | Install Ubuntu 24.04, disk encryption per D10, user `felix-hummert` | manual (the installer) | same | same | the desktop runs |
| 2 | `sudo apt install -y gh && gh auth login --web && gh repo clone overnightworks/home-tools ~/git/home-tools && ~/git/home-tools/bootstrap` | one line, printed on paper and at the top of `docs/architecture.md`; the login is guided by `gh`. **`bootstrap` does not exist today**; home-tools#9 builds it | same | same | `home --help` answers |
| 3 | `hal setup`, then `hal setup --apply` | one command, asks for `sudo` once | same | same | the dry run shows nothing left to change; `home check` reports L0 clean (Docker loopback default, cloudflared installed and **disabled**, restic, `tofu` pinned, Tailscale, auto-login off; the agent user once D14 is done) |
| 4 | **Secrets** | guided | `hal setup --apply`: per secret, open the provider page, paste hidden, verify, store. Generated service passwords need no typing | `hal restore secrets --repo <off-disk repository> --target ~/.secrets`: the recovery form below | `home secrets` without findings |
| 5 | Tool logins: Tailscale, Claude, Codex (for the agent user, once D14 is done) | guided (part of step 4: the wizard runs each official login command) | same | the same; tool logins are not restored, because they expire and rebind | each `verify` passes |
| 6 | `edge apply --closed --platform` | one command | **skipped**: step 7 creates the Keycloak secrets first and starts the platform itself | first restores the Keycloak dump into the empty DB (same backup run as step 4), then starts Keycloak, so the realm, the people and the passkeys are back | 8081 and 8090 listen on loopback, Keycloak answers; the PC is in column C |
| 7 | `keycloak/go-live` (edge#19) | guided; runs **once ever**; never publishes (section 3, X8) | creates the Keycloak secrets, bootstrap admin, master on 8090, applies the realms; asks for the permanent admin's name and first password (hidden) and creates it; pauses for the browser step (TOTP and passkey); then deletes the bootstrap admin | **skipped**: go-live sees its done-states and changes nothing | another master admin with TOTP and a passkey exists, `temp-admin` is gone |
| 8 | `edge apply --closed` | one command; shows the plan, asks `yes` | starts every enabled service at its newest tag | starts every enabled service at the tag its backup recorded as running (`deployed.tag`), so code and data fit | each health URL answers on loopback; every enabled service is in column C |
| 9 | Restore app data | one command per app: `hal restore <app> --live --repo <off-disk repository>` (new, section 7) | — | loads each app's dump and files from the same backup run as step 4 | the restore test's checks pass on the live data; the data's schema equals the running tag's `schema:` |
| 10 | Check it closed | `edge check --closed`, then the operator opens each app on loopback | the apps answer, empty | he sees his songs and his people on loopback | every light green except "not published" |
| 11 | `edge apply` (publish), then `edge start --all` (open) | two commands; `edge apply` shows the plan and asks `yes` | `edge apply` publishes the DNS, Access and tunnel lines (C stays C); `edge start --all` then moves every enabled service C → A | the same; the restored OpenTofu state plans "No changes" for what still exists. A lost state re-adopts by name (E3 rule 6). From A on, section 5's watchers take each service from its recorded tag to its newest one | `edge external check` exit 0, then `edge check` exit 0 with every enabled service in A |
| 12 | **PEOPLE** | guided: `edge people` opens `http://localhost:8090/admin/` on the users page | create each person and put them into `/apps/<name>` | nothing: the DB came back | the person signs in to their app |
| 13 | **LOOK** | open the traffic-light page | every light green | every light green | — |

### The off-disk restore path (D3)

Today `hal restore` cannot restore on a clean machine: its settings fix the repository to `~/backups/restic` and the password to `~/.secrets/restic/password` (`src/edge_backup/settings.py`, `Settings.for_home`). On a new disk, neither exists. The recovery form changes four things:

- **Which repository.** `--repo` names it on the command line: the USB disk's path (later also the rclone `gdrive` remote). Nothing is looked up locally.
- **The password.** restic asks for it at a hidden prompt; the operator types it from the password manager. In recovery form `~/.secrets` is never read; it is the target.
- **Which snapshot.** The list comes from the repository itself (`restic snapshots`), never from the local `backup-status.json`, which is gone. Each backup run tags its snapshots with one run ID. The tool shows the runs newest first, with date and host, and takes the newest complete run unless he picks another. Steps 4, 6 and 9 all use that one run, so Keycloak's client secrets fit the secret files.
- **Where the disk lives.** A USB disk on the desk dies with the PC in a fire or a theft. The `gdrive` copy is the later answer to that. A disk an agent can write to can also be wiped by an agent (D14).

**The proof: a clean-machine drill.**
- A throwaway machine (a VM or a spare disk) with no `~/.secrets` and no `~/backups`.
- Only the USB disk and the typed password.
- It runs steps 4, 6, 8, 9 and 10, and **never** step 11.
- It is done when the restore test passes there and an admin login works on its loopback. It checks data, not GPU jobs.
- D3 counts as done only after the first drill passes. The drill repeats after every change to the backup layout, and at least every 6 months. The LOOK page shows the last drill date.

**One-time pieces that never repeat on a re-setup:**
- the Cloudflare account and zone;
- planned (X8b; there is no Zero Trust today): the Access identity provider (Google);
- planned (X8b): the Access group's one member (the operator);
- the tunnel object (D6);
- the GitHub org, its App installs and the SonarCloud org.

They survive a dead PC because they live at the provider.

---

## 7. Gaps and duplicates

**Exists today:**
- `home check`, `home tidy`, `home secrets` (view only);
- edge: Traefik, socket proxy, Keycloak as code with its probe, edge-check, hal backup with weekly and monthly timers. The edge stack is **not running** on this PC (no `edge/.env`, no edge containers);
- songmaker's auto-deploy, alerts and monitoring stack;
- the tunnel with songmaker's host and auth, then the terminal 404 rule;
- org rulesets 25224011 and 25224012.

### Duplicates: the same fact in two places

| # | What is double | Where | Owner item |
|---|---|---|---|
| 1 | The secrets list | `~/README.md` table (stale: names atelier2, tidy-home, `cloudflared/config`) and `secrets_manifest.toml` | home-tools#10, slice S2 |
| 2 | Tunnel credentials | `~/.secrets/cloudflared/<id>.json` and `/etc/cloudflared/<id>.json` | home-tools#21, but D6 keeps the JSON credential; which copy is the one stays with home-tools#21. #21 plans to delete the `~/.secrets` copy, which a re-setup from backup needs |
| 3 | Tunnel ingress | the catalog (its lines written by `edge external apply`), the `ingress:` of `/etc/cloudflared/config.yml` (serving until the switch) | E3 slices X6 and X8, in the mode D6 ruled. The catalog is the owner; the `config.yml` cleanup is #144 (B7) |
| 4 | App groups | `keycloak/realm.yaml` (`/apps/songmaker`, `presentator`, `hopin`) and the catalog's `login_group` | **new slice in E3**: the catalog's CI checks against `realm.yaml` and `apps.toml` (section 3) |
| 5 | Org secret reach | home-tools#10 line 19 (repo-list drift) and E3 X5 (`fleet.yaml` org secrets) | home-tools#10 (drop its drift view) and E3 X5 (keep it) |
| 6 | Alert channels: three today | songmaker's e-mail (`songmaker-alert@`), Alertmanager inside songmaker, `home check`'s desktop notification | edge#4: **one push channel to the phone** (ntfy planned). Gatus ("is it OK now?") and Alertmanager ("why, since when?") both send into it; `home check` findings reach it through Gatus; songmaker's own e-mail alert goes |
| 7 | Deploy | songmaker's watcher only; presentator by hand, with locally tagged images; hopin none | **new item in edge**: "one deploy model: tag watcher and `edge release`". Its first slice is in songmaker (section 5) |
| 8 | Deploy checkout = work clone | `~/git/songmaker` is both; the workaround is the extra `~/git/songmaker-land` | the songmaker migration item (section 5, step 3) |
| 9 | Monitoring stack | Prometheus, Grafana and Alertmanager live in songmaker's compose, though they serve the platform | edge#4: **ruled 16.10.2026:** they move into edge and serve every service. songmaker keeps only its metrics endpoint and its dashboards as files edge loads |
| 10 | Two publishers | E3 X8 lets `keycloak/go-live` call `edge external apply`; section 6 publishes in `edge apply` | E3 X8, changed once (section 3): go-live never publishes |
| 11 | The zone name | `fleet.yaml` `cloudflare.zone` only: `edge apply --closed` writes it to `.env`, and CI derives `EDGE_DOMAIN` from the same key | landed: edge#136 |

### Gaps: something missing

| # | Gap | Evidence | Owner item |
|---|---|---|---|
| 12 | Docker loopback default not set | `/etc/docker/daemon.json` has only the nvidia runtime | home-tools#9, slice 1 (step A7) |
| 13 | `tofu` and Tailscale not installed | `which` finds neither | home-tools#9 |
| 14 | No catalog, no `edge apply`, no `edge check` | — | **new item in edge**: "catalog and `edge apply`" (with `--closed` and the publish phase). Its caller is this concept; it depends on E3 X1; E3's X1a creates `fleet.yaml` at the root, and this item owns the `run` blocks |
| 15 | E3 not opened | the plan is a draft in `ext-as-code.md`; edge#1 row E3 is its placeholder | open E3 from that draft, with the four changes in section 3 |
| 16 | Keycloak go-live not run | edge#11 go-live row; script in edge#19 | edge#19, then edge#11; D14 does not gate it (ruled 16.10.) |
| 17 | Keycloak DB in the backup (closed with the go-live, #118) | hal backup backs it up and restore-tests it as a DB-only app (K11b, edge#107); its record in `backup/apps.toml` is live with the go-live (#118, after #178 took it out while Keycloak was off); go-live step 1 asks only while hal backup is not set up (K11b.2, edge#127) | K11b.2 in edge#11 |
| 18 | Live restore not scripted | songmaker's and Keycloak's are a manual procedure in [backup.md](backup.md), run only by the operator; re-setup step 9 needs `restore <app> --live` | **a new edge item** for the scripted `restore <app> --live` of both |
| 19 | Backup on the same disk, and no restore without the local disk | [backup.md](backup.md) says it: no protection against a dead disk. `Settings.for_home` fixes repository and password to local paths | **new item in edge**: "a second backup target off this disk, and the recovery form" (section 6); done when the clean-machine drill passes; its caller is D3 |
| 20 | `~/.secrets` not backed up | ruled 14.10.2026 (home-tools#8 closed: a wizard instead) | D3 decides; if yes, hal backup declares `~/.secrets` as an app in the same new item as 19 |
| 21 | App secrets in checkout `.env` | 7 secret keys in songmaker's `.env`, 1 in presentator's; neither is in the manifest | **new item per app** (songmaker, presentator): compose `secrets:` from `~/.secrets/<app>/`, like edge does; plus manifest entries with `bound_as` (home-tools#21 can take the entries) |
| 22 | No LOOK page | Gatus proposed 15.10.2026; no item | edge#4: pull it as "LOOK: the traffic-light page, the platform's monitoring stack and one alert channel"; agent-coordination#24 (PC-Zentrale) folds into it or names it as its neighbour |
| 23 | The remote secret check | section 4: token self-checks, SonarCloud, the compose scan with `bound_as`, Keycloak | **new home-tools item** after E3 X5 (the GitHub and Cloudflare parts come from X5) |
| 24 | Agents held org admin | The `gh` login keeps `admin:org` until E3's D6 (removing `admin:org`) removes it after X3c (kept on purpose for edge#29); its live scopes are read from the `X-OAuth-Scopes` header of `gh api -i user`. `delete_repo` is removed (16.10.2026). songmaker's deploy watcher still uses that login. The bigger reach remains: row 31 | the `github/deploy-read` token in the songmaker migration item |
| 25 | Port 8090 claimed twice | edge's admin entrypoint is `127.0.0.1:8090`; `presentator-speech.service` sets `SPEECH_PORT=8090` and the copresenter `COPRESENTER_SPEECH_URL=http://127.0.0.1:8090`. Whichever starts first wins | agent-presentator#108 ("the voice is a setting, not a unit file"), or a new presentator item if #108 does not cover the port |
| 26 | hopin cannot run beside songmaker | default port 8080, no container | hopin#25 or a new hopin item "container with `edge.name`" |
| 27 | No release step | no service repo has tags; only webauth releases | `edge release` in the new deploy item (row 7) |
| 28 | Dead unit link | `agent-telegram.service` points to a moved file | claudebot#19 (the bot stays off) |
| 29 | No emergency commands | `edge status/stop/start/lock`, the markers, their boot guard and its drift finding do not exist | **new item in edge**: "edge as one operator CLI with emergency switches" (section 8). It also folds `edge-check` into a subcommand; backup and restore are the ruled `hal backup` and `hal restore`. The section 8 state table becomes this item's test table: one parametrised test per cell |
| 30 | No polkit rule for cloudflared | stopping the tunnel needs `sudo` today | home-tools#9 (`hal setup` installs the stop-only rule `tunnel/polkit/10-edge-cloudflared-stop.rules` from edge, section 8) |
| 31 | **Agents run as the operator's user** | he is in the `docker` group (root on the PC) and `sudo`; `~/.secrets` is his; after go-live, `~/.secrets/keycloak/config-cli-secret` signs in as Keycloak master `admin` (`master.yaml`: `service-account-config-cli`, realm role `admin`), without TOTP | **new home-tools item** "secure the agents" (D14), aco as neighbour for starting sessions as the agent user |
| 32 | songmaker has no single-login cutover | its own password login; the app clients are in `realm.yaml` (#108, #161), only the songmaker cutover is still open | **new songmaker item** (section 3), after edge#19 |
| 33 | No `home-tools/bootstrap` | the file does not exist | home-tools#9 |
| 34 | No way to stop every agent at once | aco runs sessions on its own tmux socket but has no "stop all" command | **new item in agent-coordination** (aco owns agent sessions) |
| 35 | Songmaker's host (`songmaker`) and presentator bypass Traefik | tunnel ingress sends them to `:8080` and `:8100`; no ingress barrier can hold them (section 8) | the catalog and `edge apply` item (gap 14) routes every hostname through Traefik on 8081 (until then the host's `target` in `fleet.yaml` carries it, `http://localhost:8080` for `songmaker.hallucinai.de`); for presentator it lands with presentator#197. The apex and `www` are no tunnel hosts any more: they redirect at the edge (below) |

---

## 8. Betrieb und Notfall (operations and incident)

The operator asked: „wer verwaltet das Home / die Dienste? ggf eine UI-Seite, die Dienste an/abschalten kann? genauso wie gesamt Cloudflare stoppen? … dass ich über ein Tool bei Problemen schnell handeln kann?“

### Two kinds of action, never mixed

| Kind | When | How | Effect |
|---|---|---|---|
| **Planned change** | anything meant to last: a service on or off, a new name, a new group | a PR to the catalog or the code, merge, then `edge apply` (section 3) | the code is the truth; `home check` is clean afterwards |
| **Emergency switch** | something is wrong *now* | one `edge` command in the operator's terminal; no code change | acts within seconds; what it does is the state table below. **`home check` reports every active switch as drift** until reality and code agree again |

### The state table: the one owner of every guard

Every other section points here ("guards: section 8 state table") and restates none of it. The table owns *when* a command may act and which state it leaves behind; *what* a deploy, a rollback or a restore does to code and data is section 5's. Each state describes one service S; the global stop applies to the whole PC.

| State | Recorded | cloudflared | S's ingress barrier | S's watcher | S's containers |
|---|---|---|---|---|---|
| **A · running** | nothing | running | absent | runs | up |
| **B · switched off** | S's switch | running | present: Traefik answers 404 for S's host | paused | stopped, or up behind the barrier after a restore |
| **C · global stop** | the global stop | stopped | absent | paused | up, reachable on loopback only |
| **D · global stop + switched off** | both | stopped | present | paused | as in B |
| **E · disabled in the catalog** | the applied catalog says `enabled: false`; a marker may remain from B or D | as the PC: running, or stopped under the global stop | absent, or S's barrier while its marker remains: either way S's host answers 404 | none: not installed | removed; volumes kept (R1, section 3 "Off") |

**E takes precedence over A–D.** "Applied" means the live edge checkout, which only `edge apply` fast-forwards: a merged `enabled: false` changes nothing until `edge apply` runs, and from then on S is in E whatever its marker says. A marker that remains keeps a re-enabled S closed; see "How the states are held". The global stop still governs cloudflared for the PC; E does not touch it.

**A command on the whole PC** (`edge stop --all`, `edge start --all`, boot) applies to each service the cell of that service's own column. On a PC with X in C and Y in D, one `edge start --all` moves X to A and Y to B.

On a new PC nothing is applied yet: `hal setup` installed cloudflared disabled, and the first `edge apply` records the global stop, so every enabled service comes up in C and every disabled one is in E. A host that bypasses Traefik (gap 35) has no B or D: its only barrier is the tunnel.

A cell names what changes; everything it does not name stays as it is. "→ B" means S is in state B afterwards.

| Command | A · running | B · switched off | C · global stop | D · global stop + switched off | E · disabled in the catalog |
|---|---|---|---|---|---|
| `edge apply`, local part (alone: `--closed`) | allowed: applies S's changed entry; when the catalog now says `enabled: false`, → E (steps: section 3, "Off") | allowed: never starts S; the plan says "S stays stopped (switch since …)". When the catalog now says `enabled: false`, → E: S goes down for good; its marker and barrier stay, so a re-enabled S stays closed until `edge start S` | allowed: as A; cloudflared stays off | allowed: as B; cloudflared stays off | allowed: while the catalog says `enabled: false`, no change. When it says `enabled: true`: → B (→ D under the global stop) while S's marker remains; without a marker → A (steps: section 3, "On"), or → C under the global stop, where the new watcher stays paused |
| `edge external apply`, alone or as the publish phase of `edge apply` | allowed: writes DNS, Access, tunnel lines and GitHub settings; refuses a plan with changes, before `yes`, while an enabled host's `ready` path does not answer on its target; then checks each host against its declaration (protected by Access with the declared policy, public, or bypass) and fails on any mismatch or undeclared host ("fails" = `edge external apply` exits 1, also when the check cannot run; after an interrupt past `tofu apply` the check is skipped and the message names `edge external check --hosts`) | refused with changes while S declares `ready`: its unit is stopped, so its path does not answer (`edge start S` or `enabled: false` ends that); otherwise as A; S's names stay published and the barrier keeps answering 404 | allowed: as A; only the provider side changes, cloudflared stays off | as B: refused with changes while S declares `ready`; otherwise as C | allowed: as A; it removes S's names (section 3, "Off"), which answer 404 until then |
| deploy watcher (timer) | runs: deploys the newest green tag that is not skipped (section 5) | paused: does nothing | paused: does nothing | paused: does nothing | none: S has no watcher |
| `edge rollback S` | allowed: the rollback of section 5, which refuses itself when the live schema does not fit; S stays in A | refused | refused | refused | refused: "S is disabled in the catalog" |
| `edge restore S --tag T` (operator) | refused: "stop it first: `edge stop S`; for a host that bypasses Traefik, `edge stop --all`" | allowed: the restore of section 5. Containers run behind the barrier; barrier stays, watcher stays paused. S opens at `edge start S` | allowed (the only way for a host that bypasses Traefik): the same; containers run on loopback, cloudflared stays off, watchers stay paused. Opens at `edge start --all` | allowed: the same; barrier stays, cloudflared stays off, watchers stay paused. Opens only after both `edge start S` and `edge start --all` | refused, as rollback |
| `hal restore S --live` (operator) | refused, as `edge restore` | allowed: stops S, loads its dump and files, starts its containers behind the barrier; barrier stays, watcher stays paused | allowed: the same on loopback; cloudflared stays off, watchers stay paused (section 6 step 9) | allowed: as B; cloudflared stays off | refused, as rollback |
| `edge stop S` | → B: stops S's containers (`docker compose -p S stop`; volumes stay), renders the barrier, pauses S's watcher, records the switch. Refused for a host that bypasses Traefik: "`<host>` bypasses Traefik; migrate it first (gap 35). Until then use `edge stop --all`." | no change | → D: as A → B; cloudflared stays off | no change | no change: nothing runs |
| `edge stop --all` | → C: stops cloudflared, pauses S's watcher, records the global stop; containers keep running | → D: the same; S's switch, barrier and containers stay | no change | no change | S stays in E; the global stop is recorded for the PC |
| `edge start S` (operator) | no change | → A: clears the switch, removes the barrier, starts the containers when stopped, resumes S's watcher | no change | → C: clears the switch, removes the barrier, starts the containers on loopback. The watcher stays paused and cloudflared stays off until `edge start --all` | refused: "S is disabled in the catalog; enable it by PR and `edge apply`" |
| `edge start --all` (operator) | no change | no change: S's switch stays | → A: repeats the host check of `edge external apply` and refuses on any failure; then starts cloudflared (asks the operator's password); then resumes S's watcher | → B: the same check and the same cloudflared start; S's watcher stays paused, so S stays closed: barrier, paused watcher, containers as they are | S stays in E; the global stop ends for the PC |
| `edge unskip S T` (operator) | allowed: removes T from S's skip list; the watcher may pick T at its next tick | allowed: the same; the watcher stays paused | allowed: the same; the watcher stays paused | allowed: the same; the watcher stays paused | allowed: the same; it matters once S is enabled again |
| `home secrets --rotate <name>` (operator) | allowed: stores the new value, restarts S if S reads it | allowed: stores the value; restarts S only if its containers run after a restore, else they read it at their next start | allowed: as A; never starts cloudflared, even when it reads the secret | allowed: as B; cloudflared stays off | allowed: stores the value; nothing restarts |
| `edge check`, `edge status` | allowed, read-only | allowed, read-only | allowed, read-only | allowed, read-only | allowed, read-only |
| boot | Docker restarts S (`restart: unless-stopped`), cloudflared starts, the watcher runs | `edge-switches.service` stops S again before cloudflared starts; barrier and paused watcher stay | cloudflared's drop-in keeps it stopped; S restarts on loopback; its watcher stays paused | both: S stopped, cloudflared off | nothing of S starts |

**How `edge check` proves the table.** It places every service in its column (E when the applied catalog says `enabled: false`, otherwise A–D from the markers) and checks that column's facts against reality:
- S's barrier file present, and a request to Traefik on 8081 with S's `Host` answers 404 from the deny service (B, D); no barrier file (A, C; E without a marker);
- S's watcher timer active (A), paused (B, C, D) or not installed (E);
- cloudflared active when no global stop is recorded (A, B, E), inactive when it is (C, D, E);
- S's containers up (A, C), stopped unless a restore started them (B, D), or absent (E).

Each mismatch is a red finding, for example a recorded switch without its barrier file. The `edge` CLI item (section 7, gap 29) turns the table into its test table: one parametrised test per cell sets up the column, runs the row's command and asserts the cell with `edge check`.

**How the states are held.**
- **The markers (P7, [edge#50](https://github.com/overnightworks/edge/issues/50)).** One persistent marker per service and one global marker under `~/.local/state/edge/` record every switch and the global stop. They are written **before** anything stops, so the intent exists first. They replace the single state file for switches.
- **The ingress barrier.** Stopping containers is not enough, because cloudflared keeps running and a redeploy would bring the route back. So `edge stop S` renders a barrier for S directly under `<edge checkout>/dynamic/`, derived from S's marker. It is a flat file, never in a subdirectory, because the pinned Traefik does not watch subdirectories. It is generated and git-ignored, written atomically, and the deny route is verified before success is reported. The `./dynamic` directory is the one `compose.yaml` mounts as `/dynamic` and `traefik.yml` watches. The file holds a top-priority router for S's host that answers 404 from a deny service and beats the container's label router. It covers only hostnames routed through Traefik; `edge check` reports each hostname that bypasses it (gap 35).
- **The applied record ([edge#85](https://github.com/overnightworks/edge/issues/85), [edge#87](https://github.com/overnightworks/edge/issues/87)).** `~/.local/state/edge/applied.json`, beside the markers, holds for each service edge follows through a file it binds from the checkout the sha256 of each such file, as last fully applied: today `traefik.yml` (traefik), `traefik-mcp.yml` (traefik-mcp) and `agent-service/egress/traefik.yml` (agent-egress). A core service's compose definition (what `compose.yaml` runs without a profile) needs no record: it changed when `docker compose config --hash` differs from its container's `com.docker.compose.config-hash` label, which is Compose's own fact. `edge apply --closed` recreates each running core service whose definition or file differs, and the core services that depend on it, socket-proxy before traefik; it empties the recreated services' entries first, observes each running with what it depends on, proves every active barrier after each recreate, and only then writes the entries, with the digests read at plan time, so a recreate that did not finish is planned again even after `traefik.yml` is reverted; a missing, unreadable or malformed record, a missing entry, or an entry naming a file the service does not mount counts as changed, and an entry for a service that binds no file from the checkout is dropped at the next write. A core container found created and never started is edge's unfinished recreate, recreated again, only while the entry of a recreate that took it in is empty: its own, or for socket-proxy, which keeps none, that of traefik, which every live socket-proxy recreate pulls in; one created by hand is a stopped service and is not started. A core service a recreate pulls in that has no live container is named and never started, and "the core is current" is withheld (scenario R3e). A follower ([edge#89](https://github.com/overnightworks/edge/issues/89)), a profile service binding a file at Traefik's static-config path (today `traefik-mcp` with `traefik-mcp.yml` and `agent-egress` with `agent-service/egress/traefik.yml`, started by the unit `agent-service`), follows its file and definition the same way after the core: while its unit is enabled and not switched off it alone is recreated, under the switch lock, and recorded once observed running, or recorded at the switch-on that starts it; while its unit is off or switched off it is named and nothing starts; a live service of a unit not planned off whose container runs another image than its resolved `image` names is named after the unit lines and never swapped, so its upgrade stays the operator's ([edge#91](https://github.com/overnightworks/edge/issues/91)).
- **Every start path honours the markers:** `edge apply`, `edge start`, the setup commands (`keycloak/go-live`'s plain `up -d` included) and later watchers. `edge apply` never clears a marker; only the operator's `edge start S` clears S's marker.
- **The global stop** writes its marker first, then stops cloudflared. The cloudflared drop-in (see Reboots) keeps the tunnel down at boot; after a reboot the tunnel reopens only once the barriers are verified.
- **Honest results (P15, [edge#50](https://github.com/overnightworks/edge/issues/50)).** A partial failure is reported as partial, never as "applied"; a closed state is reported only after it was observed; a second `edge apply` with nothing changed changes nothing.
- **Reboots.** The boot row rests on three pieces: `restart: unless-stopped` on every long-running service (section 3's catalog check flags any other policy); `edge-switches.service`, ordered before cloudflared; and the cloudflared drop-in from `hal setup`, which tests the global marker (`ConditionPathExists=!<marker>`).

Example: `edge stop hopin` at 22:10 gives the finding `emergency-switch hopin: stopped 16.10. 22:10, catalog says enabled` on every `home check` and a yellow light on the LOOK page, for as long as the switch is recorded.

**Who owns what.**
- **edge** owns everything that serves or guards a service: the tunnel, Traefik, Keycloak, the services, the deploy watchers, backup, monitoring.
- **home-tools** owns the PC, the agent user and the secrets.
- **aco** owns agent sessions.

Each command exists in exactly one tool. What it does in each state is the state table above; this table names its owner, its undo and who may run it. The last column is the rule; today it is policy only (see "Honest limit" below).

| Command | Owner | Does | Undo | May an agent run it? |
|---|---|---|---|---|
| `edge status` | edge | one table: every service (running, stopped, switched off, its tag), reachability through the tunnel, the last backup, restore test and drill, active switches, external drift | — | yes (read-only) |
| `edge stop <service>` | edge | switches one service off | `edge start <service>` | **yes**, pre-approved and reversible (ruling 11: "take an app offline") |
| `edge stop --all` | edge | the global stop: takes the whole PC off the internet; Tailscale stays up for diagnosis | `edge start --all` | **yes**, pre-approved and reversible. `hal setup` installs one polkit rule: it allows the verb **stop** on `cloudflared.service`, and nothing else, without a password; `start` is not covered. edge's `tunnel/` owns its content: `cloudflared.service`, its drop-ins `20-nonroot.conf` and `30-edge-global-stop.conf` (the boot skip while the global marker exists) and `polkit/10-edge-cloudflared-stop.rules`, with `@HOME@`, `@OPERATOR_USER@` and `@TUNNEL_ID@` for the installer (home-tools#9) to fill |
| `edge apply` | edge | brings the catalog live: the local part, then the publish phase (section 3) | — (set the entry back and apply again) | **no**: it brings things live, and only the operator does (D12) |
| `edge start <service>` / `--all` | edge | ends a switch or the global stop | — | **no**: opening a door after an incident is the operator's call (D12) |
| `edge restore <service> --tag <tag>` | edge | **operator-only recovery:** the restore of section 5 | — | **no**: a data restore (D12) |
| `edge rollback <service>` | edge | the rollback of section 5: one tag back, when the schema allows | the next tag | yes, within its state-table cell |
| `edge unskip <service> <tag>` | edge | removes one tag from the service's skip list (section 5) | `edge rollback` / `edge restore` | **no**: it re-admits a release known to be bad (D12) |
| `edge lock <user>` | edge | in Keycloak: sets the user `enabled=false`, then ends all their sessions (`POST /admin/realms/hallucinai/users/{id}/logout`). Then it asks each app in the user's groups for evidence and prints per app: `ended`, `still active` (red) or `unverified` (the app cannot answer). It never says "locked everywhere" unless every app says `ended`. It asks for the operator's own admin login **with TOTP** on every call | `edge unlock <user>` | **no** (rights). **Not enforced today:** the stored `config-cli` secret is master admin without TOTP, readable by every agent (D14) |
| `edge lock --everyone` | edge | ends every session in the `hallucinai` realm (`logout-all`); accounts stay enabled; the same per-app evidence | — (people sign in again) | **no** (rights) |
| `home secrets --emergency` | home-tools (ruled: home-tools#10 line 16) | the revoke guide: per entry it opens the revoke or sign-out page (GitHub tokens and sessions, SonarCloud, Cloudflare tokens and Access sessions, Telegram BotFather, Google), says what to click, and is checked off with Enter. It never claims a revocation happened | — | **no** (key rotation and revocation are the operator's own) |
| `home secrets --rotate <name>` | home-tools (#10) | replaces one secret: page, hidden input, verify, store, then restart its readers as the state table allows | — | **no** |

**No `edge revoke`:** revoking is about secrets, so it lives once, in `home secrets --emergency`. `edge status` prints "secrets: run `home secrets --emergency`" as a hint, nothing more.

**One tool name per owner.** `edge` gets subcommands, the way `home` did: `edge check` (today `edge-check`), `edge external` (E3's separate external script, not built yet), `edge apply`, `edge release`, `edge status`, `edge stop/start`, `edge restore`, `edge rollback`, `edge unskip`, `edge lock`, `edge people`. One command to remember (D11).

### LOOK and Grafana: two pages, two questions (ruled 16.10.2026)

- **The traffic-light page (Gatus) answers "is it OK now?"** It is tiny and independent: it must still work when Grafana, Prometheus or Keycloak are down. It pushes alerts.
- **Grafana answers "why, and since when?"** It serves every service, not only songmaker. Prometheus scrapes every enabled service from the catalog. Alertmanager sends its alerts into the same push channel. One channel, not three (section 7 row 6).
- **Both are operator-only and will sit behind Access (X8b, D4)**; no login-less tool is published before it.
- **The LOOK page has no buttons.** A page that can stop services or lock people is an attack surface on the network.
- **If buttons come later** (agent-coordination#24 "PC-Zentrale"), they are local-only: on the admin entrypoint `127.0.0.1:8090`, behind the Keycloak admin login with TOTP, never through the tunnel. Each button calls the same `edge` command, so there is no second implementation.
- Until then, each light carries its runbook link (ruling 11). The runbook names the one command to run.

### Remote kill switch while travelling (D13)

- **First: Tailscale only.** From the phone or laptop: Tailscale to the PC, then `edge stop --all` or `edge lock` in a terminal. No new control surface.
- **The Telegram bot (claudebot) stays a later option.** If it ever comes: exactly one action, `stop --all`, only for the operator's Telegram user ID, confirmed by a second message, and only after claudebot#19's 3 critical findings are fixed and D14 is done.
- **`lock` is never offered through the bot.** It needs the operator's admin login with TOTP; a bot holding a stored Keycloak credential would be an admin credential on the PC.

### Security actions in an incident, each mapped

Each recovery below is a walk through the state table (its states in brackets). Opening again follows the state table; `edge rollback` is the one step that never closes anything.

| Situation | Action | Command or guided page | Who |
|---|---|---|---|
| One app misbehaves or leaks | take it offline | `edge stop <app>` (for a host that bypasses Traefik: `edge stop --all`) | operator or agent |
| Unknown traffic, attack, or doubt | take the whole PC off the internet, keep it running | `edge stop --all` | operator or agent |
| A bad release | go back one tag, if the schema allows | `edge rollback <app>` (stays in A). If it refuses: `edge stop <app>` (A → B); the operator's `edge restore <app> --tag <tag>` (B); a check on the container itself (`docker exec` against its health URL); `edge start <app>` (B → A). For a host that bypasses Traefik: `edge stop --all` (A → C), `edge restore <app> --tag <tag>` (C), a check on loopback, `edge start --all` (C → A) | operator or agent; the restore is the operator's |
| A user account is abused | lock it and end its sessions | `edge lock <user>`; done only when every app says `ended`. An app that says `still active` or `unverified` is stopped until it can (`edge stop <app>`; for a host that bypasses Traefik `edge stop --all`, which stays until the session is proven ended) | operator |
| The login itself is in doubt | end every session | `edge lock --everyone`, with the same per-app evidence | operator |
| Keycloak's signing key may be known | rotate the realm keys **and** end every session, then prove it per app | guided page: `edge people --keys` opens Realm settings → Keys; add a new key provider, disable the old one; then `edge lock --everyone`. Rotation alone is not enough: an app that cached the old public key, and an app session already issued, keep working until the app itself checks again (Keycloak documents that revocation reaches a client only when it checks). The step is done only when each app reports no session older than the rotation; an app that cannot report is `unverified` and is stopped as in the account-abuse row (for a host that bypasses Traefik: `edge stop --all`, which stays until the session is proven ended) | operator |
| A token or password leaked | revoke it at the provider, then create a new one | `home secrets --emergency`, then `home secrets --rotate <name>` | operator |
| The operator's GitHub account is in doubt | sign out every session, review SSH keys and tokens | part of `home secrets --emergency` (github.com/settings/sessions, /keys, /tokens) | operator |
| A Cloudflare session or token is in doubt | revoke Access sessions; roll the API tokens | part of `home secrets --emergency` (Zero Trust → Users → revoke session; API Tokens → roll) | operator |
| The tunnel's credential leaked | stop, delete the tunnel, create a new one | `edge stop --all` (A → C); a guided step: the operator deletes the tunnel, creates a new one (remotely managed, D6) and stores the new credential; then `edge apply` (C): its plan finds the one new tunnel, repoints each record and, once X6a (#168) lands, writes its ingress lines; `edge start --all` (C → A) | operator |
| The phone or laptop is lost | sign out the device everywhere | `home secrets --emergency` (Google, GitHub, Tailscale device removal, Cloudflare Access) and the travel checklist section D | operator |
| An agent misbehaves on the PC | stop every agent session, then treat every secret it could read as leaked | **gap:** aco has no "stop all" command (section 7 row 34). Until D14, a misbehaving agent could read every secret, so the "token leaked" row applies to all of them | operator |
| Who did what? | read the login and admin events, and the journal | the LOOK page's login-anomaly light, linking to Keycloak's Events page (logins and failed logins kept 90 days, admin events 1 year), plus Grafana and `journalctl` | operator; an agent may read |
| Data was destroyed or tampered with | restore from the last good backup | `edge stop <app>` (A → B); `hal restore <app> --live` (B; section 7 row 18); a check on the container itself; `edge start <app>` (B → A). For a host that bypasses Traefik: `edge stop --all` (A → C), the same restore (C), a check on loopback, `edge start --all` (C → A) | operator |
| After the incident | make the end state permanent and clear the drift | a PR plus `edge apply`, or `edge start`; then `home check` is clean | operator |

**Honest limit.** Today every agent runs as the operator's user. That user is in the `docker` group, which is root on the PC. It can read all of `~/.secrets`. After go-live that includes `config-cli-secret`, which signs in as Keycloak master `admin` with no TOTP. So today:
- every "may an agent run it?" answer is a request, not a wall;
- `edge lock`'s TOTP protects nothing against an agent: the agent can use `config-cli` instead;
- the stop-only polkit rule keeps a human's slip from reopening the tunnel, but an agent can reach root through Docker anyway.

These lines become true only after D14: agents under their own user, without Docker root and without `~/.secrets`. The agent user then gets exactly `edge status`, `edge stop` and `edge rollback` through one narrow rule.

---

## 9. The operator's decisions

**What is ruled and what is open.** Ruled by the operator: D6 (the tunnel, „Cloudflare steuert“, 18.10.2026), D14 (securing the agents is not a go-live gate; it is its own block), the deploy by version tag (section 5), OpenTofu for the external services, Cloudflare and GitHub (L3 in section 3), Grafana for the whole platform (section 8), and, inside D4, Keycloak for every app user. Everything else in D1 to D13, including the rest of D4 and the remote kill switch (D13), is open: the Recommendation column is a proposal, not a ruling.

"Spoken yes" marks a line about secrets, rights, deletion or anything irreversible: it waits for his words. The others take the recommendation if he does not answer.

| # | Question | Recommendation | Options | Spoken yes? |
|---|---|---|---|---|
| D1 | **One catalog** | `fleet.yaml` at the edge root is the catalog: one entry per repo, plus a `run` block for what runs here. OpenTofu, `edge apply`, the LOOK page, Prometheus and the watchers all read it. | (a) as recommended; (b) a second file `services.yaml` beside `external/fleet.yaml` (two lists of names); (c) no catalog, compose labels only (then "Dienst aus" has no switch) | no (default a) |
| D2 | **What happens after a merge in edge** | The operator runs `edge apply` (one command, plan, `yes`), the one publisher. Automatic apply only after D14. | (a) as recommended; (b) the platform part applies by itself after merge and only external waits for him; (c) everything automatic now (a write token on a timer, readable by every agent) | **yes** (rights) |
| D3 | **Re-setup without new passwords** | Back up `~/.secrets` into the encrypted restic repository too, and add a second target off this disk (a USB disk first; later the rclone `gdrive` remote). Build the recovery form: `--repo` on the command line, the password typed at a hidden prompt, the snapshot run picked from the repository itself, one run for secrets and data. Done only when the clean-machine drill passes (section 6). The restic password and the OpenTofu passphrase live in the password manager. This changes the 14.10.2026 ruling "a wizard instead of a backup": the wizard still serves rotation and first-ever. | (a) as recommended; (b) secrets via the wizard only, but the Keycloak DB and app data off-disk (every token re-created, people keep their passkeys); (c) as today: same disk only (a dead disk = first-ever path, people re-enrol, songmaker data lost) | **yes** (secrets, irreversible loss) |
| D4 | **Where people live** | Keycloak for every app user (ruled). songmaker joins through its own cutover item (section 3: client, OIDC login, confirmed account mapping, then the old login goes); deploying alone does not move anyone. The Cloudflare Access group holds one member, the operator, set once with Google + MFA (R4). Operator-only tools (LOOK, Grafana) stay behind Access, **not** behind Keycloak: the LOOK page must still open when Keycloak or the PC's login is broken. Forward-auth at Traefik comes only when a login-less tool must be shared with other people (no such tool today). | (a) as recommended; (b) Keycloak becomes Access's identity provider, so even the operator's tool access lives in Keycloak (one people place, but LOOK goes dark with Keycloak); (c) forward-auth (oauth2-proxy) for Grafana and LOOK now | **yes** (rights; the hash deletion in the cutover too) |
| D5 | **Release rhythm and tool** | One release per service per working session (about 1–2 a day for songmaker), plus one right after a production fix. One implementation, `edge release <service>`: it tags the green tip of the catalog's default branch (hopin: `master`) and writes the `schema:` line. `vYYYY.MM.DD.N` for services, SemVer for libraries. The head runs it as a mechanical step. | (a) as recommended; (b) one tag per landing (about 7 a day for songmaker, nearly main-deploy); (c) only when the operator says "release" | no (default a) |
| D6 | **The tunnel: mode and credential, decided once** (replaces E3 D5) | **Ruled (operator, 18.10.2026: „Cloudflare steuert“): Cloudflare manages the tunnel's configuration.** The ingress lives once, at Cloudflare, and OpenTofu writes its lines from the catalog (planned: X6a, #168; the `config.yml` cleanup is #144, B7). The tunnel's JSON credential stays: the unit reads it with `LoadCredential=` (`tunnel/cloudflared.service.d/20-nonroot.conf`). There is no tunnel token. Operator ruling of 20.10.2026: the catalog owns the whole tunnel configuration, and a line outside it is removed after the plan and `yes` („a“). Cost: the switch is one-way (back = a new local tunnel plus a DNS cutover) per a cloudflared maintainer's statement (cloudflared#1029, 11.09.2023), not per Cloudflare's documentation; and the write token gains Cloudflare Tunnel › Edit. | Ruled; the options considered were (a) remote management, (b) local with `edge apply` rendering `config.yml` into `/etc`, (c) local, `/etc` only | ruled (irreversible) |
| D7 | **The engine under `hal setup`** | Ansible, run locally (`--check --diff` is the dry run), as home-tools#9 recommends; `hal setup` wraps it, so the operator sees one command. | (a) Ansible; (b) plain Python in home-tools (no new tool, but idempotence by hand); (c) NixOS (a reinstall, not now) | no (default a) |
| D8 | **Read-only credentials for the checks** | Yes to: E3's `fleet-read` tokens; a SonarCloud read token; a Keycloak service account with only `view-clients` and `view-realm`; `github/deploy-read`. No to Cloudflare "API Tokens Read" and to a GitHub App for now: each token checks itself, and the full token lists stay a yearly glance. | each line yes/no | **yes** (secrets) |
| D9 | **Groups stay in `realm.yaml`** | The catalog references groups and CI checks both directions; it never generates them, because a generated group list would delete memberships when a service is switched off. | (a) as recommended; (b) the catalog generates the groups | no (default a) |
| D10 | **Disk encryption at the re-setup** | Encrypt (LUKS) with TPM unlock, so the PC still boots by itself after a power cut and the travel plan's remote power-cycle keeps working. **Two checks first:** this board has a working TPM 2.0 and the Ubuntu installer's TPM unlock boots on it; and the recovery key is stored in the password manager and tested once, by unlocking with it, before any data goes on the disk. The disk is unencrypted today. | (a) LUKS + TPM unlock, after both checks; (b) LUKS with a typed passphrase (no unattended reboot); (c) none, as today | **yes** (irreversible at install) |
| D11 | **One `edge` command** | `edge` with subcommands (`apply`, `release`, `status`, `check`, `backup`, `external`, `stop`, `start`, `restore`, `rollback`, `lock`, `people`), mirroring `home`. E3's X1 builds `edge external` as a subcommand, not a separate script. | (a) as recommended; (b) keep separate scripts per job | no (default a) |
| D12 | **What agents may run in an incident** | `status`, `stop <service>`, `stop --all`, and `rollback` within its state-table cell (section 8). Never `start` (it reopens a door), never `apply`, never `restore`, `lock`, revoke or rotate (R4, ruled 16.10.2026, [edge#50](https://github.com/overnightworks/edge/issues/50)). **Enforced only after D14;** until then it is a rule agents are told, not a wall, and it is no go-live gate. | (a) as recommended; (b) agents may also `start` a switch an agent set itself; (c) no incident commands for agents at all | **yes** (rights) |
| D13 | **Remote kill switch** | Tailscale only, for now: `edge stop --all` or `edge lock` in a terminal over Tailscale. No bot. | (a) Tailscale only, as recommended; (b) the Telegram bot with `stop --all` only, after claudebot#19 and D14; (c) the bot also with `lock` via a stored Keycloak `manage-users` account (an admin credential on the PC) | **yes** (rights) |
| D14 | **Secure the agents: their own OS user, privileged credentials out of their reach** | **Decided (operator, 16.10.2026): not before go-live; its own block.** „die Agenten müssen jetzt nicht zuerst abgesichert sein! … weil aktuell kannst du auch alles machen => ist genauso unsicher“. edge#19 may be built and run; D14 is ordered independently. Go-live writes `config-cli-secret`, a master-admin credential, where every agent can read it; the operator accepted that because agents already have the same reach today (root via Docker). The item: an `agent` user, not in `docker` or `sudo`; aco starts sessions as that user; the agent tools log in again under it (Claude, Codex, a fine-grained `gh` token for the work repos only); `~/.secrets` and the restic password stay the operator's; Docker for tests and probe stacks runs rootless under the agent user; `edge status`, `edge stop` and `edge rollback` reach it through one narrow rule. **Cost of doing it:** the largest single item here; every agent tool logs in again; probe stacks move to rootless Docker. **Cost of deferring, honestly:** every agent can read every secret and, after go-live, act as Keycloak master admin (create admins, unlock people, switch off MFA); every agent is root through Docker; every agent can push code and workflows to every repo (`repo`, `workflow`); an agent can delete the backups it can reach; automatic apply (D2) stays impossible; every "agents may not" line in this document is a request. One confused or prompt-injected agent then equals the operator. | (a) before go-live; (b) after go-live, but before any person other than the operator gets an account; (c) defer, as today. **Ruled: not (a); (b) or (c) is chosen when the block is ordered.** | **decided** (rights, secrets) |

**Proposed first step after the rulings.** Open E3 from `ext-as-code.md` with the four changes in section 3, and dispatch X1. Meanwhile, these lanes can run, each with a clear owner:
- the songmaker tag migration (D5 default);
- edge#19 (go-live script), may be built and run now (D14 is not a precondition);
- the "secure the agents" item in home-tools, its own block, ordered independently of go-live;
- the off-disk backup and recovery form in edge, if D3 is (a).


---

## 10. Platform rules: move, spread, scale

**Caller:** the operator, 17.10.2026: „ich will nichts auf Vorrat bauen, aber ich will das system prinzipiell so gestalten, dass die umstellung vom prinzip ein knopfdruck wäre“, and „alles soll auch mehr oder weniger als komponente austauschbar sein mit vertretbarem aufwand! … push back wo ich falsch liege“. Drafted and counter-checked under the rule-change gate in [edge#83](https://github.com/overnightworks/edge/issues/83).

The rules are labelled "§10 rule n", because [edge#50](https://github.com/overnightworks/edge/issues/50) already uses P1–P15 and R1–R4, and `docs/naming.md` uses P1–P3b. This section cites the facts it builds on and does not restate them.

### Three goals, kept apart

- **Move (relocation):** the whole platform goes to a new machine by a short, scripted runbook. Its proof is the planned clean-machine drill (section 6, "The off-disk restore path"), which runs the steps that section lists. D3 is still open (section 9).
- **Spread:** one instance of a service runs on a second machine. It is an item each time, with the "Any second machine" checklist of §10 rule 4 as its done-when.
- **Scale:** N instances of a service run. It is an item each time, and only when a caller needs it. Several rulings and facts make services singletons on purpose:
  - agent-presentator ADR 0006 and its `compose.yaml:1`: SQLite and files, one instance;
  - agent-providers#18 A1b: one replica;
  - edge's `duplicate-edge-name` check ([backup.md](backup.md), edge-check findings);
  - hopin's rooms in process memory (`src/hopin/rooms/manager.py:11`).

### The rules

**§10 rule 1: five inputs, nothing else.**
- A service runs from five things:
  1. its release tag: the image, the compose file, and every plain configuration value as a compose default (section 5);
  2. its catalog entry (section 3);
  3. its secrets, as files from the inventory (section 4);
  4. its data, from its backup record (`backup/apps.toml`);
  5. artifacts it fetches itself at start (model weights, caches), declared rebuildable.
- It has no bind source outside the tracked files of its release checkout: no `~/…` binary or login, no host unix socket of another unit, no Docker socket, no per-machine uid, gid or path. There are two exceptions. The always-on core's socket-proxy (section 3, "Entries, not repositories"). And the agent service's login: one read-only directory bind of `~/.secrets/agent-service/<profile>/`, owned by uid 1000 (the operator's on this PC), because the renewal replaces the file inside it and a file bind would keep the old inode (ruled 20.10.2026, "the next run reads it, without a restart").
- **Proof:** the planned clean-machine drill (section 6).
- **What can be checked:** [edge#50](https://github.com/overnightworks/edge/issues/50)'s CAT row adds a check that flags, in the services an enabled entry resolves to (P13), any bind source that is not a tracked release path and any `docker.sock` mount, in `edge apply`'s plan and in `edge check` against the deployed tag. App compose files live in other repositories, so edge CI cannot see them.
- **A move keeps every public host.** Renaming a host is its own item: it touches the redirect URIs in `keycloak/realm.yaml` and the app's `redirect_uri`, the Access app and the token audience (`docs/naming.md`). DNS and TLS need nothing new, because the hosts live at Cloudflare (L3, section 2).

**§10 rule 2: peers are reached by a configured name.**
- The database, Redis, the issuer, other apps and MCP origins come from a URL or DSN in configuration.
- Names come from compose service names, the catalog host, or `docs/naming.md` (the MCP origin host).
- Never an IP, and never a vendor DNS name. A code default may point only inside its own container.
- "Never an IP" is about reaching a peer. A trust list such as webauth's `trusted_proxies` names a network that the compose file owns.

**§10 rule 3: each app owns its stores.**
- No volume, database or Redis is shared across compose projects. The edge networks are the exception.
- Integration goes through the owning app's API.
- A call between apps carries a signed, short-lived token whose audience is the callee: an issuer token, or the agent-service capability (agent-providers decision record 0001). Never a static secret shared between apps.

**§10 rule 4: singleton by default; spreading and scaling are items.**
- Today's state is allowed: in-process state, SQLite, local files.
- An item that spreads or scales a service has the matching checklist as its done-when.
- **Any second machine** (spread or scale):
  - encrypted links between machines;
  - routing to a remote host, driven by the catalog (edge);
  - peer names that resolve across machines;
  - hardware placement (GPU, `runtime: nvidia`).
- **More than one instance:**
  - shared limiters and locks;
  - files reachable from every instance (the S3 API, or the owning app's API);
  - one leader for background loops, or loops that are safe to run twice;
  - WebSocket and stream fan-out through a shared channel;
  - expand/contract migrations;
  - one clock for every time decision made across instances (webauth#189).
- songmaker is the first expected caller: a second GPU machine. Its list is cut at that pull.

**§10 rule 5: standards at the seams; vendor specifics live in edge, except those the swap table names.**
- Identity: OIDC code flow with PKCE and static endpoints (webauth#49 ruled out discovery at login), W5/W9 logout, and W10 bearer or device login for CLIs.
- Data and runtime: SQL over a DSN, the Redis protocol, the S3 API once files leave a host, OCI images with compose.
- Operations: OpenMetrics `/metrics` where an app exposes metrics, X.509/ACME, secrets as files.
- LLM vendors only behind agent-service.

**§10 rule 6: logs go to stdout and stderr.** No app writes a log file to a volume or a host path.

**§10 rule 7: our own services speak one shape.** It binds every service edge runs for several apps whose protocol we write ourselves (today the agent service). A seam with a standard keeps it (§10 rule 5); an app's internal workers and its browser-facing API are outside.
- **Calls:** HTTP with typed JSON. Every request body carries the contract's version `v`; an unknown version answers 400 `unsupported_version`.
- **Streams:** events from service to app are SSE; media are a chunked HTTP body of their media type; a WebSocket only where both sides stream at once (live audio).
- **Identity:** the app's workload token, plus the person's token when a person acts, both Keycloak tokens whose audience is the service (§10 rule 3).
- **Refusals:** an HTTP status and a JSON body with one code from the service's closed list. A refusal that means the same as an agent-service refusal takes its name and status (agent-providers `docs/wire.md`, "The run").
- **MCP** only where a model calls an app's tools, never for an app calling a service.
- **One client library per service** owns its wire types; the service and the apps import them, and no app re-implements them.

Health: `run.health` (section 3).

**§10 rule 8: a GPU service holds a declared budget.** It binds every service on a platform machine that runs models on a GPU.
- Its configuration declares the card it uses and its memory budget in GB; its compose file (or, for a host process, its settings) is the one owner of both, and the catalog repeats neither.
- Within its budget the service manages its own models, resident or loaded and evicted by its own policy. It never exceeds the budget and never frees memory for another service.
- At start it loads what it needs to serve. If it cannot have its budget, it exits non-zero and its restart policy retries. While running, a service that can no longer serve answers its health check as not ready, never healthy.
- The budgets on one card, plus each process's CUDA context, sum to at most that card's memory. A GeForce card enforces no budget, so the budget is declared and each service proves it by measuring its own peak after warm-up.
- Priority between services is their budget size or their card, ruled by the operator; nothing arbitrates at runtime.
- **What can be checked:** once two GPU services are compose services edge sees, edge's compose contract check sums the declared budgets per card. Until then each service's own item holds its budget.
- Moving a GPU service to another card or machine follows §10 rule 4.

**Cited, not restated:**
- backup and the restore test: section 2, L5, and `backup/apps.toml`;
- health: `run.health` in the catalog (section 3);
- secrets: section 2, L1, and section 4, owned by home-tools;
- deploy and schema: section 5;
- names: `docs/naming.md`.

### The swap table: the real cost of swapping each component

| Component | What apps see | Real swap cost |
|---|---|---|
| Keycloak | OIDC through webauth and agent-service | **A migration, not a configuration change.** Accounts are keyed to the issuer's `sub`, and passkeys live in the IdP, so every person re-enrols and every app remaps its accounts. edge rewrites realm-as-code, go-live, `edge lock` and the probes. The group path shape lives in webauth (`oidc_login.py`, `_require_group_path`) and in agent-service (`routing.py`, `APP_GROUP_PREFIX`). A flat-name mode waits until an IdP swap is pulled |
| Tailscale | nothing; no app references it | home-tools (L0); also D13 |
| Cloudflare DNS, tunnel and Access | HTTP behind a proxy, plus webauth's `trusted_proxies`; no `cf-*` header in any app | edge and OpenTofu (L3). Access is planned (X8b) and will gate D4; no login-less tool is published before it. The aco board's `access_jwt.py` is being retired (webauth#49 AC1) |
| Traefik | the neutral `edge.name` label | edge only. A second machine needs routing that label discovery on this host cannot give; that is decided in that item (§10 rule 4) |
| Postgres | DSN | configuration plus a dump and restore. presentator's SQLite sits behind ports (agent-presentator ADR 0006), so moving it is an adapter swap |
| Redis | URL; songmaker runs its own | Valkey drops in. arq ties songmaker's queue to Redis, which is accepted |
| Object storage | none today | arrives with a scale item (§10 rule 4) |
| step-ca | one trusted root file | edge |
| Prometheus, Grafana, Gatus, ntfy | `/metrics`, stdout | edge (edge#4) |
| restic | per-app dumps | edge |
| Docker Compose | OCI and compose | low for apps; high for edge (apply, check, backup labels, barrier, watchers and the Traefik provider all speak Compose). presentator's `docker.sock` stays a high cost until agent-presentator#197 removes it (section 5, "How each service joins") |
| `~/.secrets` | files | home-tools (L1). Apps stay unchanged only once gap 21 (section 7) has moved their secrets to files |
| GitHub | git | not a swap goal (accepted) |
| LLM providers and LiteLLM | the agent-service API | behind agent-providers#18 |

### Where the operator's wish overreaches

- **„Alles austauschbar / perfekt“:** the target is a known swap cost with one owner, not cheap swaps. The IdP, Compose on edge's side, and GitHub are expensive by nature.
- **„Ein Knopfdruck“:** this is a scripted runbook for moving the whole platform, once §10 rule 1 holds and D3 is ruled. Spreading or scaling a service is an item each time.
- **„Alle Apps jetzt anpassen“:** this collides with „nichts auf Vorrat“ and with webauth#49's app freeze. Only hazards and relocation blockers with a caller go now.
- **„Keycloak wechseln“:** the cost lies in people and accounts, not in claim names.
- **„Tailscale wechseln“:** it is already cheap.
- **„Konfigurationen überall konsistent“:** consistency means one owner per fact (the catalog, the inventory, `docs/naming.md`), not one config format for every app.
