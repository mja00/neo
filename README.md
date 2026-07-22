# Neo

A batteries-included, Docker Compose Matrix homeserver stack you can stand up on a
host in minutes. Synapse at the core, with optional Element Web, an admin UI,
Coturn for voice/video, and Prometheus + Grafana monitoring — each toggled with a
Compose profile.

Neo terminates no TLS itself. It expects [nginx proxy manager](https://nginxproxymanager.com/)
(NPM) to sit in front, handling Cloudflare → local-service TLS. Containers join the
network NPM already owns and are proxied by name.

## Quick start

```bash
git clone <this-repo> neo && cd neo
./scripts/bootstrap.sh          # creates .env, then stops
$EDITOR .env                    # set NEO_SERVER_NAME + *_HOST values, choose profiles
./scripts/bootstrap.sh          # generates secrets, renders config, makes signing key
docker compose up -d
./scripts/register-user.sh      # create the first admin
```

`bootstrap.sh` prints the exact DNS records and NPM proxy hosts to create when it
finishes. It is safe to re-run: existing secrets and the signing key are never
overwritten.

## What you configure

Everything lives in `.env`. The essentials:

| Variable | What it is |
| --- | --- |
| `NEO_SERVER_NAME` | Your Matrix identity domain (`@you:example.com`). **Immutable after first start.** |
| `NEO_MATRIX_HOST` | Hostname the homeserver is reached at (client + federation). |
| `NEO_ELEMENT_HOST` / `NEO_ADMIN_HOST` / `NEO_GRAFANA_HOST` | Hostnames for the optional UIs. |
| `NEO_TURN_HOST` | TURN hostname (DNS-only record → `PUBLIC_IP`). |
| `PUBLIC_IP` | This host's routable IP — required for Coturn. |
| `COMPOSE_PROFILES` | Which optional services run: `element,admin,coturn,monitoring`. |
| `NEO_PROXY_NETWORK` | The external Docker network NPM owns (default `nginxproxymanager_default`). |
| `NEO_PUID` / `NEO_PGID` | UID/GID Synapse runs as — match the owner of `./data`. |

Secrets (`*_SECRET`, `*_PASSWORD`) are left blank; `bootstrap.sh` fills them.

## Components

| Profile | Service | Loopback port (`NEO_PORT_*`) |
| --- | --- | --- |
| _core_ | Synapse | `127.0.0.1:8801` (→ 8008) |
| _core_ | well-known delegation | `neo-wellknown:80` (or CF Worker) |
| `mas` | Matrix Authentication Service | `127.0.0.1:8802` (→ 8080) |
| `element` | Element Web | `127.0.0.1:8803` (→ 80) |
| `admin` | Synapse Admin (Ketesa) | `127.0.0.1:8804` (→ 80) |
| `coturn` | Coturn (VoIP) | _not proxied — host ports_ |
| `monitoring` | Prometheus + Grafana + node-exporter + cAdvisor | `127.0.0.1:8805` (→ Grafana 3000) |

Postgres runs on an internal-only network. Redis is intentionally absent — a
single (monolithic) Synapse does not use it; it belongs with a future worker setup.

## nginx proxy manager setup

How NPM reaches Neo depends on how NPM itself is networked:

- **NPM in host-network mode** (its default when it publishes 80/443 directly, and
  the most common setup): it can't resolve container names, so Neo publishes each
  web service on `127.0.0.1:<port>` (the `NEO_PORT_*` values) and NPM forwards to
  `127.0.0.1:<port>`. This is the default the setup assumes.
- **NPM sharing Neo's Docker network:** forward to the container name + port
  instead (e.g. `neo-synapse:8008`). The loopback ports are harmless in this case.

Create these proxy hosts (Forward Scheme **http**, Forward Hostname `127.0.0.1`):

1. **`NEO_MATRIX_HOST` → `127.0.0.1:${NEO_PORT_SYNAPSE}`** — proxy the entire
   `/_matrix` prefix (client **and** federation ride this). In the Advanced tab,
   raise `client_max_body_size` to match `SYNAPSE_MAX_UPLOAD_SIZE`, and restore the
   real client IP from Cloudflare (see below).
2. **`NEO_SERVER_NAME` (apex) → `127.0.0.1:80`** (the `neo-wellknown` container) —
   use a **custom location** for `/.well-known/matrix/` only, so any existing site
   on the apex is untouched. *If the apex is served elsewhere (a different NPM/host),
   you can't add a location here — instead serve the two well-known files at the
   Cloudflare edge with the Worker in [`cloudflare/`](cloudflare/) and set
   `NEO_WELLKNOWN_EXTERNAL=true`.*
3. `NEO_ELEMENT_HOST` → `127.0.0.1:${NEO_PORT_ELEMENT}`, `NEO_ADMIN_HOST` →
   `127.0.0.1:${NEO_PORT_ADMIN}`, `NEO_GRAFANA_HOST` → `127.0.0.1:${NEO_PORT_GRAFANA}`.

### Real client IP

Traffic arrives Cloudflare → NPM → Synapse. Synapse trusts `X-Forwarded-For`
(`x_forwarded: true`). Make sure NPM forwards the true client IP (from Cloudflare's
`CF-Connecting-IP`), otherwise every user looks like one address and login /
registration rate limits and IP bans misfire.

### Cloudflare

Add a rule for the matrix host to **bypass cache** and disable Rocket Loader /
minification on `/_matrix/*` and `/.well-known/matrix/*` so API responses are not
transformed. Note the free tier caps request bodies at **100 MB** regardless of
`client_max_body_size` — keep uploads under that.

## Coturn (VoIP)

Coturn cannot be reverse-proxied — TURN/STUN are not HTTP. It runs with
`network_mode: host` and needs:

- A **DNS-only (grey-cloud)** record for `NEO_TURN_HOST` pointing at `PUBLIC_IP`.
  Cloudflare will not pass UDP. This exposes the origin IP — a deliberate trade-off.
- Host ports `3478` (STUN/TURN) and `5349` (TURNS), plus the UDP relay range
  `COTURN_MIN_PORT`–`COTURN_MAX_PORT`, open in any firewall.
- For `turns://` (5349): a TLS cert. Set `COTURN_CERT_DIR` to a host cert directory
  (mounted read-only at `/certs`) and `COTURN_TLS_CERT` / `COTURN_TLS_KEY` to the
  paths inside it. **Certs do not auto-reload** — restart Coturn after renewal:
  `docker compose restart coturn`.

## Users and registration

The helper scripts detect whether MAS is enabled and route accordingly.

**Built-in auth (no `mas` profile)** — registration closed except via tokens:
- **First admin:** `./scripts/register-user.sh --admin` (shared secret, bypasses tokens).
- **Invite others:** `ADMIN_TOKEN=syt_... ./scripts/mint-token.sh 1` mints a
  single-use token. Get `ADMIN_TOKEN` from Element → Settings → Help & About →
  Advanced → Access Token.

**With MAS (`mas` profile)** — MAS owns auth (see below).
- **First admin:** `./scripts/register-user.sh --admin` (runs `mas-cli manage register-user`).
- **Invite others:** `./scripts/mint-token.sh 1` mints a MAS registration token;
  the invitee signs up at `https://<NEO_AUTH_HOST>`.
- **Synapse Admin token:** `./scripts/mas-admin-token.sh <username>` — required to
  log into Ketesa, because password/compat sessions can't use the admin API.

## Authentication with MAS

Add `mas` to `COMPOSE_PROFILES` **before first launch** to delegate authentication
to [Matrix Authentication Service](https://element-hq.github.io/matrix-authentication-service/)
(OAuth2/OIDC). This is an install-time decision — switching auth modes after users
exist is painful. Password login still works (MAS's compatibility layer), and
self-service signup is enabled behind invite tokens.

What Neo wires up for you (via `bootstrap.sh`):
- A `mas` container and its own `mas` Postgres database.
- `config/mas/config.yaml` generated once (keeps MAS's secrets/keys) and patched
  with this deployment's URLs, database, and the shared `MAS_MATRIX_SECRET`.
- Synapse's `homeserver.yaml` gets the `matrix_authentication_service` block, and
  local registration/password auth are disabled (Synapse won't start otherwise).
- The client well-known gains the `org.matrix.msc2965.authentication` block.

You still wire two things in NPM (bootstrap prints them, with your actual port):
- A proxy host `NEO_AUTH_HOST → 127.0.0.1:${NEO_PORT_MAS}`, plus its orange-cloud DNS record.
- On the matrix host, an **Advanced custom location ordered before** the catch-all,
  sending `login`/`logout`/`refresh` to MAS:
  ```
  location ~ ^/_matrix/client/(.*)/(login|logout|refresh) { proxy_pass http://127.0.0.1:8802; }
  ```

Note: mobile **Element X** and QR-code login require MAS; this is what enables them.
Greenfield only — no `syn2mas` migration needed.

## Backups

Three things are unrecoverable if lost — back them up off-host:

1. **Database:** `docker compose exec postgres pg_dump -U synapse synapse | gzip > synapse.sql.gz`.
   Restore into a fresh **UTF8 + C-locale** database only. With MAS, also dump the
   `mas` database (`pg_dump -U synapse mas`).
2. **Media + signing key:** the `./data/synapse` directory (`signing.key` here is
   your federation identity — losing it breaks trust with every server you know).
3. **Secrets:** `.env` (all secrets) and, with MAS, `config/mas/config.yaml`
   (MAS's own encryption + signing keys).

## Verifying a deployment

- `docker compose ps` — all selected services healthy.
- Federation: run `NEO_SERVER_NAME` through
  [federationtester.matrix.org](https://federationtester.matrix.org/).
- Uploads: send a file at the configured max through a client.
- VoIP: force a relayed (TURN) call and confirm it connects.
- Monitoring: Grafana → the provisioned dashboards (**Synapse**, **Node Exporter
  Full** for host metrics, **Cadvisor** for per-container usage); Prometheus →
  Status → Targets shows `synapse`, `node`, `cadvisor` all UP.

## Performance tuning

The defaults are tuned for the target host — **Xeon D-1540 (8c/16t), 32 GB ECC,
NVMe RAID** — running a private instance for a few users:

- **Postgres** (in `docker-compose.yml` `command`): `shared_buffers=2GB`,
  `effective_cache_size=8GB`, NVMe cost/concurrency (`random_page_cost=1.1`,
  `effective_io_concurrency=200`), and parallelism matched to 8 cores.
- **Synapse** (`caches` in `homeserver.yaml`): `global_factor: 2.0`, larger event
  cache — there's ample RAM, so keep caches warm.
- **Resource limits** per service (`deploy.resources.limits`) so nothing can
  balloon: Synapse/Postgres 4 GB each, Prometheus 1 GB, Grafana 512 MB, the rest
  ≤256 MB. Generous for this load — they're guard rails, not a squeeze.
- **Prometheus** retains 30 days (cheap on 450 GB NVMe).

Moving to a smaller/larger box? Scale `shared_buffers`/`effective_cache_size` with
RAM and revisit the limits. For many more users, enable Synapse workers (the
deferred profile) — not worth the complexity at this scale.

## Deliberately deferred

- **Workers / scaling:** ships monolithic; workers (and Redis) are a future profile.
- **Bridges:** not included; the profile pattern makes them easy to add.
