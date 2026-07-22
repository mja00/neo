# Cloudflare well-known Worker

Serves `/.well-known/matrix/{server,client}` on your **apex** domain from the
Cloudflare edge. Use this when the apex is hosted elsewhere (a different NPM,
another server, a static site) and can't serve the delegation files itself.

This keeps your Matrix name as the bare apex (e.g. `@you:mart.fyi`) while the
homeserver runs at `matrix.<apex>`. It touches nothing but the two well-known
paths — the existing site is untouched.

## Configure

Edit `wrangler.toml`:
- `routes[].pattern` / `zone_name` — your apex (must be a zone in the CF account you deploy with).
- `MATRIX_HOST` — matches `NEO_MATRIX_HOST` in the Neo `.env`.
- `AUTH_HOST` — matches `NEO_AUTH_HOST` (only when the `mas` profile is on; remove otherwise).

## Deploy

```sh
cd cloudflare
npx wrangler login      # one-time, opens a browser
npx wrangler deploy
```

## Verify

```sh
curl https://mart.fyi/.well-known/matrix/server
# {"m.server":"matrix.mart.fyi:443"}
curl https://mart.fyi/.well-known/matrix/client
# {"m.homeserver":{"base_url":"https://matrix.mart.fyi"}, ...}
```

Then set `NEO_WELLKNOWN_EXTERNAL=true` in the Neo `.env` so bootstrap stops
prompting you to add an apex proxy host (the `neo-wellknown` container goes unused
and can be ignored). Finally confirm delegation with the
[Matrix Federation Tester](https://federationtester.matrix.org/).
