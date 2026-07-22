#!/usr/bin/env bash
# Create a Matrix user via the shared secret (bypasses registration tokens).
# Use this to bootstrap the FIRST admin. Prompts for username, password, admin.
# Pass -a to force admin, or any register_new_matrix_user flags.
set -euo pipefail

cd "$(dirname "$0")/.."

exec docker compose exec synapse \
  register_new_matrix_user -c /data/homeserver.yaml "$@" http://localhost:8008
