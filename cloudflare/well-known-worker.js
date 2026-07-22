// Serves the Matrix .well-known delegation files at the Cloudflare edge, so your
// apex domain can point clients/federation at the homeserver WITHOUT touching the
// site already hosted there. Scoped to /.well-known/matrix/* via the route in
// wrangler.toml — nothing else on the domain is affected. The files are static
// JSON, so this returns them directly (no proxying, no dependency on the host).
//
// Hostnames come from wrangler.toml [vars]. Deploy: see cloudflare/README.md.

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    const headers = {
      "content-type": "application/json",
      // Required so browser clients on another origin can read the client file.
      "access-control-allow-origin": "*",
    };

    if (pathname === "/.well-known/matrix/server") {
      // Delegates federation to the homeserver on 443 (no port 8448 needed).
      return Response.json({ "m.server": `${env.MATRIX_HOST}:443` }, { headers });
    }

    if (pathname === "/.well-known/matrix/client") {
      const body = { "m.homeserver": { "base_url": `https://${env.MATRIX_HOST}` } };
      // Advertise MAS only when an auth host is configured (mas profile).
      if (env.AUTH_HOST) {
        body["org.matrix.msc2965.authentication"] = {
          issuer: `https://${env.AUTH_HOST}/`,
          account: `https://${env.AUTH_HOST}/account`,
        };
      }
      return Response.json(body, { headers });
    }

    return new Response("not found", { status: 404 });
  },
};
