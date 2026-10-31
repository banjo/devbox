# TODO

## Verify on a real server

Nothing below has run against Hetzner yet. Check each step and fix what breaks.

- [ ] `devbox server-types` against the live API: the `available`/`recommended` fields are present in the installed `hcloud` version, prices and network zones look right
- [ ] `devbox up` end to end: SSH key upload, firewall creation, server creation, cloud-init finishing (Docker, mise, cloudflared)
- [ ] `up` on a sold-out type gives a useful error, both from our availability check and from Hetzner's own create error
- [ ] An ARM type (`cax*`): cloud-init installs the arm64 cloudflared package, `ubuntu-24.04` resolves to the arm image
- [ ] `sync`: Git file list (incl. `!.env.example` negations), deletions of locally removed files, `node_modules` and `.env` on the server left alone
- [ ] `exec`, `exec --detach`, `ps`, `logs -f`, `stop` with a real dev server (systemd units, `HOME`, mise shims on `PATH`)
- [ ] `exec --timeout`: the scope is stopped on the server, exit code 124, no leftover processes (re-run the check from issue #2)
- [ ] Costs: prices stored on `up` match the Hetzner console; `status`, `down` and `costs` totals look right after a real session
- [ ] `expose`: quick tunnel URL is found in the journal and loads
- [ ] `status` and `down`; reconcile after deleting a server by hand in the Hetzner console
- [ ] Firewall: SSH works from this machine, nothing else is reachable; behavior when this machine's IP changes; old `devbox` firewall/key from earlier versions are left untouched
- [ ] Full agent run with the skill: a project with no notes goes from `up` to a working preview, the agent saves notes, then `down` and `up` again work from the notes alone

## Next

- [ ] Snapshots (migration 3): `down --snapshot`, restore from the latest snapshot on `up`
- [ ] Database backups to S3
- [ ] Fixed preview hostname with a named Cloudflare tunnel, optionally behind Cloudflare Access
- [ ] Automatic shutdown of idle servers
- [ ] Warning for long-running servers and a monthly budget threshold
- [ ] Include primary IPv4 and snapshot storage in cost estimates
- [ ] Pull changes made on the server back to the local copy (`devbox pull`)
- [ ] Run as a non-root user on the server and pin SSH host keys instead of `accept-new`
- [ ] Remember the last server type/location per project and suggest it first

## Known limitations

- Previews are public: anyone with the URL can open them.
- The preview URL changes every time the tunnel restarts.
- `down` loses everything that exists only on the server (database contents, files not synced back).
- Availability from Hetzner is an indicator, not a guarantee; `up` can still fail if a type sells out between listing and creating.
- One server at a time unless `up --force`.
- Costs are estimates of the server price only. Servers deleted outside devbox are recorded as deleted when devbox next notices, which can overstate their cost.
- Submodule contents are not synced.
- `exec --timeout` does not stop containers started in the background (`-d`).
