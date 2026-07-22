#!/usr/bin/env bash
# Mint a registration token so someone can sign up. Requires an admin's access
# token (Element: Settings > Help & About > Advanced > Access Token).
#
# Usage: ADMIN_TOKEN=syt_xxx ./scripts/mint-token.sh [uses_allowed]
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
source .env; set +a

: "${ADMIN_TOKEN:?Set ADMIN_TOKEN to an admin access token}"
USES="${1:-1}"
PROXY_NET="${NEO_PROXY_NETWORK:-nginxproxymanager_default}"

# Hit Synapse over the proxy network by name — no public DNS dependency.
docker run --rm --network "$PROXY_NET" curlimages/curl:latest \
  -sS -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"uses_allowed\": ${USES}}" \
  http://neo-synapse:8008/_synapse/admin/v1/registration_tokens/new
echo
