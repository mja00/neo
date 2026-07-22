#!/usr/bin/env bash
# Neo bootstrap: generate secrets, render config, create the Synapse signing key.
# Safe to re-run — existing secrets and the signing key are never overwritten.
set -euo pipefail

cd "$(dirname "$0")/.."

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; BLD=$'\033[1m'; RST=$'\033[0m'
info() { printf '%s\n' "${GRN}==>${RST} $*"; }
warn() { printf '%s\n' "${YEL}warning:${RST} $*" >&2; }
die()  { printf '%s\n' "${RED}error:${RST} $*" >&2; exit 1; }

# --- 1. First run: create .env and stop so the user can fill in domains -------
if [[ ! -f .env ]]; then
  cp .env.example .env
  info "Created .env from .env.example."
  printf '%s\n' "${BLD}Edit .env and set NEO_SERVER_NAME and the *_HOST values, then re-run this script.${RST}"
  exit 0
fi

set -a; # shellcheck disable=SC1091
source .env; set +a

# --- 2. Sanity-check required values -----------------------------------------
[[ -n "${NEO_SERVER_NAME:-}" && "$NEO_SERVER_NAME" != "example.com" ]] \
  || die "Set NEO_SERVER_NAME in .env (it is immutable after the first start)."
[[ -n "${NEO_MATRIX_HOST:-}" && "$NEO_MATRIX_HOST" != "matrix.example.com" ]] \
  || die "Set NEO_MATRIX_HOST in .env."

case ",${COMPOSE_PROFILES:-}," in
  *,coturn,*) [[ -n "${PUBLIC_IP:-}" ]] \
    || die "The coturn profile is enabled but PUBLIC_IP is empty — TURN relay needs it." ;;
esac

# Is Matrix Authentication Service enabled? It changes how the auth config renders.
MAS_ON=false
case ",${COMPOSE_PROFILES:-}," in *,mas,*) MAS_ON=true ;; esac
if [[ "$MAS_ON" == true ]]; then
  [[ -n "${NEO_AUTH_HOST:-}" && "$NEO_AUTH_HOST" != "auth.example.com" ]] \
    || die "The mas profile is enabled but NEO_AUTH_HOST is unset — set it in .env."
  command -v python3 >/dev/null \
    || die "python3 is required to configure MAS."
  python3 -c 'import yaml' 2>/dev/null \
    || die "python3 PyYAML is required to configure MAS (e.g. 'pip install pyyaml')."
fi

# Workers profile: offload outbound federation to the neo-fedsender worker.
WORKERS_ON=false
case ",${COMPOSE_PROFILES:-}," in *,workers,*) WORKERS_ON=true ;; esac

warn "NEO_SERVER_NAME is '${NEO_SERVER_NAME}'. This is permanent — it can never be changed once a user or room exists."

# --- 3. Verify the external proxy network exists (NPM owns it) ----------------
PROXY_NET="${NEO_PROXY_NETWORK:-nginxproxymanager_default}"
if ! docker network inspect "$PROXY_NET" >/dev/null 2>&1; then
  die "Docker network '$PROXY_NET' not found. Start nginx proxy manager first, or set NEO_PROXY_NETWORK in .env to the network from 'docker network ls'."
fi

# --- 4. Generate any missing secrets, writing back into .env ------------------
ensure_secret() {
  local key="$1" bytes="$2" current
  current="$(grep -E "^${key}=" .env | head -n1 | cut -d= -f2-)"
  if [[ -z "$current" ]]; then
    local value; value="$(openssl rand -hex "$bytes")"
    # hex output is sed-delimiter-safe; replace the empty assignment in place.
    sed -i "s|^${key}=.*|${key}=${value}|" .env
    info "Generated ${key}."
  fi
}
ensure_secret POSTGRES_PASSWORD 32
ensure_secret SYNAPSE_REGISTRATION_SHARED_SECRET 32
ensure_secret SYNAPSE_MACAROON_SECRET 32
ensure_secret SYNAPSE_FORM_SECRET 32
ensure_secret COTURN_STATIC_AUTH_SECRET 32
ensure_secret GRAFANA_ADMIN_PASSWORD 16
ensure_secret MAS_MATRIX_SECRET 32

# Re-load so freshly generated secrets are available to envsubst below.
set -a; # shellcheck disable=SC1091
source .env; set +a

