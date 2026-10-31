# Devbox POC Plan

**Goal:** Prove the basic loop. From the machine the agent runs on, create a temporary Hetzner server for a project, copy the code there, let the agent install and start it however that project needs, get a preview URL, keep editing locally, and delete the server when done.

The CLI does not know how any project is built. It only manages the server and runs commands on it. The agent works out how to start each project and keeps notes on it. A SQLite registry makes it possible to add snapshots, backups and idle shutdown later without a rewrite. Everything else from the [original requirements](https://gist.github.com/banjo/961e2c548c74d249119b0732e8ce28d5) is left for later (section 9).

## 1. Pieces

- **`devbox` CLI**: a Python 3.11+ package in this repo, standard library only (`argparse`, `sqlite3`, `subprocess`). Installed with `uv tool install -e .`. It calls `hcloud`, `ssh` and `rsync` as subprocesses.
- **Registry**: SQLite at `~/.local/state/devbox/devbox.db`, holding projects, their notes, and their servers.
- **Skill**: `skills/hetzner-devbox/SKILL.md`, linked into the agent's skills folder (for example `~/.hermes/skills/` or `~/.agents/skills/`). Describes how the agent works with the CLI and how it figures out how to run a project.
- Nothing is added to the project repos.

## 2. Config

`~/.config/devbox/config.toml` (written once by hand):

```toml
hcloud_token_file = "~/.config/devbox/hcloud.token"  # dedicated Hetzner project
image = "ubuntu-24.04"
ssh_key = "~/.ssh/devbox"                            # created with ssh-keygen if missing
# server_type / location: optional defaults, normally left out
```

### Choosing a server

Hetzner regularly sells out of server types in some locations, so the server is not fixed in config. `devbox server-types` reads `hcloud server-type list` and `hcloud location list` and prints every type/location that can be ordered now (per-location `available` flag, not deprecated), cheapest first, with vCPU, RAM, disk, architecture, network zone and price. The agent picks one (cheapest available with at least 2 vCPU / 4 GB near the user by default), tells the user the price, and asks before `devbox up NAME --type T --location L`. If the choice is sold out, `up` refuses and lists the cheapest comparable alternatives.

## 3. Registry

The schema version is kept in `PRAGMA user_version`. Migrations are numbered SQL strings in `db.py` and run at startup, so new tables (such as `snapshots`) are just a new migration.

```sql
-- migration 1
CREATE TABLE projects (
  name        TEXT PRIMARY KEY,
  path        TEXT NOT NULL,      -- local project folder
  notes       TEXT NOT NULL DEFAULT '',  -- free text, written by the agent
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE servers (
  id           INTEGER PRIMARY KEY,
  project      TEXT NOT NULL REFERENCES projects(name),
  hetzner_id   INTEGER NOT NULL,
  ip           TEXT,
  server_type  TEXT,
  location     TEXT,
  status       TEXT NOT NULL,     -- creating | running | deleted
  preview_port INTEGER,
  preview_url  TEXT,
  created_at   TEXT NOT NULL,
  deleted_at   TEXT
);
```

A project has at most one server with status other than `deleted`. Deleted rows are kept as history.

**Hetzner is the source of truth for whether a server exists.** Every server is created with labels `managed-by=devbox`, `devbox-controller=<id>` (a random id per install) and `project=<name>`. The SSH key and firewall are named `devbox-<id>` and carry the same labels; a resource with that name but without the labels is never modified. Before `up`, `status` and `down`, the CLI compares the registry with `hcloud server list -l managed-by=devbox -o json`:
- The registry says running but the server is gone in Hetzner: mark the row `deleted`.
- A labeled server exists with no registry row (for example after a crash during `up`): add it to the registry.

Background processes are not stored in the registry. They are systemd units on the server, and `ps` asks the server directly.

Migration 2 adds cost tracking: `servers.price_hourly` and `servers.price_monthly` (stored by `up` from the offer it created), `projects.removed_at` (so `project remove` keeps cost history), and an `events` table (project, server, kind, time) counting `up`, `sync`, `exec`, `exec_detach`, `expose` and `down` for the per-server stats.

Snapshots later: a `snapshots` table (project, hetzner_image_id, created_at, status) in migration 3. `up` then restores from the latest snapshot when one exists, and `down --snapshot` creates one first.

### Project notes

The notes are free text that the agent writes once it has got a project running, for example:

```text
Node 22 via mise (`mise use -g node@22`).
`docker compose up -d db` (Postgres on 5432), then `npm ci`.
Seed: `npm run db:seed` (only on a new server).
Dev server: `exec --detach --name web -- npm run dev -- --host 0.0.0.0`, port 3000.
Needs .env with DATABASE_URL=postgres://postgres:postgres@localhost:5432/app.
```

Next time the agent reads the notes first and does not have to work it out again. If something has changed, it updates them.

## 4. Commands

| Command | What it does |
|---|---|
| `devbox project add NAME --path P` | Registers a project. |
| `devbox project list` | Lists projects and whether each has a server. |
| `devbox project notes NAME [--set TEXT \| --set-file F]` | Shows or replaces the project's notes. |
| `devbox project remove NAME` | Forgets a project that has no server. |
| `devbox server-types [--min-cores N] [--min-memory GB] [--arch x86\|arm] [--location L] [--zone Z] [--all]` | Lists server types that can be ordered now, cheapest first. |
| `devbox up NAME [--type T --location L]` | Creates the server if none is running (waits for cloud-init), then syncs code. Runs no project commands. Running it again reuses the server and only syncs. |
| `devbox sync NAME` | Copies local code to the server. |
| `devbox exec NAME -- CMD...` | Runs a command in `/srv/app` on the server, waits for it, and returns its exit code. |
| `devbox exec NAME --detach --name PROC -- CMD...` | Starts a long-running command (dev server, `compose up` in the foreground, watcher) as a systemd unit `devbox-PROC`. Replaces a running process with the same name. |
| `devbox ps NAME` | Lists background processes and whether they are running. |
| `devbox stop NAME PROC` | Stops a background process. |
| `devbox logs NAME PROC [-f] [-n LINES]` | Shows output from a background process. |
| `devbox expose NAME PORT` | Starts or moves the preview tunnel to a port on the server and prints the URL. |
| `devbox ssh NAME` | Opens a shell on the server. |
| `devbox status [NAME]` | Shows each project's server (or `none`), IP, type, uptime, cost so far, background processes and preview URL, plus this month's total. |
| `devbox down NAME` | Deletes the server and marks it `deleted`, then prints its uptime, billed hours, estimated cost, activity counts and the project's total. Anything that exists only on the server (database contents, files not synced back) is lost. |
| `devbox costs [--month M \| --days N \| --since D] [--until D] [--project P] [--by project\|month\|day\|type\|server]` | Estimated cost for any period, grouped. Per started hour, capped at the monthly price per calendar month, server price only. |

All commands accept `--json` for output the agent can parse.

## 5. How it works

- **Server:** `hcloud server create` with name `devbox-<name>`, the labels, the devbox SSH key, the firewall and `cloud-init.yaml`. Cloud-init installs only general tools: Docker with the Compose plugin, `rsync`, `git`, `build-essential`, `mise` and `cloudflared`. Anything project-specific is up to the agent. Everything runs as root in `/srv/app`, which is fine for a throwaway POC box.
- **One server at a time:** `up` refuses to create a server if another project already has one running, unless `--force` is given.
- **Firewall:** one Hetzner firewall, `devbox`. It allows only SSH, and only from the controlling machine's IP, and is created on first use.
- **SSH:** `-i ssh_key`, `StrictHostKeyChecking=accept-new`, and a `known_hosts` file per server that `down` removes.
- **Sync:** `rsync -az --delete --exclude .git --filter=':- .gitignore'` from the project path to `/srv/app`. Ignored folders such as `node_modules` stay on the server.
- **Background processes:** `systemd-run --unit devbox-<proc> --working-directory /srv/app bash -lc '<cmd>'`, so they survive SSH disconnects. `ps` lists `devbox-*` units, and `logs` reads `journalctl -u devbox-<proc>`.
- **Preview:** a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:<port>`) as the unit `devbox-tunnel`. It needs no Cloudflare account, and the server exposes no ports. The URL is a public, random `*.trycloudflare.com` address read from the unit log, saved in the registry, and new each time the tunnel restarts.
- **Remote only:** `exec`, `ps`, `logs`, `expose` and `ssh` fail if the project has no running server. Nothing falls back to running locally.

## 6. Files

```text
pyproject.toml
README.md
src/devbox/__main__.py
src/devbox/cli.py        # argparse and commands
src/devbox/config.py     # config.toml
src/devbox/db.py         # connection, migrations, queries
src/devbox/hetzner.py    # hcloud wrapper: create, list, delete, firewall, ssh key
src/devbox/remote.py     # ssh, rsync, systemd units, tunnel URL
src/devbox/cloud-init.yaml
skills/hetzner-devbox/SKILL.md
tests/test_db.py         # migrations, one-server-per-project rule, notes
tests/test_reconcile.py  # registry vs Hetzner state
tests/test_hetzner.py    # hcloud wrapper against a fake hcloud executable
tests/test_cli.py        # project commands, exec argument handling, no local fallback, down/status/costs
tests/test_remote.py     # Git-based sync file list and deletions, exec timeout enforcement
tests/test_costs.py      # billing math, period summaries, cost history
src/devbox/costs.py      # cost estimates
```

## 7. The skill

The skill tells the agent:

1. **Edit locally.** Reading code, editing, Git and small checks happen locally. Do not start a server for that.
2. **Run remotely.** Installs, builds, Compose, dev servers and tests go through `devbox exec`, never locally.
3. **Ask before `devbox up` creates a server.** It costs money. Once a server is running, the agent can use it freely.
4. **Starting a project:**
   1. `devbox project notes NAME`. If there are notes, follow them.
   2. If there are none, read the project (README, `package.json`, `compose.yaml`, `Makefile`, `.env.example`, `.tool-versions`/`.nvmrc` and so on) and work out what it needs.
   3. Install the runtime and dependencies with `devbox exec`, start services and dev servers with `devbox exec --detach`, check `devbox logs`, then `devbox expose` the right port.
   4. Ask the user if it is still unclear how the project is run, or which secrets or env values it needs.
   5. Once it works, save or update the notes with `devbox project notes NAME --set ...`. Keep them short: steps, commands, ports, env values, gotchas.
5. **Development loop:** edit locally, then run `devbox sync`. For new dependencies or a restart, use `devbox exec` or restart the process with `exec --detach`. Use `devbox logs` to debug. Always give the user the preview URL.
6. **When done** ("we're done", "shut it down"): remind the user that the server's data is lost, then run `devbox down`. Commit and push only if they ask.
7. **Remind about forgotten servers.** If `devbox status` shows a server that has been up for a long time, tell the user it still costs money.

| The user says | Agent does |
|---|---|
| "Start the app so I can test it" | Asks first if no server is running, `devbox up myapp`, follows the notes (or works it out), `expose`, gives the URL |
| "Change the dashboard layout" | Edits locally, then `devbox sync myapp` if a server is running |
| "Run the tests" | `devbox exec myapp -- npm test` |
| "We're done for today" | `devbox down myapp` |

## 8. Steps

1. Package skeleton, config, `db.py` with migration 1, `project add/list/notes`, and tests for the registry.
2. `hetzner.py`, reconcile and `status`. Check them against the real API with read-only calls.
3. `cloud-init.yaml`, `up`, `sync`, `exec` (plain and `--detach`), `ps`, `stop`, `logs`, `ssh`, `down`.
4. `expose` and the preview URL in `status`.
5. `SKILL.md` and a short README covering setup and commands.
6. Live test, approved by the user first: let the agent take one real project with no notes from `up` to a working preview URL, check that it saved notes, edit and `sync` to see the change, `down`, then `up` again and confirm it starts the project from the notes alone. Finally check the server is gone in Hetzner and `deleted` in the registry.

## 9. Later, if the POC works

- Snapshots (migration 3): `down --snapshot`, restore on `up`
- Database backups to S3
- Fixed preview hostname with a named Cloudflare tunnel (and optionally Access)
- Automatic shutdown of idle servers and cost estimates
- Pulling changes made on the server back to the local copy
- Running as a non-root user and pinned host keys
