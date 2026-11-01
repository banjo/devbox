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
  random URL anyway. The tunnel and DNS records stay when the server goes; delete them in the
  Cloudflare dashboard if you drop the project.
- `--protect` asks for a password (HTTP basic auth, user `devbox`): a Caddy proxy in Docker sits
  between the tunnel and the app. The password is per project, kept in `preview-auth/<project>.json`
  in the state folder and printed by `expose`. For sign-in with Cloudflare Access instead, add an
  Access application for the stable hostname in the Cloudflare dashboard; the hostname doesn't
  change, so it keeps working.
- `--wait-for /health` polls the app on the server until the path answers 2xx or 3xx
  (`--wait-timeout`, default 2m) and only then opens the preview. Otherwise `expose` fails with the
  last status and opens nothing.
