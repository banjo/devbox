# Previews

- Previews use a Cloudflare tunnel, so no ports are opened. Without further config each preview gets
  a public, random `*.trycloudflare.com` URL that changes whenever the tunnel restarts; no
  Cloudflare account is needed.
- Several previews can run side by side: `expose NAME 5173 --name web` and
  `expose NAME 3000 --name api`. Without `--name` the preview is called `default`.
  `unexpose NAME [PREVIEW...]` closes them (all without names). `status` lists them and flags
  previews whose tunnel no longer runs (after a restore or power-on); run `expose` again.
- Stable URLs: set `preview_domain = "example.com"` (a zone in your Cloudflare account) and a
  Cloudflare API token with Account › Cloudflare Tunnel › Edit and Zone › DNS › Edit
  (`cloudflare_token_file`, or `CLOUDFLARE_API_TOKEN`). devbox then creates one named tunnel per
  project (`devbox-<controller>-<project>`), reused by every server of that project, and a proxied
  CNAME per preview: `<project>.example.com` for the default preview, `<project>-<name>.example.com`
  for named ones. It never replaces a DNS record it didn't create. Cloudflare's free certificate
  covers one level of subdomain, so use the zone itself as `preview_domain`. `--quick` gives a
  random URL anyway. The tunnel and DNS records stay when the server goes; after `project remove`,
  `devbox cleanup` deletes them.
- `--protect` asks for a password (HTTP basic auth, user `devbox`): a Caddy proxy in Docker sits
  between the tunnel and the app. The password is per project, kept in `preview-auth/<project>.json`
  in the state folder and printed by `expose`. For sign-in with Cloudflare Access instead, add an
  Access application for the stable hostname in the Cloudflare dashboard; the hostname doesn't
  change, so it keeps working.
- `--wait-for /health` polls the app on the server until the path answers 2xx or 3xx
  (`--wait-timeout`, default 2m) and only then opens the preview. Otherwise `expose` fails with the
  last status and opens nothing.
- The app can read its preview URL from the environment. devbox keeps
  `/etc/profile.d/devbox-preview.sh` on the server up to date (on `up`, `expose`, `unexpose` and
  when a preview's process is stopped), and every `exec`, background process and `ssh` session loads
  it:
  - `DEVBOX_PREVIEW_URL` and `DEVBOX_PREVIEW_HOST`: the default preview. With `preview_domain` they
    are set from `up` on, before anything is exposed, so the app can start with the right values.
    Without it they are set once `expose` has a quick tunnel URL, and change with it.
  - `DEVBOX_PREVIEW_URL_<NAME>` and `DEVBOX_PREVIEW_HOST_<NAME>` for each named preview that is
    exposed (`--name my-api` gives `DEVBOX_PREVIEW_URL_MY_API`).
  - `DEVBOX_PREVIEW_DOMAIN`: the `preview_domain`, to work out other stable hostnames.

  Processes only see the values they started with: start the app after `up` (stable URLs), or
  restart it after `expose` (quick tunnels). `up --json` and `expose --json` list them under
  `preview_env`.

## Recipes

Vite only answers host names it knows. Allow the preview's instead of every host:

```js
// vite.config.js
export default {
  server: {
    host: true,
    allowedHosts: [process.env.DEVBOX_PREVIEW_HOST].filter(Boolean),
  },
}
```

Frontend, API and WebSocket on one URL: expose only the frontend and let the dev server proxy the
rest, so the browser never needs a second hostname (and `--protect` covers everything):

```js
server: {
  proxy: {
    '/api': 'http://localhost:3000',
    '/socket': { target: 'ws://localhost:3000', ws: true },
  },
}
```

Docker Compose passes the values on when they are listed:

```yaml
services:
  web:
    environment:
      PUBLIC_URL: ${DEVBOX_PREVIEW_URL:-http://localhost:3000}
```
