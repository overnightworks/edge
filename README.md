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
| `keycloak/go-live` | the one command that takes Keycloak live (see [docs/keycloak.md](docs/keycloak.md#go-live)) |
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

The one login for the apps: its two doors, the realm as code, secrets, go-live, apply, upgrade and the
probe are in [docs/keycloak.md](docs/keycloak.md).

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

**The smoke.** In the live checkout, after `hal apply` and with the image built (above):

```sh
cd ~/git/edge && python3 agent-service/live_smoke.py
```

It signs you in as yourself (the `songmaker` client's code flow, edge#184 D6), recreates the service
and runs the live cells L2-L7 and E6-E10, E12 against it, which the module docstring lists; E3-E5 and
E10's packet windows stay with the probe's `egress.py`. It needs the image, the client's secret file and
the login token under `~/.secrets`, and the person in the group `/ki-abo` (add yourself once in the
admin console on port 8090). Run it at once, as the workload token lives five minutes, and send the head
its report.

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
`agent-smoke` against the service with a stand-in issuer, so the tokens verify and the CLI meets
the real upstream with the dummy login (`run_started`, then `failed`, no token in any output). U16
runs the cells of `live_smoke.py` there, past its sign-in: all hold except L2, which must fail with
the dummy login. L4 is proven only live, with the real login; the probe has no L4 cell.

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