# --- 5. Render templates (explicit allowlist so literal $ in configs survives) -
# Single quotes are intentional: envsubst must receive the literal variable names.
# shellcheck disable=SC2016
ALLOW='${NEO_SERVER_NAME} ${NEO_MATRIX_HOST} ${NEO_TURN_HOST} ${NEO_AUTH_HOST} ${POSTGRES_PASSWORD} ${SYNAPSE_REGISTRATION_SHARED_SECRET} ${SYNAPSE_MACAROON_SECRET} ${SYNAPSE_FORM_SECRET} ${SYNAPSE_MAX_UPLOAD_SIZE} ${COTURN_STATIC_AUTH_SECRET} ${COTURN_MIN_PORT} ${COTURN_MAX_PORT} ${PUBLIC_IP} ${MAS_MATRIX_SECRET}'

render() {
  local src="$1" dst="${1%.template}"
  envsubst "$ALLOW" < "$src" > "$dst"
  info "Rendered ${dst}."
}
render_to() { envsubst "$ALLOW" < "$1" > "$2"; info "Rendered $2."; }

render config/synapse/homeserver.yaml.template
render config/synapse/log.config.template
render config/wellknown/server.json.template
render config/element/config.json.template
render config/coturn/turnserver.conf.template
render config/monitoring/prometheus.yml.template

# Auth mode: append the matching fragment to homeserver.yaml (keeps YAML keys
# unique) and render the matching client well-known.
if [[ "$MAS_ON" == true ]]; then
  envsubst "$ALLOW" < config/synapse/auth.mas.yaml.template >> config/synapse/homeserver.yaml
  render_to config/wellknown/client.mas.json.template config/wellknown/client.json
  info "Auth mode: MAS (delegated)."
else
  envsubst "$ALLOW" < config/synapse/auth.local.yaml.template >> config/synapse/homeserver.yaml
  render_to config/wellknown/client.json.template config/wellknown/client.json
  info "Auth mode: built-in Synapse."
fi

# Workers: append the redis/sender block to homeserver.yaml and render the worker
# config. Only when the profile is on, so the main process never enables Redis
# without the redis container present.
if [[ "$WORKERS_ON" == true ]]; then
  envsubst "$ALLOW" < config/synapse/workers.yaml.template >> config/synapse/homeserver.yaml
  render_to config/synapse/worker-fedsender.yaml.template config/synapse/worker-fedsender.yaml
  render_to config/synapse/worker-fedreader.yaml.template config/synapse/worker-fedreader.yaml
  render_to config/synapse/worker-synchrotron.yaml.template config/synapse/worker-synchrotron.yaml
  info "Workers: federation sender + reader + sync enabled."
fi

# TURN over TLS: append cert lines only when a cert path is configured.
if [[ -n "${COTURN_TLS_CERT:-}" && -n "${COTURN_TLS_KEY:-}" ]]; then
  {
    echo "cert=${COTURN_TLS_CERT}"
    echo "pkey=${COTURN_TLS_KEY}"
  } >> config/coturn/turnserver.conf
  info "Configured Coturn TLS (turns://)."
else
  warn "No Coturn TLS cert set — turns:// (5349) will not work. Set COTURN_TLS_CERT/KEY for TLS."
fi

# --- 6. Generate the Synapse signing key once (regenerating breaks federation) -
mkdir -p data/synapse/media
if [[ ! -s data/synapse/signing.key ]]; then
  info "Generating Synapse signing key..."
  docker run --rm \
    -v "$PWD/data/synapse:/data" \
    --user "${NEO_PUID:-1000}:${NEO_PGID:-1000}" \
    --entrypoint generate_signing_key \
    "ghcr.io/element-hq/synapse:${SYNAPSE_IMAGE_TAG}" \
    -o /data/signing.key
else
  info "Signing key already exists — leaving it untouched."
fi

# --- 6b. MAS config: generate once, then patch our topology into it -----------
if [[ "$MAS_ON" == true ]]; then
  mkdir -p config/mas
  if [[ ! -s config/mas/config.yaml ]]; then
    info "Generating MAS config (secrets + keys)..."
    # `config generate` writes the config to stdout; logs go to stderr.
    docker run --rm "ghcr.io/element-hq/matrix-authentication-service:${MAS_IMAGE_TAG}" \
      config generate > config/mas/config.yaml
    info "Patching MAS config for this deployment..."
    NEO_AUTH_HOST="$NEO_AUTH_HOST" NEO_SERVER_NAME="$NEO_SERVER_NAME" \
      POSTGRES_PASSWORD="$POSTGRES_PASSWORD" MAS_MATRIX_SECRET="$MAS_MATRIX_SECRET" \
      python3 scripts/patch-mas-config.py config/mas/config.yaml
  else
    info "MAS config already exists — leaving it untouched."
  fi
fi

