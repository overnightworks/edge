# The agent service

Reference and runbook for the operator and for an engineer changing the agent service. The code is
the truth: `compose.yaml` (the `agent-service` service, its `agent`, `agent-songmaker`, `agent-presentator` and `egress` networks),
`agent-service/service.toml`, `agent-service/image/` (the image recipe), `agent-service/live_smoke.py`
and the probes under `probe/agent-service/`. The egress gateway it leaves through is in
[internal-ca.md](internal-ca.md); the platform picture is [architecture.md](architecture.md).

## The image

`agent-service/image/` is the recipe of the service's image (agent-runtime `v0.7.0`, Claude Code
`2.1.302`; the pins are in the Dockerfile and `requirements.txt`). Every byte that enters it is
pinned: both base images by digest, the Python packages by hash, the Claude CLI by its sha256. A
changed or vanished artifact fails the build. The image is never pulled from a registry, because its
name could be anyone's on a public one (`pull_policy: never`), so it is built on the PC.
`agent-service/setup` builds it when it is missing, and `hal apply` asks to run that setup before it
switches the unit on. The build line it runs, from the repository root:

```sh
docker build -f agent-service/image/Dockerfile -t edge-agent-service:0.7.0-2.1.302 agent-service/image
```

A release bump runs `python3 scripts/agent_service_lock.py <tag>` (it needs `git` and `uv`), the
only writer of `requirements.txt`, and changes the CLI pin in the Dockerfile and the image name in
`compose.yaml` and in the build line above together. `probe/agent-service/image.py` builds without cache and starts the
entrypoint from the clean image.

## The service

`agent-service` (profile `agent-service`) is that image with `agent-service/service.toml`: one
container, one worker, no published port. It runs as `1000:1000` with `init`, a read-only root, no
capability, `no-new-privileges`, no core dump and `restart: unless-stopped`; `/tmp` (its `HOME`) and
the working root `/run/agent-service/cli` are size-bounded tmpfs mounts owned by 1000 with mode 0700
(the service refuses to start on a working root that is not owned by its user with exactly that
mode), all `nosuid,nodev,noexec`. The unit `agent-service` of `fleet.yaml` starts it with
`agent-egress`: `hal apply` brings both up. A missing image or login directory stops the start
(`pull_policy: never`, `create_host_path: false`), so apply reports the unit partial and never on.

A change to `service.toml`, `compose.yaml` or the image reaches the running service only by
recreating it, which ends every run in flight:
`docker compose --profile agent-service up -d --force-recreate agent-service`, in the live checkout
after `git -C ~/git/edge pull --ff-only` and, for an image change, a fresh build.

## The login

The service runs the Claude CLI under the operator's subscription, with the one token that
`claude setup-token` prints, in one file inside a read-only directory bind:

```
~/.secrets/agent-service/              0700
~/.secrets/agent-service/claude/       0700, uid 1000   (bound at /run/agent-service/login/claude)
~/.secrets/agent-service/claude/token  0600, uid 1000, one line
```

`agent-service/setup` makes it, in the operator's terminal and after `hal apply` has asked: it creates
the directories with 0700, narrows a too-open token file to 0600, and when the token is missing or
refused it asks the operator to run `claude setup-token` in another terminal and paste the line, which
is read without echo, judged by the reader's rules as the exact bytes to write, and only then written
to `token.new` (created 0600) before the rename over `token`. A refused paste leaves the old token
as it was; the folders are opened without following a link and locked for the swap.
`agent-service/setup --check` changes nothing and says the first gap. The token is pasted only
there, never on a command line or in an issue. The file is a regular file of at most 16 KiB with one line, the token: no JSON,
no API key (`sk-ant-api…`), nothing group or others can touch. The service refuses to start
otherwise, naming `profiles.claude.token_file` and the path, never the content. `home secrets` lists
the entry `agent-service-claude`.

**Why 1000:1000.** The service's reader refuses a login file that group or others can touch, so the
container user must own the directory and the file. 1000 is the operator's uid on this PC, so his
own `claude setup-token` writes a file the container can read. Another uid needs its own renewal
procedure, which is not covered here. It is a deployment choice, not the reader's requirement.

