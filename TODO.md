# TODO

## Verify on a real server

Nothing below has run against Hetzner yet. Check each step and fix what breaks.

- [ ] `devbox server-types` against the live API: the `available`/`recommended` fields are present in the installed `hcloud` version, prices and network zones look right
- [ ] `devbox up` end to end: SSH key upload, firewall creation, server creation, cloud-init finishing (Docker, mise, cloudflared)
- [ ] `up` on a sold-out type gives a useful error, both from our availability check and from Hetzner's own create error
- [ ] An ARM type (`cax*`): cloud-init installs the arm64 cloudflared package, `ubuntu-24.04` resolves to the arm image
- [ ] `sync`: `.gitignore` filtering, `--delete` leaves `node_modules` and `.env` on the server alone
- [ ] `exec`, `exec --detach`, `ps`, `logs -f`, `stop` with a real dev server (systemd units, `HOME`, mise shims on `PATH`)
- [ ] `expose`: quick tunnel URL is found in the journal and loads
- [ ] `status` and `down`; reconcile after deleting a server by hand in the Hetzner console
- [ ] Firewall: SSH works from this machine, nothing else is reachable; behavior when this machine's IP changes
- [ ] Full agent run with the skill: a project with no notes goes from `up` to a working preview, the agent saves notes, then `down` and `up` again work from the notes alone

## Next

- [ ] Snapshots (migration 2): `down --snapshot`, restore from the latest snapshot on `up`
- [ ] Database backups to S3
- [ ] Fixed preview hostname with a named Cloudflare tunnel, optionally behind Cloudflare Access
- [ ] Automatic shutdown of idle servers
- [ ] Cost so far in `status` (price × uptime) and a warning for long-running servers
- [ ] Pull changes made on the server back to the local copy (`devbox pull`)
- [ ] Run as a non-root user on the server and pin SSH host keys instead of `accept-new`
- [ ] Remember the last server type/location per project and suggest it first

## Known limitations

- Previews are public: anyone with the URL can open them.
- The preview URL changes every time the tunnel restarts.
- `down` loses everything that exists only on the server (database contents, files not synced back).
- Availability from Hetzner is an indicator, not a guarantee; `up` can still fail if a type sells out between listing and creating.
- One server at a time unless `up --force`.
