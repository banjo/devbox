# devbox

Temporary Hetzner development servers for coding agents. Code is edited locally, copied to a disposable server, and run there, so the machine the agent runs on stays light. The CLI manages the server; the agent (see [the skill](skills/hetzner-devbox/SKILL.md)) works out how to run each project and keeps notes on it.

Status: proof of concept. See [PLAN.md](PLAN.md).

## Setup

On the machine where the agent runs:

1. Install the tools: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), [`hcloud`](https://github.com/hetznercloud/cli), `ssh` and `rsync`.
2. Create a dedicated Hetzner Cloud project and an API token with read/write access. Save it:

   ```sh
   mkdir -p ~/.config/devbox
   install -m 600 /dev/null ~/.config/devbox/hcloud.token
   $EDITOR ~/.config/devbox/hcloud.token
   ```

3. Write `~/.config/devbox/config.toml`. Only the token file is required; see [`examples/config.toml`](examples/config.toml) for the other keys.

   ```toml
   hcloud_token_file = "~/.config/devbox/hcloud.token"
   ```

4. Install the CLI:

   ```sh
   git clone https://github.com/banjo/devbox && cd devbox
   uv tool install -e .
   ```

5. Install the skill by linking `skills/hetzner-devbox` into your agent's skills folder, for example:

   ```sh
   # Hermes
   mkdir -p ~/.hermes/skills/devops
   ln -s "$PWD/skills/hetzner-devbox" ~/.hermes/skills/devops/hetzner-devbox

   # Agents that read ~/.agents/skills
   mkdir -p ~/.agents/skills
   ln -s "$PWD/skills/hetzner-devbox" ~/.agents/skills/hetzner-devbox
   ```

6. Check it works: `devbox status` should print `no devbox servers`.

The first `devbox up` creates an SSH key at `~/.ssh/devbox` (if missing), uploads it to Hetzner, and creates a firewall that allows SSH only from this machine's public IP. Both are named `devbox-<controller id>`, where the controller id is a random id stored in `~/.local/state/devbox/controller-id` (or set with `controller_id` in the config).

Upgrading from a version before controller ids: Hetzner allows each public key only once per project, so if your local key is already registered as the old `devbox` key (labeled `managed-by=devbox`, no controller label), devbox keeps using that key as it is. If the same public key is registered under another owner, `up` stops and tells you how to either label that key for this install or use a different `ssh_key`. The old firewall named `devbox` is no longer used and can be deleted in the Hetzner console once no server uses it. Running servers keep working.

## Commands

```text
devbox project add NAME --path PATH       register a project
devbox project list                       list projects
devbox project notes NAME                 show the notes on how to run it
devbox project notes NAME --set TEXT      replace them (or --set-file FILE, - for stdin)
devbox project set NAME --idle-timeout D  per-project idle timeout (2h, 1d, off, default)
devbox project remove NAME                hide a project (its cost history is kept)

devbox server-types [filters]             server types that can be ordered now, cheapest first
devbox up NAME [--type T --location L]    reuse the server, or create one (from the latest snapshot
                                          if there is one; --fresh for a clean image), sync code
devbox resume NAME                        like up, but only from a snapshot
devbox sync NAME                          copy local code to the server
devbox exec NAME [--timeout S] -- CMD...  run a command in /srv/app and wait
devbox exec NAME --detach --name P -- CMD start a background process P
devbox ps NAME                            list background processes
devbox logs NAME P [-f] [-n LINES]        output from process P
devbox stop NAME P                        stop process P
devbox expose NAME PORT                   public preview URL for PORT
devbox ssh NAME                           shell on the server
devbox status [NAME]                      servers, processes, preview URLs, cost so far
devbox pause NAME                         snapshot the server, then delete it (= down --snapshot)
devbox down NAME [--snapshot]             delete the server; prints its cost and activity
devbox touch NAME                         record activity so idle shutdown waits
devbox idle-check [--dry-run]             pause/delete servers idle past their timeout (for cron)
devbox snapshot list [NAME]               snapshots with size and cost
devbox snapshot delete IMAGE_ID           delete a snapshot
devbox costs [--month M | --days N | --since D] [--until D] [--project P] [--by project|month|day|type|server]
                                          estimated server + snapshot cost for a period
```

All commands accept `--json`. `exec` runs a single argument as a shell script (`devbox exec myapp -- 'npm ci && npm test'`) and quotes several arguments as one command.

## How it works

- State lives in SQLite at `~/.local/state/devbox/devbox.db`. Hetzner is the source of truth for which servers exist; `up`, `status` and `down` reconcile against servers labeled `managed-by=devbox` and this install's `devbox-controller` label.
- Every resource devbox creates (servers, SSH key, firewall) carries those labels. A resource with the expected name but without both labels is never modified; devbox stops with an error instead.
- Hetzner often sells out of server types in some locations, so the server is chosen per `up` rather than fixed in config. `devbox server-types` lists what is available now (filters: `--min-cores`, `--min-memory`, `--arch`, `--location`, `--zone`, `--all`). Without `--type`/`--location`, `up` uses the config defaults, or else the type and location of the project's last server. If that choice is sold out (or can't hold the snapshot being restored), `up` stops and suggests the cheapest comparable alternatives, same region first. Availability is Hetzner's indicator, not a guarantee.
- One server at a time. `up --force` allows a second one.
- Servers run Ubuntu with Docker + Compose, `mise`, `cloudflared`, `git` and build tools. Everything runs as root in `/srv/app`.
- Sync sends exactly the files Git sees in the working tree: tracked files plus untracked files that are not ignored, using Git's own ignore rules (so `!.env.example` style exceptions work). Ignored files on the server, such as `node_modules` and `.env`, are left alone. A file that was synced before and has since been deleted locally is deleted on the server too. Outside a Git repo, everything except `.git` is copied and nothing is deleted. Submodule contents are not synced.
- Background processes are systemd units named `devbox-<name>`. They survive SSH disconnects; logs are in the journal.
- `exec --timeout S` is enforced on the server: the command runs in its own systemd scope with `RuntimeMaxSec`, so after S seconds systemd sends SIGTERM to every process in it and SIGKILL 10 seconds later, even if the SSH connection is gone. `exec` checks the scope's result on the server and then exits with 124 (`timed_out: true` with `--json`); a real SSH failure is still reported as one. Containers started in the background (`docker compose up -d`, `docker run -d`) belong to Docker, not the scope, and keep running. Without `--timeout`, a foreground command keeps running on the server if the connection drops.
- Previews use a Cloudflare quick tunnel: a public, random `*.trycloudflare.com` URL that changes whenever the tunnel restarts. No Cloudflare account and no open ports.
- `down` deletes the server. Anything that exists only there (database contents, unsynced files) is gone.