**Renewal.** `agent-service/setup --renew` takes a new pasted line, writes it to `token.new` beside the
old one and swaps the name (an atomic rename); the next run opens the file anew, without a restart. The bind
is the directory, never the file: a file bind (or a compose `secrets:` file) would keep the old
inode, and the rename would never reach the running container. A missing directory fails the start
instead of becoming a root-owned one; the directory itself stays for ever, so never replace or
re-create it.

## The signing key

The service signs the capability of every admitted app-tool run with one RSA-3072 key; the app's
verifier (songmaker-web) checks it against the public half. The service never makes the key
(agent-runtime `docs/operations.md`, "App tools"); only `agent-service/setup` does, in the operator's
terminal after `hal apply` has asked:

```
~/.secrets/agent-service/signing/              0700, uid 1000
~/.secrets/agent-service/signing/private.pem   0600, uid 1000, one unencrypted PKCS#8 PEM block, at most 16 KiB
~/.secrets/agent-service/signing/public.pem    0644, uid 1000, the SPKI half `openssl pkey -pubout` derives
```

**The contract.** Setup makes the pair with the host's `openssl` (version 3), and key bytes travel only
over pipes: never on a command line, never in a printed line. Every path is reached without following a
link, under one lock on the folder, and nothing is ever written through a link. `private.pem` is created
exclusively (`private.pem.new`, then `link(2)`, which refuses any existing entry). A plain run and
`--renew` never replace an existing key. A missing public half is derived again, and a public half with
another mode is set to 0644. Setup refuses, in one line that names the file and the next step, a pair
it cannot use, and changes nothing, `--renew` and the token included (the key is judged first):

- a private key that is malformed, encrypted, of another size or exponent, failing openssl's own key
  check, or missing while `public.pem` is still there, or a public half that is malformed or not its pair: the line names
  `agent-service/setup --replace-signing-key`;
- a wrong owner, a mode other than 0600 or a link: the line asks for the operator's own fix, because setup
  never narrows or unlinks those.

`agent-service/setup --check` changes nothing and exits 0 for a sound pair, 1 for the first gap, a
refusal or a leftover of an interrupted run, and 2 when openssl or the host cannot answer; an openssl that exits 1 on a key counts as refusing it
only when it passes the same check on a throwaway key. A leftover (`*.new`, `*.next.pem`) beside a
sound pair is cleared by the next plain run; beside a missing or unusable pair only the `*.new` of an
interrupted creation is, and the `*.next.pem` wait for `--replace-signing-key`. A temporary entry that
is a link, no regular file or not the service's is never unlinked. `home secrets` lists both files; a public half at 0644 is no finding.

**Replacing the key.** `agent-service/setup --replace-signing-key` is the one command for a planned
rotation, for a key that may have leaked, and for a lost private key (there is no backup: the key protects
no stored data). It works only at a terminal, says what it does, and goes on after a typed `yes`. The old
private key's directory entry is removed by the rename; the blocks are not erased. The stages:

| Stage | State | What holds |
|---|---|---|
| R0 | the new pair is being made (openssl fails, the new key fails its check, an interruption) | the active pair is unchanged, or still missing; running containers are unaffected; run the command again, or plain setup to clear leftovers |
| R1 | interrupted between the two renames | the new private key sits beside the old public half: `--check` says so and plain setup refuses; the service still signs with the old key it read at startup, and songmaker-web still trusts the old one, until the service is recreated, then calls fail with 401; the old pair is gone, so run the command again |
| R2 | both renamed, nothing recreated | `--check` is 0, but the old key still signs and is still trusted: a compromise is not recovered yet |
| R3 | one container recreated | every tool call fails with 401 until the other is recreated |
| R4 | both recreated | done once a new capability verifies against the new `public.pem` and every songmaker-web container was created after it |

The command ends by naming the two recreates: agent-service with `python3 agent-service/live_smoke.py
--mcp-signer` while songmaker-web is not on the network (otherwise as in "The service"), then
songmaker-web with songmaker's own deploy command. A file bind keeps the old inode, so only a recreate
reaches a container, never a restart. There is no grace period.

## What a turn leaves behind

