# The agent service

Reference and runbook for the operator and for an engineer changing the agent service. The code is
the truth: `compose.yaml` (the `agent-service` service, its `agent` and `egress` networks),
`agent-service/service.toml`, `agent-service/image/` (the image recipe), `agent-service/live_smoke.py`
and the probes under `probe/agent-service/`. The egress gateway it leaves through is in
[internal-ca.md](internal-ca.md); the platform picture is [architecture.md](architecture.md).

## The image

`agent-service/image/` is the recipe of the service's image (agent-providers `v0.4.0`, Claude Code
`2.1.302`; the pins are in the Dockerfile and `requirements.txt`). Every byte that enters it is
pinned: both base images by digest, the Python packages by hash, the Claude CLI by its sha256. A
changed or vanished artifact fails the build. The image is never pulled from a registry, because its
name could be anyone's on a public one (`pull_policy: never`), so it is built on the PC.
`agent-service/setup` builds it when it is missing, and `hal apply` asks to run that setup before it
switches the unit on. The build line it runs, from the repository root:

```sh
docker build -f agent-service/image/Dockerfile -t edge-agent-service:0.4.0-2.1.302 agent-service/image
```

A release bump runs `python3 scripts/agent_service_lock.py <tag>` (it needs `git` and `uv`), the
only writer of `requirements.txt`, and changes the CLI pin in the Dockerfile and the image name in
`compose.yaml` and in the build line above together. `probe/agent-service/image.py` builds without cache and starts the
entrypoint from the clean image. No command builds the image yet: the `docker build` line is the
operator's step.

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
is read without echo and written to `token.new` (`umask 077`) before the rename over `token`.
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

## An app's side

An app reaches the service by joining the `agent` network, whose name is fixed (`hal_agent`, so the
project's rename does not move it), as an external network:

```yaml
services:
  app:
    networks: [default, agent]
networks:
  agent: {external: true, name: hal_agent}
```

and calls `http://agent-service:8080` (the service's own default port; the smoke and the unit probe
use it). No CA mount is needed: the service is plain http on an isolated internal network, and no
host process can reach it, because the network has no host address and publishes no port.

## Proofs

Re-run the probes in `probe/agent-service/` for every Traefik, Docker or agent-providers upgrade.

**The live smoke** is one command, run in the live checkout after `hal apply` and with the image
built: `cd ~/git/edge && python3 agent-service/live_smoke.py`. It signs you in as yourself (the
`songmaker` client's code flow, edge#184 D6), recreates the service and runs the live cells L2-L7
and E6-E10, E12 against it, which its docstring lists; E3-E5 and E10's packet windows stay with the
probe's `egress.py`. It needs the image, the client's secret file and the login token under
`~/.secrets`, and the person in the group `/ki-abo` (add yourself once in the admin console on port
8090). Run it at once, as the workload token lives five minutes, and send the head its report.

**The unit probe** is `probe/agent-service/unit.py --scratch DIR`; its docstring gives the full command
line (the probe-stack lock and a timeout). It starts the real service in a throwaway project
with a dummy login and drives the cells U1 to U11 and U15, which its docstring and code define: the
process, the container hardening, the tmpfs and login mounts, a renewal seen at once, and the
refusals (bad working-root mode, missing login directory, token at the wrong mode, no token).
U12 is `egress.py --target service`; U13 is the static facts in `tests/test_agent_service_unit.py`.
`--cell smoke` drives U14, the `agent-smoke` against the service with a stand-in issuer, and U16 runs
the cells of `live_smoke.py` there, past its sign-in: all hold except L2, which must fail with the
dummy login. L4 is proven only live, with the real login; the probe has no L4 cell.
