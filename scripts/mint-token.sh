#!/usr/bin/env bash
# Mint a registration token so someone can sign up. Usage: ./scripts/mint-token.sh [uses]
# (default 1 use). Routes to MAS when the `mas` profile is on, otherwise Synapse.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
source .env; set +a

USES="${1:-1}"

if [[ ",${COMPOSE_PROFILES:-}," == *,mas,* ]]; then
  exec docker compose exec mas \
    mas-cli manage issue-user-registration-token -c /config/config.yaml --usage-limit "$USES"
fi

# Synapse path: needs an admin's access token (Element > Settings > Help & About
# > Advanced > Access Token).
: "${ADMIN_TOKEN:?Set ADMIN_TOKEN to an admin access token}"
PROXY_NET="${NEO_PROXY_NETWORK:-nginxproxymanager_default}"
docker run --rm --network "$PROXY_NET" curlimages/curl:latest \
  -sS -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"uses_allowed\": ${USES}}" \
  http://neo-synapse:8008/_synapse/admin/v1/registration_tokens/new
echo
