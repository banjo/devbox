# Devbox POC Plan

**Goal:** Prove the basic loop. From the Hermes host, create a temporary Hetzner server for a project, copy the code there, install and start it (Docker Compose and/or a dev server), get a preview URL, keep editing locally, and delete the server when done.

Snapshots, backups, Cloudflare Access, idle archival, cost tracking and the rest of the [original requirements](https://gist.github.com/banjo/961e2c548c74d249119b0732e8ce28d5) are left for later (section 7).

## 1. Pieces

- **`devbox`**: one Bash script in this repo, symlinked to `~/.local/bin/devbox`. It calls `hcloud`, `ssh`, `rsync` and nothing else.
- **Skill**: `skills/hetzner-devbox/SKILL.md`, symlinked to `~/.hermes/skills/hetzner-devbox/`. Describes how the agent should work with the CLI.
- **Project notes**: one small file per project on the Hermes host, written by the agent. Nothing is added to the project repos.

No database, no state file. Hetzner is the state: the server for a project is the one named `devbox-<project>` with label `managed-by=devbox`.

## 2. Config

`~/.config/devbox/config.env` (written once by hand):

```sh
HCLOUD_TOKEN=...            # dedicated Hetzner project
SERVER_TYPE=cx23
LOCATION=fsn1
IMAGE=ubuntu-24.04
SSH_KEY=~/.ssh/devbox       # created with ssh-keygen if missing
```

`~/.config/devbox/projects/<project>.env` (written by the agent after asking Anton):

```sh
PROJECT_PATH=/root/code/crm
SETUP='mise use -g node@22 && npm ci'                          # runs once per new server
START='docker compose up -d && npm run dev -- --host 0.0.0.0'  # long-running
PORT=3000                                                      # port the preview points at
```

## 3. Commands

| Command | What it does |
|---|---|
| `devbox up PROJECT` | Creates the server if it does not exist, waits for cloud-init, syncs code, runs `SETUP` the first time, (re)starts `START`, starts the preview tunnel, prints the URL. Running it again reuses the server and just syncs and restarts. |
| `devbox sync PROJECT` | Copies local code to the server. A dev server running in watch mode picks up the changes. |
| `devbox exec PROJECT -- CMD...` | Runs a command in the project folder on the server and returns its exit code. |
| `devbox logs PROJECT [-f]` | Shows output from `START`. |
| `devbox ssh PROJECT` | Opens a shell on the server. |
| `devbox status` | Lists devbox servers with project, IP, type, uptime and preview URL. |
| `devbox down PROJECT` | Deletes the server. Anything that exists only on the server (database contents, files not synced back) is lost. |

## 4. How it works

- **Server:** `hcloud server create` with name `devbox-<project>`, labels `managed-by=devbox,project=<project>`, the devbox SSH key and `cloud-init.yaml`. Cloud-init installs Docker with the Compose plugin, `rsync`, `git`, `build-essential`, `mise` and `cloudflared`. Everything runs as root in `/srv/app`. That is fine for a disposable POC box.
- **One server at a time:** `up` refuses to create a server if another `managed-by=devbox` server exists, unless `--force` is given.
- **Firewall:** one Hetzner firewall, `devbox`, that allows only SSH, and only from the Hermes host's IP. It is created on first use.
- **SSH:** `-i $SSH_KEY`, `StrictHostKeyChecking=accept-new`, and a `known_hosts` file per project that `down` removes.
- **Sync:** `rsync -az --delete --exclude .git --filter=':- .gitignore'` from `PROJECT_PATH` to `/srv/app`. Ignored folders such as `node_modules` stay on the server and are not deleted.
- **Start:** `START` runs as a systemd unit (`systemd-run --unit devbox-app bash -lc "$START"`) with output in the journal, so it survives SSH disconnects. `up` stops the old unit before starting a new one. `logs` = `journalctl -u devbox-app`.
- **Preview:** a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:$PORT`) as another systemd unit. It needs no Cloudflare account, and the server exposes no ports. The URL is a public, random `*.trycloudflare.com` address read from the unit log. It changes every time the tunnel restarts.
- **Remote only:** `exec`, `logs` and `ssh` fail if the server does not exist. Nothing ever falls back to running on the Hermes host.

## 5. The skill

The skill tells the agent:

1. **Edit locally.** Reading code, editing, Git and small checks happen on the Hermes host. Do not start a server for that.
2. **Run remotely.** Installs, builds, Compose, dev servers and tests go through `devbox exec` or `devbox up`, never directly on the Hermes host.
3. **First time for a project:** if `~/.config/devbox/projects/<project>.env` is missing, ask Anton how the project is started (runtime/version, setup command, start command, port), write the file, and confirm it with him.
4. **Ask before creating a server.** It costs money. Once a server is running, the agent can use it freely.
5. **Development loop:** edit locally, then run `devbox sync`. If the change needs a restart or new dependencies, run `devbox exec` or `devbox up` instead. Use `devbox logs` to debug and always give Anton the preview URL.
6. **When done** ("vi är klara", "stäng ner"): remind Anton that the server's data is lost, then run `devbox down`. Commit and push only if he asks.
7. **Remind about forgotten servers.** If `devbox status` shows a server Anton has not used for a while, tell him it still costs money.

Phrase examples for the skill:

| Anton says | Agent does |
|---|---|
| "Starta CRM så jag kan testa" | Asks first if no server is running, then `devbox up crm` and gives the URL |
| "Ändra layouten på dashboarden" | Edits locally, then `devbox sync crm` if a server is running |
| "Kör testerna" | `devbox exec crm -- npm test` |
| "Vi är klara för idag" | `devbox down crm` |

## 6. Steps

1. Script skeleton: config loading, `status`, label lookup, SSH key and firewall setup. Test against the real API with read-only calls.
2. `cloud-init.yaml`, `up` (create + wait + sync + setup + start), `sync`, `exec`, `logs`, `ssh`, `down`.
3. Preview tunnel and URL in `up`/`status`.
4. `SKILL.md` and a short README covering setup and commands.
5. Live test, approved by Anton first: run one real project end to end (`up`, open URL, edit + sync, see change, `exec` a test, `down`, confirm the server is gone in Hetzner).

ShellCheck on the script. No test suite for the POC.

## 7. Later, if the POC works

- Snapshots before `down` and restore on `up`, for faster startup
- Database backups to S3
- Fixed preview hostname with a named Cloudflare tunnel (and optionally Access)
- Automatic shutdown of idle servers and cost estimates
- Pulling changes made on the server back to Hermes
- Running as a non-root user and pinned host keys
