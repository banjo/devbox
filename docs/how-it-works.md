# How it works

- State lives in SQLite at `~/.local/state/devbox/devbox.db`. Hetzner is the source of truth for
  which servers exist; `up`, `status` and `down` reconcile against servers labeled
  `managed-by=devbox` and this install's `devbox-controller` label.
- `status` is scoped to this controller and says so (`no devbox servers for controller ID`). Other
  controllers' devbox servers in the same Hetzner project are listed separately, read-only
  (`other_controllers` in `--json`); they are never adopted, cleaned up or changed.
- Every resource devbox creates (servers, SSH key, firewall) carries those labels. A resource with
  the expected name but without both labels is never modified; devbox stops with an error instead.
- Hetzner often sells out of server types in some locations, so the server is chosen per `up` rather
  than fixed in config. `devbox server-types` lists what is available now (filters: `--min-cores`,
  `--min-memory`, `--arch`, `--location`, `--zone`, `--all`). Without `--type`/`--location`, `up`
  uses the config defaults, or else the type and location of the project's last server. If that
  choice is sold out (or can't hold the snapshot being restored), `up` stops and suggests the
  cheapest comparable alternatives, same region first. Availability is Hetzner's indicator, not a
  guarantee.
- One server at a time. `up --force` allows a second one.
- SSH host keys are pinned. For every new server (fresh or restored from a snapshot) devbox
  generates an ed25519 host key locally and hands it to cloud-init, so it knows the server's key
  before the first connection and only ever accepts that one (`StrictHostKeyChecking=yes`). Until
  cloud-init has installed it, `up` keeps waiting rather than trust another key. The private host
  key passes through Hetzner's user data. Servers created by older versions keep trust-on-first-use
  until they are replaced.
- Servers run Ubuntu with Docker + Compose, `mise`, `cloudflared`, `git` and build tools. Everything
  runs as root in `/srv/app`.
- Sync sends exactly the files Git sees in the working tree: tracked files plus untracked files that
  are not ignored, using Git's own ignore rules (so `!.env.example` style exceptions work). Ignored
  files on the server, such as `node_modules` and `.env`, are left alone. A file that was synced
  before and has since been deleted locally is deleted on the server too. Outside a Git repo,
  everything except `.git` is copied and nothing is deleted. Submodule contents are not synced.
- Background processes are systemd units named `devbox-<name>`. They survive SSH disconnects; logs
  are in the journal.
- `exec --timeout S` is enforced on the server: the command runs in its own systemd scope with
  `RuntimeMaxSec`, so after S seconds systemd sends SIGTERM to every process in it and SIGKILL 10
  seconds later, even if the SSH connection is gone. `exec` checks the scope's result on the server
  and then exits with 124 (`timed_out: true` with `--json`); a real SSH failure is still reported as
  one. Containers started in the background (`docker compose up -d`, `docker run -d`) belong to
  Docker, not the scope, and keep running. Without `--timeout`, a foreground command keeps running
  on the server if the connection drops.
- `down` deletes the server. Anything that exists only there (database contents, unsynced files) is
  gone.

Previews are described in [Previews](previews.md).

## Startup

- `up` runs every step under one deadline: creating (or powering on) the server, waiting for SSH,
  first-boot setup (cloud-init), checking that Docker, Compose, mise, cloudflared, rsync, git and
  python3 are installed, syncing code and writing secrets. The default is `startup_timeout = "5m"`;
  change it in the config, per project with `devbox project set NAME --startup-timeout 10m`, or per
  run with `up --startup-timeout`. Every network call and wait uses the time that's left; nothing
  extends it.
- Progress is printed to stderr as `[mm:ss] ...` lines, including the last line of setup output
  while cloud-init runs (with secrets masked). With `--json`, stdout only has the final JSON.
- If cloud-init reports an error or tools are missing, `up` fails right away instead of waiting.
- On failure, `up` exits 1 (124 for a timeout, 130 for Ctrl-C) and prints the stage, how long it
  took, the server id and IP, that the server is kept and still billed, where the diagnostics file
  is (`~/.local/state/devbox/diagnostics/`: cloud-init status, the last 60 lines of setup output and
  failed units, secrets masked), and the commands to retry or delete. With `--json` the same
  information is in the error object (`code`, `stage`, `elapsed_seconds`, `limit_seconds`,
  `server_id`, `server_retained`, `billable`, `diagnostics`, `cleanup`, ...).
- The server is recorded as "setup failed at <stage>" (`status` shows it). Running `up` again
  continues setup on the same server; it never creates a second one.
- `--cleanup-on-failure` deletes the server when startup fails, but only a fresh server created by
  that run (never a reused one or one restored from a snapshot, and never a snapshot). devbox checks
  that Hetzner no longer lists it before saying it's gone.
- If the create request itself times out, the server may still appear at Hetzner later;
  `devbox status` picks it up.
- On the server, package downloads use short timeouts (20 s per connection, 2 retries) so a stalled
  mirror fails instead of hanging.
