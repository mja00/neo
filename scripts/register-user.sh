#!/usr/bin/env bash
# Create a Matrix user. Use this to bootstrap the FIRST admin (pass --admin).
# Routes to MAS when the `mas` profile is on, otherwise Synapse's shared-secret
# registration. Extra flags pass through to the underlying tool.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; # shellcheck disable=SC1091
source .env 2>/dev/null || true; set +a

if [[ ",${COMPOSE_PROFILES:-}," == *,mas,* ]]; then
  # MAS interactively prompts for attributes unless --yes. -a = admin.
  exec docker compose exec mas \
    mas-cli manage register-user -c /config/config.yaml "$@"
fi

exec docker compose exec synapse \
  register_new_matrix_user -c /data/homeserver.yaml "$@" http://localhost:8008
