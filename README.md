# edge

The gatekeeper in front of the tools on the operator's PC. Traefik listens on
`127.0.0.1:8081` only and routes `<name>.<EDGE_DOMAIN>` to the container that
carries the label `edge.name=<name>`. Traefik never sees the Docker socket: a
GET-only socket proxy hands it the container list and events.

Nothing here is reachable from outside until the operator applies the steps A8
below. Cloudflare Access enforcement (and JWT checks) arrives with that step;
until then the edge is a local service only.

| File | Owns |
|---|---|
| `compose.yaml` | the three services, image pins (version and digest), the socket directory volume |
| `traefik.yml` | the one entrypoint and the providers |
| `dynamic/` | file-provider routes for host processes (empty for now) |
| `.env.example` | the two settings the stack needs |
| `cloudflared-config.example.yml` | the tunnel ingress template the operator applies in A8 |

## Threat model

Access protects the edge; on the machine any process reaches any tool.

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

## Configure

```sh
cp .env.example .env     # .env is local; never commit it
getent group docker | cut -d: -f3   # put this number into DOCKER_GID
```

## Start, readiness, stop

```sh
docker compose up -d
```

Readiness: the stack is ready when `127.0.0.1:8081` listens and an unknown host
gets 404. The procedure gives up after 30 seconds:

```sh
set -a; . ./.env; set +a
timeout 30 sh -c '
  until ss -ltnH "sport = :8081" | grep -q "127.0.0.1:8081"; do sleep 1; done
  until [ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
      -H "Host: unknown.$EDGE_DOMAIN" http://127.0.0.1:8081/)" = 404 ]; do sleep 1; done
' && echo ready
```

Stop: `docker compose down`.

What the entrypoint answers without a route:

- unknown host or invalid name: `404`;
- HTTP/1.1 request without a `Host` header: `400` (Go's HTTP server rejects it);
- HTTP/1.0 request without a `Host` header: `404`.

## The probe profile

`hello` (traefik/whoami, labelled `edge.name=hello`) lives under the compose
profile `probe`, so a plain `docker compose up -d` never starts it.

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

## Rollback

```sh
docker compose --profile probe down
```

This removes all three containers and the `edge-probe` network; the listener on
`127.0.0.1:8081` disappears. The socket directory volume is tmpfs and holds
nothing; `docker compose --profile probe down --volumes` removes its definition
too. Then revert the commit.

## Operator steps A8 (going live)

The operator does these himself, in this order; an agent never applies them.
Each step must hold before the next one starts, because the DNS wildcard is
what makes the edge reachable.

1. **Presentator's Access app.** Check that `presentator.hallucinai.de` keeps
   its own Access app (its people, plus the `/sources/*` bypass) and still
   answers 401 to a request without a session.
2. **Wildcard Access app.** Create the Access app for `*.hallucinai.de`,
   operator only. Note its AUD tag and the team name.
3. **Ingress line.** Apply `cloudflared-config.example.yml` to
   `/etc/cloudflared/config.yml` with the real tunnel ID, team name and AUD tag
   (the wildcard rule always carries `access.required`, `teamName` and
   `audTag` together), then restart cloudflared. The apex, `www` and
   presentator rules stay ahead of the wildcard.
4. **DNS `*`.** Add the proxied `*` CNAME to the tunnel. Only now do
   `<name>.hallucinai.de` requests reach the edge.
