#!/usr/bin/env bash
# Issue a Synapse-admin access token for a user (MAS only). Needed because MAS
# password/compat login sessions cannot use the Synapse admin API — Synapse Admin
# (Ketesa) needs a token minted this way.
#
# Usage: ./scripts/mas-admin-token.sh <username>
set -euo pipefail

cd "$(dirname "$0")/.."

USER="${1:?Usage: mas-admin-token.sh <username>}"

exec docker compose exec mas \
  mas-cli manage issue-compatibility-token -c /config/config.yaml \
  --yes-i-want-to-grant-synapse-admin-privileges "$USER"