The service has no writable persistent mount. The CLI's per-run home and work directories and `/tmp`
are tmpfs ([The service](#the-service)), which the kernel may page to swap, and a restart or recreate
drops them. The only host binds are `service.toml` and the login directory, both read-only
([The login](#the-login)); the login reaches the CLI only through its environment. The service sets
no `logging` override, so its logs follow the Docker daemon's settings; their destination and
retention are set there, not in this file (log retention: edge#167,
<https://github.com/overnightworks/edge/issues/167>). What the service itself keeps of a run is
stated in agent-runtime's section
[What a run keeps](https://github.com/overnightworks/agent-runtime/blob/main/docs/wire.md#what-a-run-keeps).
The content of
a turn leaves this PC for Anthropic, which generates the answer.

**Whose account.** Only the operator and his own test accounts are members of `/services/agent-service/subscription`, so his
Claude subscription serves only his own use. Every other person reaches the service only through the
API route (`/services/agent-service/api`), under Anthropic's commercial API terms. That route is not
enabled yet (`api_groups` and `[route]` are unset in `service.toml`), so a member of the api group
gets 403 `not_enabled`. An app's privacy text therefore describes only the API route.

## An app's side

An app reaches the service by joining its own agent network, `hal_agent_<app>` (`hal_agent_songmaker`,
`hal_agent_presentator`), as an external network. The names are fixed, so the project's rename does not
move them, and no app sees another's network (the older shared `hal_agent` stays, with the service and
`agent-smoke` on it, until each app has moved):

```yaml
services:
  app:
    networks: [default, agent]
networks:
  agent: {external: true, name: hal_agent_songmaker}
```

and calls `http://agent-service:8080` (the service's own default port; the smoke and the unit probe
use it). No CA mount is needed: the service is plain http on an isolated internal network, and no
host process can reach it, because the network has no host address and publishes no port.

## Proofs

Re-run the probes in `probe/agent-service/` for every Traefik, Docker or agent-runtime upgrade.

**The live smoke** is one command, run in the live checkout after `hal apply` and with the image
built: `cd ~/git/edge && python3 agent-service/live_smoke.py`. It signs you in as yourself (the
`songmaker` client's device grant, edge#184 D6): it prints a link and a code and opens the link
where a browser is there; you sign in and confirm on any device, and the run goes on by itself,
nothing is pasted (it waits as long as the code lives). Keycloak starts a
device sign-in only for a request with songmaker's client secret, which lives in `~/.secrets` on
this machine, so no stranger can send you a link to confirm; no other client allows the grant.
It recreates the service and runs the live cells L2-L7
and E6-E10, E12 against it, which its docstring lists; E3-E5 and E10's packet windows stay with the
probe's `egress.py`. It needs the image, the client's secret file and the login token under
`~/.secrets`, and the person in the groups `/apps/songmaker` and `/services/agent-service/subscription`.
The realm declares these groups but grants no membership: add yourself once in the admin
console on port 8090. The smoke names the possible causes of a refusal for rights. Run it at once, as
the workload token lives five minutes, and send the head its report.

**The unit probe** is `probe/agent-service/unit.py --scratch DIR`; its docstring gives the full command
line (the probe-stack lock and a timeout). It starts the real service in a throwaway project
with a dummy login and drives the cells U1 to U11 and U15, which its docstring and code define: the
process, the container hardening, the tmpfs and login mounts, a renewal seen at once, and the
refusals (bad working-root mode, missing login directory, token at the wrong mode, no token).
U12 is `egress.py --target service`; U13 is the static facts in `tests/test_agent_service_unit.py`.
`--cell smoke` drives U14, the `agent-smoke` against the service with a stand-in issuer, U17, the
refusals before any run (wire v1: 400 `unsupported_version`, outside the app: 403 `forbidden`,
the api group only: 403 `not_enabled`), and U16 runs
the cells of `live_smoke.py` there, past its sign-in: all hold except L2, which must fail with the
dummy login. L4 is proven only live, with the real login; the probe has no L4 cell.
`--cell containment` adds a throwaway signing key and a fake app that lists tools on
`agent-songmaker`, and runs `agent-smoke --tool NAME:EFFECT` against it: B1 (the run with the relay
is accepted and fails at the upstream) and B2 (a capability written into a tool description fails
the relay's owner and the run) of agent-runtime#440. B2 (c) asserts that the runner's failure is
`CapabilityReflected`, that no run reaches `CliLoginRefusedError` and that no announcement verdict
other than `RELAY_NOT_CONNECTED` appears; a `RELAY_NOT_CONNECTED` line is tolerated. The runs seen
so far show the relay owner's and then the runner's `CapabilityReflected` line, without any
verdict line. The cells print counts and event names, never a log line. Its loopback capture (the
(a) parts) is not driven yet.
