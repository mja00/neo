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

# Re-load so freshly generated secrets are available to envsubst below.
set -a; # shellcheck disable=SC1091
source .env; set +a

# --- 5. Render templates (explicit allowlist so literal $ in configs survives) -
# Single quotes are intentional: envsubst must receive the literal variable names.
# shellcheck disable=SC2016
ALLOW='${NEO_SERVER_NAME} ${NEO_MATRIX_HOST} ${NEO_TURN_HOST} ${POSTGRES_PASSWORD} ${SYNAPSE_REGISTRATION_SHARED_SECRET} ${SYNAPSE_MACAROON_SECRET} ${SYNAPSE_FORM_SECRET} ${SYNAPSE_MAX_UPLOAD_SIZE} ${COTURN_STATIC_AUTH_SECRET} ${COTURN_MIN_PORT} ${COTURN_MAX_PORT} ${PUBLIC_IP}'

render() {
  local src="$1" dst="${1%.template}"
  envsubst "$ALLOW" < "$src" > "$dst"
  info "Rendered ${dst}."
}
render config/synapse/homeserver.yaml.template
render config/synapse/log.config.template
render config/wellknown/server.json.template
render config/wellknown/client.json.template
render config/element/config.json.template
render config/coturn/turnserver.conf.template
render config/monitoring/prometheus.yml.template

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

# --- 7. Print the DNS records and NPM proxy hosts to create -------------------
cat <<EOF

${BLD}Bootstrap complete.${RST} Next steps:

${BLD}1. DNS records${RST} (Cloudflare):
   ${NEO_MATRIX_HOST}     -> this host   (orange-cloud / proxied)
   ${NEO_SERVER_NAME}     -> this host   (orange-cloud — serves /.well-known)
   ${NEO_ELEMENT_HOST}    -> this host   (orange-cloud)
   ${NEO_ADMIN_HOST}      -> this host   (orange-cloud)
   ${NEO_GRAFANA_HOST}    -> this host   (orange-cloud)
   ${NEO_TURN_HOST:-turn.$NEO_SERVER_NAME}  -> ${PUBLIC_IP:-<PUBLIC_IP>}  (${BLD}grey-cloud / DNS-only${RST} — CF cannot proxy UDP)

${BLD}2. nginx proxy manager hosts${RST} (Forward scheme http, over the '${PROXY_NET}' network):
   ${NEO_MATRIX_HOST}  -> neo-synapse:8008    (proxy the whole /_matrix prefix; set client_max_body_size ${SYNAPSE_MAX_UPLOAD_SIZE})
   ${NEO_SERVER_NAME}  -> neo-wellknown:80    (custom location: only /.well-known/matrix/)
   ${NEO_ELEMENT_HOST} -> neo-element:80
   ${NEO_ADMIN_HOST}   -> neo-synapse-admin:80
   ${NEO_GRAFANA_HOST} -> neo-grafana:3000

${BLD}3. Start the stack:${RST}
   docker compose up -d

${BLD}4. Create the first admin user:${RST}
   ./scripts/register-user.sh
EOF
