#!/usr/bin/env python3
"""Patch a freshly generated MAS config for this deployment.

`mas-cli config generate` produces secrets + keys we want to keep, plus default
http/database/matrix sections we must replace with our topology. We overwrite
only those sections and leave everything else (notably `secrets`) untouched.
"""

import os
import sys

import yaml

path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f)

auth_host = os.environ["NEO_AUTH_HOST"]
server_name = os.environ["NEO_SERVER_NAME"]
pg_password = os.environ["POSTGRES_PASSWORD"]
matrix_secret = os.environ["MAS_MATRIX_SECRET"]
# Comma-separated localparts granted the MAS admin scope; drives Ketesa's MAS panel.
mas_admins = [
    u.strip() for u in os.environ.get("NEO_MAS_ADMIN_USERS", "").split(",") if u.strip()
]

cfg["http"] = {
    "public_base": f"https://{auth_host}/",
    "listeners": [
        {
            "name": "web",
            # adminapi backs Ketesa's MAS-native management (users, sessions, tokens).
            "resources": [
                {"name": n}
                for n in (
                    "discovery",
                    "human",
                    "oauth",
                    "compat",
                    "graphql",
                    "assets",
                    "adminapi",
                )
            ],
            "binds": [{"host": "0.0.0.0", "port": 8080}],
        }
    ],
    # Trust forwarded headers from the reverse proxy / docker networks.
    "trusted_proxies": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1/8"],
}

# MAS reuses the `synapse` Postgres role against its own `mas` database.
cfg["database"] = {"uri": f"postgresql://synapse:{pg_password}@postgres/mas"}

cfg["matrix"] = {
    "kind": "synapse",
    "homeserver": server_name,
    "endpoint": "http://synapse:8008",
    "secret": matrix_secret,
}

# Keep legacy password login working, and allow token-gated self-service signup.
cfg.setdefault("passwords", {})["enabled"] = True
account = cfg.setdefault("account", {})
account["password_registration_enabled"] = True
account["registration_token_required"] = True

# Grant the admin scope to named localparts; merged into policy.data so the
# generated default policy (its wasm ref, if any) stays intact.
if mas_admins:
    cfg.setdefault("policy", {}).setdefault("data", {})["admin_users"] = mas_admins

with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
