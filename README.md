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

6. Check it works: `devbox doctor` checks the config, local tools, `hcloud`, the token, the SSH key and firewall, leftover resources and whether `idle-check` is scheduled, and says how to fix what's wrong.

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
    up/resume options: --startup-timeout 5m, --cleanup-on-failure, --fresh, --force, --over-budget
devbox sync NAME [--watch]                copy local code to the server (keep syncing on save with --watch)
devbox pull NAME [PATHS...] [--all]       copy files that differ on the server into a local staging folder
devbox forward NAME PORT...               forward local ports to the server (PORT or LOCAL:REMOTE)
devbox secrets set NAME KEY=VALUE... [--file F] [--from-file F|-]
                                          store env values locally; written to the server now and on every up
devbox secrets list|unset|push NAME       show key names, forget keys, write to the server again
devbox db engines                         supported database engines and their options
devbox db add NAME DB --engine E --option K=V...
                                          describe a project database (postgres, sqlite)
devbox db list|remove NAME [DB]           databases and their latest dumps
devbox db dump NAME [DB] [-o FILE]        dump a database from the server to a local file
devbox db restore NAME [DB] [--file F] --yes
                                          replace a database on the server with a dump
devbox exec NAME [--timeout S] -- CMD...  run a command in /srv/app and wait
devbox exec NAME --detach --name P [--keep-alive] -- CMD
                                          start a background process P (--keep-alive: idle shutdown waits for it)
devbox ps NAME                            list background processes
devbox logs NAME P [-f] [-n LINES]        output from process P
devbox stop NAME P                        stop process P
devbox expose NAME PORT                   public preview URL for PORT
devbox ssh NAME                           shell on the server
devbox status [NAME]                      servers, processes, preview URLs, cost so far
devbox pause NAME                         snapshot the server, then delete it (= down --snapshot)
devbox down NAME [--snapshot] [--dump]    delete the server (dumping databases first with --dump); prints cost and activity
    pause/down --at-hour-end                 keep the server until just before its next billing hour, then pause/delete it
devbox touch NAME                         record activity so idle shutdown waits
devbox idle-check [--dry-run]             pause/delete servers idle past their timeout (for cron)
devbox autostop enable|disable [--yes]    install/remove the cron entry for idle-check (shows the plan without --yes)
devbox autostop status                    whether idle shutdown really runs, and when each server is checked
devbox snapshot list [NAME]               snapshots with size and cost
devbox snapshot delete IMAGE_ID           delete a snapshot
devbox doctor                             check config, tools, token, SSH key, firewall, leftovers, scheduling
devbox cleanup [--yes]                    list (or delete) unused devbox resources and stale local files
devbox history [NAME] [--limit N]         past servers and snapshots with dates, cost and activity
devbox costs [--month M | --days N | --since D] [--until D] [--project P] [--by project|month|day|type|server]
                                          estimated server + snapshot cost for a period