### Snapshots, pause and resume

- `pause` (or `down --snapshot`) shuts the server down cleanly, saves its disk as a Hetzner snapshot, then deletes the server. If the snapshot fails, the server is kept and powered on again.
- The next `up` creates the server from the latest snapshot, so installed runtimes, `node_modules`, Docker images and database data are already there. Background processes and the preview are not running after a restore; start them again. `up --fresh` ignores snapshots; `resume` refuses to start without one.
- The restored server must have the snapshot's architecture and at least its disk size; `up` checks this.
- `keep_snapshots` (default 1) snapshots are kept per project. Older ones are deleted after a new snapshot is available, never before.
- Snapshots cost money while they exist (per GB per month; the price comes from Hetzner's pricing API). `status`, `snapshot list` and `costs` show it. `down` without `--snapshot` keeps existing snapshots; delete them with `snapshot delete`. A project with snapshots can't be removed until they are deleted.

### Idle shutdown

- `devbox idle-check` pauses (`idle_action = "pause"`, default) or deletes (`"down"`) servers whose last devbox activity is older than the idle timeout (`idle_timeout = "8h"` by default, or per project with `project set --idle-timeout`). Activity is any devbox command for that server (`up`, `sync`, `exec`, `expose`, `touch`, ...). Browsing the preview does not count; use `devbox touch NAME` while testing manually.
- It only runs when scheduled. Either a cron entry:

  ```cron
  */15 * * * * $HOME/.local/bin/devbox idle-check >> $HOME/.local/state/devbox/idle.log 2>&1
  ```

  or the systemd user units in [`contrib/systemd`](contrib/systemd):

  ```sh
  cp contrib/systemd/devbox-idle-check.* ~/.config/systemd/user/
  systemctl --user enable --now devbox-idle-check.timer
  ```

### Costs and budget
- `up` stores the server's hourly and monthly price from Hetzner when it creates it, and every server's and snapshot's start and end time stays in the registry. Servers are estimated the way Hetzner bills them: per started hour, capped at the monthly price per calendar month. Snapshots are estimated per GB per month, prorated by the hour. Prices include VAT; primary IPv4 and traffic are not included.
- Reports compute each month's cost once and spread it evenly over the time the server ran that month. A period or a day/month row gets its share, so rows always add up to the total and adjacent periods add up to the combined period (billed hours in a partial period can be fractional). `costs` rejects impossible dates, `--days` outside 1–36500, and periods where `--until` is not after the start.
- `status` shows the cost so far and this month's total, `down`/`pause` print the server's total and activity (syncs, execs, background starts, previews), and `devbox costs` sums any period by project, month, day, server type or server. Removing a project keeps its history.
- `monthly_budget = 20` in the config makes `up` warn at 80% and when the month's spending reaches the budget; with `budget_action = "block"` it refuses to create a server unless `--over-budget` is passed. `status` shows what is left.

## Development

```sh
uv run --with pytest pytest
```
