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