# --- 7. Print the DNS records and NPM proxy hosts to create -------------------
# Apex well-known: served here by neo-wellknown, or externally (e.g. CF Worker).
if [[ "${NEO_WELLKNOWN_EXTERNAL:-false}" == true ]]; then
  apex_dns="${NEO_SERVER_NAME}     -> well-known served externally (see cloudflare/)"
  apex_npm="   (apex proxy host not needed — well-known is served at the edge)"
else
  apex_dns="${NEO_SERVER_NAME}     -> this host   (orange-cloud — serves /.well-known)"
  apex_npm="   ${NEO_SERVER_NAME}  -> neo-wellknown:80    (custom location: only /.well-known/matrix/)"
fi

mas_dns=""; mas_npm=""; mas_route=""
if [[ "$MAS_ON" == true ]]; then
  mas_dns="
   ${NEO_AUTH_HOST}    -> this host   (orange-cloud — MAS)"
  mas_npm="
   ${NEO_AUTH_HOST}    -> 127.0.0.1:${NEO_PORT_MAS:-8802}"
  mas_route="
   ${BLD}MAS routing on ${NEO_MATRIX_HOST}${RST}: add an Advanced custom location, ordered
   BEFORE the catch-all, so login/logout/refresh go to MAS:
       location ~ ^/_matrix/client/(.*)/(login|logout|refresh) { proxy_pass http://127.0.0.1:${NEO_PORT_MAS:-8802}; }"
fi

workers_route=""
if [[ "$WORKERS_ON" == true ]]; then
  workers_route="
   ${BLD}Worker routing on ${NEO_MATRIX_HOST}${RST}: add these Advanced custom locations
   (after the MAS rule if present — regex first-match wins, all beat the default forward):
       location ~ ^/_matrix/client/(r0|v3)/sync\$                        { proxy_pass http://127.0.0.1:${NEO_PORT_SYNCHROTRON:-8807}; }
       location ~ ^/_matrix/client/(api/v1|r0|v3)/(events|initialSync)\$ { proxy_pass http://127.0.0.1:${NEO_PORT_SYNCHROTRON:-8807}; }
       location ~ ^/_matrix/client/(api/v1|r0|v3|unstable)/rooms/.*/(messages|context|members|state)\$ { proxy_pass http://127.0.0.1:${NEO_PORT_SYNCHROTRON:-8807}; }
       location ~ ^/_matrix/client/(r0|v3|unstable)/keys/query\$         { proxy_pass http://127.0.0.1:${NEO_PORT_SYNCHROTRON:-8807}; }
       location ~ ^/_matrix/federation/                                 { proxy_pass http://127.0.0.1:${NEO_PORT_FEDREADER:-8806}; }
   Add proxy_set_header Host \$host / X-Forwarded-For / X-Forwarded-Proto \$scheme to each."
fi

cat <<EOF

${BLD}Bootstrap complete.${RST} Next steps:

${BLD}1. DNS records${RST} (Cloudflare):
   ${NEO_MATRIX_HOST}     -> this host   (orange-cloud / proxied)
   ${apex_dns}
   ${NEO_ELEMENT_HOST}    -> this host   (orange-cloud)
   ${NEO_ADMIN_HOST}      -> this host   (orange-cloud)
   ${NEO_GRAFANA_HOST}    -> this host   (orange-cloud)${mas_dns}
   ${NEO_TURN_HOST:-turn.$NEO_SERVER_NAME}  -> ${PUBLIC_IP:-<PUBLIC_IP>}  (${BLD}grey-cloud / DNS-only${RST} — CF cannot proxy UDP)

${BLD}2. nginx proxy manager hosts${RST} (Forward scheme http, Forward Hostname 127.0.0.1):
   ${NEO_MATRIX_HOST}  -> 127.0.0.1:${NEO_PORT_SYNAPSE:-8801}    (proxy the whole /_matrix prefix; set client_max_body_size ${SYNAPSE_MAX_UPLOAD_SIZE})
${apex_npm}
   ${NEO_ELEMENT_HOST} -> 127.0.0.1:${NEO_PORT_ELEMENT:-8803}
   ${NEO_ADMIN_HOST}   -> 127.0.0.1:${NEO_PORT_ADMIN:-8804}
   ${NEO_GRAFANA_HOST} -> 127.0.0.1:${NEO_PORT_GRAFANA:-8805}${mas_npm}${mas_route}${workers_route}
   (host-mode NPM forwards to loopback ports; if your NPM shares Neo's Docker
    network instead, use the container names neo-synapse:8008, neo-mas:8080, etc.)

${BLD}3. Start the stack:${RST}
   docker compose up -d

${BLD}4. Create the first admin user:${RST}
   ./scripts/register-user.sh --admin
EOF
