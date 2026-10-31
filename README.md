# devbox

Temporary Hetzner development servers, driven from the Hermes host. Code is edited locally, copied to a disposable server, and run there. The CLI manages the server; the agent (see [the skill](skills/hetzner-devbox/SKILL.md)) works out how to run each project and keeps notes on it.

Status: proof of concept. See [PLAN.md](PLAN.md).

## Setup

On the Hermes host:

1. Install the tools: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), [`hcloud`](https://github.com/hetznercloud/cli), `ssh` and `rsync`.
2. Create a dedicated Hetzner Cloud project and an API token with read/write access. Save it:

   ```sh
   mkdir -p ~/.config/devbox
   install -m 600 /dev/null ~/.config/devbox/hcloud.token
   $EDITOR ~/.config/devbox/hcloud.token
   ```

3. Write `~/.config/devbox/config.toml`. Every key is optional except the token file; the defaults are shown in [`examples/config.toml`](examples/config.toml).

   ```toml
   hcloud_token_file = "~/.config/devbox/hcloud.token"
   ```

4. Install the CLI and the skill:

   ```sh
   cd /root/code/devbox
   uv tool install -e .
   mkdir -p ~/.hermes/skills/devops
   ln -s "$PWD/skills/hetzner-devbox" ~/.hermes/skills/devops/hetzner-devbox
   ```

5. Check it works: `devbox status` should print `no devbox servers`.

The first `devbox up` creates an SSH key at `~/.ssh/devbox` (if missing), uploads it to Hetzner as `devbox`, and creates a firewall `devbox` that allows SSH only from this host's public IP.

## Commands

```text
devbox project add NAME --path PATH       register a project
devbox project list                       list projects
devbox project notes NAME                 show the notes on how to run it
devbox project notes NAME --set TEXT      replace them (or --set-file FILE, - for stdin)
devbox project remove NAME                forget a project

devbox up NAME [--force]                  create or reuse the server, sync code
devbox sync NAME                          copy local code to the server
devbox exec NAME -- CMD...                run a command in /srv/app and wait
devbox exec NAME --detach --name P -- CMD start a background process P
devbox ps NAME                            list background processes
devbox logs NAME P [-f] [-n LINES]        output from process P
devbox stop NAME P                        stop process P
devbox expose NAME PORT                   public preview URL for PORT
devbox ssh NAME                           shell on the server
devbox status [NAME]                      servers, processes, preview URLs
devbox down NAME                          delete the server
```

All commands accept `--json`. `exec` runs a single argument as a shell script (`devbox exec crm -- 'npm ci && npm test'`) and quotes several arguments as one command.

## How it works

- State lives in SQLite at `~/.local/state/devbox/devbox.db`. Hetzner is the source of truth for which servers exist; `up`, `status` and `down` reconcile against servers labeled `managed-by=devbox`.
- One server at a time. `up --force` allows a second one.
- Servers run Ubuntu with Docker + Compose, `mise`, `cloudflared`, `git` and build tools. Everything runs as root in `/srv/app`.
- Sync is `rsync --delete` that skips `.git` and anything in `.gitignore`, so `node_modules` and `.env` on the server are left alone.
- Background processes are systemd units named `devbox-<name>`. They survive SSH disconnects; logs are in the journal.
- Previews use a Cloudflare quick tunnel: a public, random `*.trycloudflare.com` URL that changes whenever the tunnel restarts. No Cloudflare account and no open ports.
- `down` deletes the server. Anything that exists only there (database contents, unsynced files) is gone.

## Development

```sh
uv run --with pytest pytest
```