```

All commands accept `--json`, before or after the command (`devbox --json status`, `devbox project list --json`). With `--json`, errors are a JSON object on stdout (`error`, plus `code` where known; usage errors have `code: "usage"` and exit 2) and the exit code is nonzero. `exec` runs a single argument as a shell script (`devbox exec myapp -- 'npm ci && npm test'`) and quotes several arguments as one command.

## How it works

- State lives in SQLite at `~/.local/state/devbox/devbox.db`. Hetzner is the source of truth for which servers exist; `up`, `status` and `down` reconcile against servers labeled `managed-by=devbox` and this install's `devbox-controller` label.
- `status` is scoped to this controller and says so (`no devbox servers for controller ID`). Other controllers' devbox servers in the same Hetzner project are listed separately, read-only (`other_controllers` in `--json`); they are never adopted, cleaned up or changed.
- Every resource devbox creates (servers, SSH key, firewall) carries those labels. A resource with the expected name but without both labels is never modified; devbox stops with an error instead.
- Hetzner often sells out of server types in some locations, so the server is chosen per `up` rather than fixed in config. `devbox server-types` lists what is available now (filters: `--min-cores`, `--min-memory`, `--arch`, `--location`, `--zone`, `--all`). Without `--type`/`--location`, `up` uses the config defaults, or else the type and location of the project's last server. If that choice is sold out (or can't hold the snapshot being restored), `up` stops and suggests the cheapest comparable alternatives, same region first. Availability is Hetzner's indicator, not a guarantee.
- One server at a time. `up --force` allows a second one.
- Servers run Ubuntu with Docker + Compose, `mise`, `cloudflared`, `git` and build tools. Everything runs as root in `/srv/app`.
- Sync sends exactly the files Git sees in the working tree: tracked files plus untracked files that are not ignored, using Git's own ignore rules (so `!.env.example` style exceptions work). Ignored files on the server, such as `node_modules` and `.env`, are left alone. A file that was synced before and has since been deleted locally is deleted on the server too. Outside a Git repo, everything except `.git` is copied and nothing is deleted. Submodule contents are not synced.
- Background processes are systemd units named `devbox-<name>`. They survive SSH disconnects; logs are in the journal.
- `exec --timeout S` is enforced on the server: the command runs in its own systemd scope with `RuntimeMaxSec`, so after S seconds systemd sends SIGTERM to every process in it and SIGKILL 10 seconds later, even if the SSH connection is gone. `exec` checks the scope's result on the server and then exits with 124 (`timed_out: true` with `--json`); a real SSH failure is still reported as one. Containers started in the background (`docker compose up -d`, `docker run -d`) belong to Docker, not the scope, and keep running. Without `--timeout`, a foreground command keeps running on the server if the connection drops.
- Previews use a Cloudflare quick tunnel: a public, random `*.trycloudflare.com` URL that changes whenever the tunnel restarts. No Cloudflare account and no open ports.
- `down` deletes the server. Anything that exists only there (database contents, unsynced files) is gone.

### Startup

- `up` runs every step under one deadline: creating (or powering on) the server, waiting for SSH, first-boot setup (cloud-init), checking that Docker, Compose, mise, cloudflared, rsync, git and python3 are installed, syncing code and writing secrets. The default is `startup_timeout = "5m"`; change it in the config, per project with `devbox project set NAME --startup-timeout 10m`, or per run with `up --startup-timeout`. Every network call and wait uses the time that's left; nothing extends it.
- Progress is printed to stderr as `[mm:ss] ...` lines, including the last line of setup output while cloud-init runs (with secrets masked). With `--json`, stdout only has the final JSON.
- If cloud-init reports an error or tools are missing, `up` fails right away instead of waiting.
- On failure, `up` exits 1 (124 for a timeout, 130 for Ctrl-C) and prints the stage, how long it took, the server id and IP, that the server is kept and still billed, where the diagnostics file is (`~/.local/state/devbox/diagnostics/`: cloud-init status, the last 60 lines of setup output and failed units, secrets masked), and the commands to retry or delete. With `--json` the same information is in the error object (`code`, `stage`, `elapsed_seconds`, `limit_seconds`, `server_id`, `server_retained`, `billable`, `diagnostics`, `cleanup`, ...).
- The server is recorded as "setup failed at <stage>" (`status` shows it). Running `up` again continues setup on the same server; it never creates a second one.
- `--cleanup-on-failure` deletes the server when startup fails, but only a fresh server created by that run (never a reused one or one restored from a snapshot, and never a snapshot). devbox checks that Hetzner no longer lists it before saying it's gone.
- If the create request itself times out, the server may still appear at Hetzner later; `devbox status` picks it up.
- On the server, package downloads use short timeouts (20 s per connection, 2 retries) so a stalled mirror fails instead of hanging.

### Development loop

- `sync --watch` checks the files sync would send every second (`--interval`), waits until a burst of saves is over, and syncs. Every sync counts as activity for idle shutdown. Stop it with Ctrl-C.
- `pull` lists the files on the server (skipping `.git`, `node_modules`, virtualenvs and caches), drops what Git ignores unless `--all` or explicit paths are given, and copies the ones whose content differs from the local copy into `~/.local/state/devbox/pulls/NAME/<time>/`. Nothing in the project is changed: review with `diff -ru`, then copy what you want. It also lists tracked local files that don't exist on the server.
- `forward NAME 5432 15432:5432` runs SSH port forwarding bound to `127.0.0.1` until Ctrl-C, for local database tools and debuggers.
- Secrets live in `~/.config/devbox/secrets/NAME.json` (mode 0600, folder 0700). They are **not encrypted**; like SSH keys they rely on file permissions. Values are never printed. `up` and `secrets set` write them into the chosen env files on the server, replacing only those keys and keeping every other line. If a key appears more than once in the file, the duplicates are removed so the new value is the one that counts. Errors about malformed input name the line or argument but never repeat its content. Prefer `--from-file -` over `KEY=VALUE` arguments so values don't end up in shell history. `project remove` deletes a project's secrets.

### Databases

- Databases are described per project with an engine and options; `devbox db engines` lists them:
  - `postgres`: `database` (required), `service` (Docker Compose service; omit to use `pg_dump` on the server), `compose_file` (Compose file relative to the project when it isn't in the root, e.g. `docker/docker-compose.yml`; passed as `docker compose -f`), `user` (default `postgres`), `host`, `port`. Dumps use `pg_dump -Fc`; restores use `pg_restore --clean --if-exists --single-transaction`.
  - `sqlite`: `path` (relative to the project). Uses the server's `python3` and SQLite's backup API, so no `sqlite3` CLI is needed and the copy is consistent while the app runs. Dumping a database file that doesn't exist fails instead of creating an empty one. A restore creates missing folders, rejects empty, truncated or corrupt input (header size check and `PRAGMA integrity_check`) before it replaces anything, and removes its temporary file either way.
- Dumps are streamed to `~/.local/state/devbox/dumps/NAME/DB-<time>.<ext>` (files 0600, folders 0700, regardless of umask); `restore` uses the latest one unless `--file` is given, and always needs `--yes`.
- More engines are a small class in `src/devbox/databases.py`: options, a dump command that writes to stdout and a restore command that reads stdin.

### Snapshots, pause and resume

- `pause` (or `down --snapshot`) shuts the server down cleanly, saves its disk as a Hetzner snapshot, then deletes the server. If the snapshot fails, the server is kept and powered on again. If the snapshot succeeds but deleting the server fails, the server stays shut down (and billed), `status` shows it as powered off, and running `pause` again finishes the job with the same snapshot; `up` powers it back on instead.
- devbox records each server's power state from Hetzner. Commands that need a running server refuse with a clear message when it is powered off.
- The next `up` creates the server from the latest snapshot, so installed runtimes, `node_modules`, Docker images and database data are already there. Background processes and the preview are not running after a restore; start them again. `up --fresh` ignores snapshots; `resume` refuses to start without one.
- The restored server must have the snapshot's architecture and at least its disk size; `up` checks this.
- `keep_snapshots` (default 1) snapshots are kept per project. Older ones are deleted after a new snapshot is available, never before.
- Snapshots cost money while they exist (per GB per month; the price comes from Hetzner's pricing API). `status`, `snapshot list` and `costs` show it. `down` without `--snapshot` keeps existing snapshots; delete them with `snapshot delete`. A project with snapshots can't be removed until they are deleted.

### Idle shutdown

- `devbox idle-check` pauses (`idle_action = "pause"`, default) or deletes (`"down"`) servers whose last activity is older than the idle timeout (`idle_timeout = "8h"` by default, or per project with `project set --idle-timeout`).
- Activity is recorded by the commands a person or agent runs against a server: `up`, `sync` (and every `sync --watch` round), `exec`, `ps`, `logs`, `stop`, `ssh`, `expose`, `pull`, `forward`, `secrets set/push`, `db dump/restore` and `touch`. Read-only overviews (`status`, `history`, `costs`, `snapshot list`, `doctor`) do not count, so an automated loop polling `status` can't keep a server alive forever. Browsing the preview doesn't count either; use `devbox touch NAME` while testing by hand.
- Running work is never interrupted. Before acting, `idle-check` asks the server whether a foreground `exec` is still running (every `exec` runs in its own `devbox-exec-*` scope) or a background job started with `exec --detach --keep-alive` is active, and leaves the server alone if so, or if it can't check. Ordinary background processes such as dev servers don't keep a server alive; use `--keep-alive` for long builds or test runs.
- It only runs when scheduled. The supported way is `devbox autostop`:

  ```sh
  devbox autostop enable          # shows the cron entry it would add
  devbox autostop enable --yes    # test-runs it like cron would, then installs it
  devbox autostop status          # health, coverage and when each server is checked
  devbox autostop disable --yes
  ```

  - The entry runs `idle-check` every 5 minutes with this install's `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and a `PATH` that finds `hcloud`, `ssh` and `rsync`, so it covers exactly this config, whichever folder you run it from. Output goes to `idle-check.log` in the state folder. A marker comment above the entry lets devbox find, replace and remove only its own line; other crontab lines are never touched. Several config homes (controllers) each get their own entry.
  - Cron doesn't see your shell's `HCLOUD_TOKEN`, so `enable` requires `hcloud_token_file` in the config (a file with only the token, `chmod 600`) and refuses otherwise. The token never appears in the crontab or the log.
  - Before installing, `enable --yes` runs `idle-check --dry-run` with a cron-like environment and installs nothing if that fails. It warns when `idle_action = "down"` (servers are deleted, not paused) and when another idle-check schedule is already installed.
  - Runs never overlap: a run that starts while another is still going is skipped. Every run (not `--dry-run`) is recorded in `idle-check.json` in the state folder.
  - `autostop status` (exit 1 unless healthy) reports the schedule, the credentials, the last run, success and failure, and for each server the idle timeout (including per-project overrides and `off`), when it becomes eligible and the first check after that. A running foreground `exec` or `--keep-alive` job postpones the action. It lists which config homes and controllers have entries on this machine, and warns about other controllers that have servers in the Hetzner project but no entry here. Those servers are only reported, never changed.
  - It is healthy only when the entry exists, the token file works, the last run succeeded and a check succeeded within two intervals plus 5 minutes (15 minutes for the 5-minute entry; entries from older versions run every 15 minutes and get 35), or the entry is newer than that. A failed or overdue check, or a missing token file, counts as not enforced.

  A hand-made schedule still works: a cron entry such as

  ```cron
  */15 * * * * $HOME/.local/bin/devbox idle-check >> $HOME/.local/state/devbox/idle.log 2>&1
  ```

  or the systemd user units in [`contrib/systemd`](contrib/systemd):

  ```sh
  cp contrib/systemd/devbox-idle-check.* ~/.config/systemd/user/
  systemctl --user enable --now devbox-idle-check.timer
  ```

  For these, devbox can only see that a schedule exists (and whether the last recorded run failed), not which config it covers.

