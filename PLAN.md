# Devbox POC Plan

**Goal:** Prove the basic loop. From the Hermes host, create a temporary Hetzner server for a project, copy the code there, install and start it (Docker Compose and/or a dev server), get a preview URL, keep editing locally, and delete the server when done.

Built as a real CLI with a SQLite registry so that later features (snapshots, backups, idle shutdown) can be added without a rewrite. Everything else from the [original requirements](https://gist.github.com/banjo/961e2c548c74d249119b0732e8ce28d5) is left for later (section 8).

## 1. Pieces

- **`devbox` CLI**: a Python 3.11+ package in this repo, standard library only (`argparse`, `sqlite3`, `subprocess`). Installed with `uv tool install -e .`. It calls `hcloud`, `ssh` and `rsync` as subprocesses.
- **Registry**: SQLite at `~/.local/state/devbox/devbox.db`. Holds projects, how to run them, and their servers.
- **Skill**: `skills/hetzner-devbox/SKILL.md`, symlinked to `~/.hermes/skills/hetzner-devbox/`. Describes how the agent should work with the CLI.
- Nothing is added to the project repos. How a project is started is stored in the registry through the CLI.

## 2. Config

`~/.config/devbox/config.toml` (written once by hand):

```toml
hcloud_token_file = "~/.config/devbox/hcloud.token"  # dedicated Hetzner project
server_type = "cx23"
location = "fsn1"
image = "ubuntu-24.04"
ssh_key = "~/.ssh/devbox"                            # created with ssh-keygen if missing
```

## 3. Registry

The schema version is kept in `PRAGMA user_version`. Migrations are numbered SQL strings in `db.py` and run at startup, so new tables (such as `snapshots`) are a new migration.

```sql
-- migration 1
CREATE TABLE projects (
  name        TEXT PRIMARY KEY,
  path        TEXT NOT NULL,      -- local project folder on Hermes
  setup_cmd   TEXT,               -- runs once per new server
  start_cmd   TEXT,               -- long-running: compose and/or dev server
  port        INTEGER,            -- port the preview points at
  created_at  TEXT NOT NULL
);

CREATE TABLE servers (
  id           INTEGER PRIMARY KEY,
  project      TEXT NOT NULL REFERENCES projects(name),
  hetzner_id   INTEGER NOT NULL,
  ip           TEXT,
  server_type  TEXT,
  location     TEXT,
  status       TEXT NOT NULL,     -- creating | running | deleted
  setup_done   INTEGER NOT NULL DEFAULT 0,
  preview_url  TEXT,
  created_at   TEXT NOT NULL,
  deleted_at   TEXT
);
```

A project has at most one server with status other than `deleted`. Deleted rows are kept as history.

**Hetzner is the source of truth for whether a server exists.** Every server is created with labels `managed-by=devbox` and `project=<name>`. Before `up`, `status` and `down`, the CLI compares the registry with `hcloud server list -l managed-by=devbox -o json`:
- The registry says running but the server is gone in Hetzner: mark the row `deleted`.
- A labeled server exists but has no registry row (for example after a crash during `up`): add it to the registry.

Snapshots later: a `snapshots` table (project, hetzner_image_id, created_at, status) in migration 2. `up` then restores from the latest snapshot when one exists, and `down --snapshot` creates one first.

## 4. Commands

| Command | What it does |
|---|---|
| `devbox project add NAME --path P [--setup CMD] [--start CMD] [--port N]` | Registers a project. |
| `devbox project set NAME ...` | Changes setup, start or port. |
| `devbox project show NAME` / `devbox project list` | Shows project settings. |
| `devbox up NAME` | Creates the server if none is running, waits for cloud-init, syncs code, runs setup on a new server, (re)starts the app and the preview tunnel, prints the URL. Running it again reuses the server and only syncs and restarts. |
| `devbox sync NAME` | Copies local code to the server. A dev server in watch mode picks up the changes. |
| `devbox exec NAME -- CMD...` | Runs a command in `/srv/app` on the server and returns its exit code. |
| `devbox logs NAME [-f]` | Shows app output. |
| `devbox ssh NAME` | Opens a shell on the server. |
| `devbox status [NAME]` | Shows each project's server (or `none`), IP, type, uptime and preview URL. |
| `devbox down NAME` | Deletes the server and marks it `deleted`. Anything that exists only on the server (database contents, files not synced back) is lost. |

All commands accept `--json` for output the agent can parse.

## 5. How it works

- **Server:** `hcloud server create` with name `devbox-<name>`, the labels, the devbox SSH key, the firewall and `cloud-init.yaml`. Cloud-init installs Docker with the Compose plugin, `rsync`, `git`, `build-essential`, `mise` and `cloudflared`. Everything runs as root in `/srv/app`, which is fine for a throwaway POC box.
- **One server at a time:** `up` refuses to create a server if another project already has one running, unless `--force` is given.
- **Firewall:** one Hetzner firewall, `devbox`. It allows only SSH, and only from the Hermes host's IP, and is created on first use.
- **SSH:** `-i ssh_key`, `StrictHostKeyChecking=accept-new`, and a `known_hosts` file per server that `down` removes.
- **Sync:** `rsync -az --delete --exclude .git --filter=':- .gitignore'` from the project path to `/srv/app`. Ignored folders such as `node_modules` stay on the server.
- **App:** `start_cmd` runs as a systemd unit (`systemd-run --unit devbox-app bash -lc ...`), so it survives SSH disconnects. `up` stops the old unit before starting a new one. `logs` reads `journalctl -u devbox-app`.
- **Preview:** a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:<port>`) as a second unit. It needs no Cloudflare account, and the server exposes no ports. The URL is a public, random `*.trycloudflare.com` address read from the unit log, saved in the registry, and new each time the tunnel restarts.
- **Remote only:** `exec`, `logs` and `ssh` fail if the project has no running server. Nothing falls back to running on the Hermes host.

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
tests/test_db.py         # migrations, one-server-per-project rule
tests/test_reconcile.py  # registry vs fake hcloud output
```

## 7. The skill

The skill tells the agent:

1. **Edit locally.** Reading code, editing, Git and small checks happen on the Hermes host. Do not start a server for that.
2. **Run remotely.** Installs, builds, Compose, dev servers and tests go through `devbox exec` or `devbox up`, never directly on the Hermes host.
3. **New project:** if `devbox project show <name>` fails, ask Anton how the project is started (runtime/version, setup command, start command, port), save it with `devbox project add`, and confirm it with him.
4. **Ask before `devbox up` creates a server.** It costs money. Once a server is running, the agent can use it freely.
5. **Development loop:** edit locally, then run `devbox sync`. For a restart or new dependencies, run `devbox exec` or `devbox up`. Use `devbox logs` to debug. Always give Anton the preview URL.
6. **When done** ("vi är klara", "stäng ner"): remind Anton that the server's data is lost, then run `devbox down`. Commit and push only if he asks.
7. **Remind about forgotten servers.** If `devbox status` shows a server that has been up for a long time, tell Anton it still costs money.

| Anton says | Agent does |
|---|---|
| "Starta CRM så jag kan testa" | Asks first if no server is running, then `devbox up crm` and gives the URL |
| "Ändra layouten på dashboarden" | Edits locally, then `devbox sync crm` if a server is running |
| "Kör testerna" | `devbox exec crm -- npm test` |
| "Vi är klara för idag" | `devbox down crm` |

## 8. Steps

1. Package skeleton, config, `db.py` with migration 1, `project add/set/show/list`, and tests for the registry.
2. `hetzner.py`, reconcile and `status`. Check them against the real API with read-only calls.
3. `cloud-init.yaml`, `up`, `sync`, `exec`, `logs`, `ssh`, `down`.
4. Preview tunnel and URL in `up` and `status`.
5. `SKILL.md` and a short README covering setup and commands.
6. Live test, approved by Anton first: run one real project end to end (`project add`, `up`, open the URL, edit and `sync`, see the change, `exec` a test, `down`, confirm the server is gone in Hetzner and `deleted` in the registry).

## 9. Later, if the POC works

- Snapshots (migration 2): `down --snapshot`, restore on `up`
- Database backups to S3
- Fixed preview hostname with a named Cloudflare tunnel (and optionally Access)
- Automatic shutdown of idle servers and cost estimates
- Pulling changes made on the server back to Hermes
- Running as a non-root user and pinned host keys