- When a server has an idle timeout but idle shutdown is not enforced (no schedule, or an autostop entry that is failing or overdue), `status` and `up` print a warning (`idle_enforcement` in `status --json`), and `doctor` reports it.

### Using the paid hour

Hetzner bills every started hour from when a server is created until it is deleted (stopped servers are billed too). A server deleted after 5 minutes costs a full hour; six 5-minute jobs on six new servers cost six hours. Once an hour has started it is paid, so devbox can keep an idle server until just before the next hour starts and reuse it for the next job.

- `idle_timeout = "hour-end"` (or `devbox project set NAME --idle-timeout hour-end`): a server that has been idle for at least `min_idle` (default `5m`, so it isn't stopped between two jobs) is paused or deleted in a window just before its next billing hour. If the window passes (work was running, or no check ran in time), the new hour is already paid, so it waits for the window before the following hour.
- `devbox pause NAME --at-hour-end` / `devbox down NAME --at-hour-end`: for workflows that stop the server after each job. The server stays usable and is paused or deleted in the window before its next billing hour. Using it again (`up`, `exec`, `sync`, `touch`, `ssh`, `expose`, `forward`, `db restore`) cancels the stop; `ps`, `logs`, `pull` and `db dump` don't. `--dump` can't be combined with it.
- The window is one check interval long and ends `billing_margin` (default `5m`) plus the expected action time before the boundary, so exactly one scheduled check falls inside it. The action time is the longest of the last five pauses (snapshot and delete) or deletes devbox timed on this machine (`action-times.json` in the state folder), 5 minutes for pause and 1 minute for down until it has measured one. With the 5-minute autostop entry a pause starts 15 minutes before the boundary at the earliest; with a hand-made schedule devbox assumes 15 minutes between checks and starts earlier.
- Hours count from the creation time Hetzner reports, which `status`, `up` and `idle-check` copy into the registry.
- A running foreground `exec` or `--keep-alive` job postpones the stop to the next hour, as for ordinary timeouts. `up` waits for a stop that is in progress, then restores from the new snapshot.
- `status` shows `paid until HH:MM, pause at about HH:MM`; `status --json` and `idle-check --json` have `policy` (`timeout`, `hour-end`, `requested` or `off`), `paid_until`, `planned_at`, `planned_action` and `missed_boundary`. `autostop status` also says when a billing hour passed without the stop.
- Both need a working schedule (`devbox autostop enable`); `--at-hour-end` warns when nothing would carry it out.

Other ways to spend less:

- When a project's servers this month are on track to cost at least as much as one server kept running all month (each new server pays its own started hours, and its own monthly cap), `status` says so and suggests `project set NAME --idle-timeout off` (`monthly_cap_hint` in `status --json`).
- Run parallel jobs on one server (`exec --detach`) instead of starting a second one; each server pays its own hours.
- Use `down` instead of `pause` when nothing on the server needs to survive: no snapshot storage and no restore time.
- `server-types` lists the cheapest first. Since June 2026 the `cpx` and `ccx` families cost 2–3 times more than `cx` for the same cores and memory.

### Housekeeping

- `devbox doctor` runs read-only checks and exits 1 if something blocks devbox from working (for example an SSH key conflict or no `hcloud`). Idle shutdown counts as scheduled for a healthy `autostop` entry for this config, an uncommented cron entry that runs `devbox idle-check`, or a systemd user timer that is both enabled and active.
- `devbox cleanup` lists what devbox created that is no longer used: labeled servers and snapshots that aren't in the registry, devbox firewalls not attached to any server (including the old `devbox` firewall), devbox SSH keys other than the one in use, and stale local host-key and sync files. Only resources labeled `managed-by=devbox` without another install's controller label are considered (including snapshots made by older versions). The SSH key devbox uses is never listed; if it can't tell which key is in use (for example the local key is also registered by someone else), it skips SSH keys and says why. Nothing is deleted without `--yes`.
- `devbox history` lists every server (newest first) with type, location, run time, cost, activity and whether it was restored from or paused into a snapshot, plus all snapshots and the overall total. It works offline from the registry.

### Costs and budget
- `up` stores the server's hourly and monthly price from Hetzner when it creates it, and every server's and snapshot's start and end time stays in the registry. Servers are estimated the way Hetzner bills them: per started hour, capped at the monthly price per calendar month. Snapshots are estimated per GB per month, prorated by the hour. Prices include VAT; primary IPv4 and traffic are not included.
- Reports compute each month's cost once and spread it evenly over the time the server ran that month. A period or a day/month row gets its share, so rows always add up to the total and adjacent periods add up to the combined period (billed hours in a partial period can be fractional). Rounded rows (6 decimals in JSON, cents in the table, 2 decimals for hours) are adjusted with the largest-remainder method so they still add up exactly to the rounded total. `costs` rejects impossible or empty dates, `--days` outside 1–36500, and periods where `--until` is not after the start.
- `status` shows the cost so far and this month's total, `down`/`pause` print the server's total and activity (syncs, execs, background starts, previews), and `devbox costs` sums any period by project, month, day, server type or server. Removing a project keeps its history.
- `monthly_budget = 20` in the config makes `up` warn at 80% and when the month's spending reaches the budget; with `budget_action = "block"` it refuses to create a server unless `--over-budget` is passed. `status` shows what is left.

## Development

```sh
uv run --with pytest pytest
```
