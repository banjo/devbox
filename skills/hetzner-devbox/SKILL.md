---
name: hetzner-devbox
description: Run projects on temporary Hetzner servers via devbox
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hetzner, devbox, remote, docker, preview]
    category: devops
    requires_toolsets: [terminal]
---

# Hetzner devbox

The `devbox` CLI gives each project a temporary Hetzner server. Code is edited on this machine and copied to the server, where installs, builds, Docker Compose, databases, dev servers and tests run. The CLI only manages the server; you decide how to run each project and keep notes on it.

## When to Use

- The user wants to run, test, build or preview a project ("start the app", "run the tests", "let me see the changes").
- A task needs Docker, a database, a dev server, a production build, a large install or integration tests.
- The user is done for now ("we're done", "shut it down").

Do **not** use it for reading or editing code, Git, or small checks such as linting one file. Those stay local and must not start a server.

## Rules

- **Never run heavy commands on this machine.** No `docker`, `npm install`, `npm run build`, dev servers or test suites locally. Use `devbox exec`.
- **Ask before `devbox up` creates a server.** It costs money. Check `devbox status NAME` first; if a server is already running, use it without asking.
- **Order the cheapest type that fits, every time.** 4 GB when a cheap one exists, otherwise the cheapest 2 GB. Never default to a `cpx*` because it feels safe; see "Choosing a server".
- **Ask before starting another server.** Each server is billed separately. If `up` refuses because `max_servers` servers are running (`code: server_limit`), don't stop the other server. Check `devbox status` for its capacity and offer the user its options in the order `up` lists them: run the work on the running server, raise `max_servers` in the config, or `up NAME --additional` this once. Never pass `--additional` or edit `max_servers` without their approval, and only suggest `pause` of the other server if the user says they're done with it.
- **Prefer `devbox pause` over `devbox down`** when the user is done. Pause keeps everything in a snapshot (a few cents a month) and the next `up` restores it. Ask before `devbox down`: it deletes the server and everything that exists only there, such as database contents.
- **Mind the budget.** If `up` warns about the monthly budget, tell the user. If it refuses, only pass `--over-budget` when the user says so.
- **Previews are public** unless you pass `--protect`. Anyone with the URL can open it. Do not expose admin tools or real data; when the app holds anything private, use `--protect` and give the user the password `expose` prints.
- **Do not commit or push** unless the user asks.
- Add `--json` when you need to parse output.
- When unsure about a command or flag, run `devbox --help` (commands by group, main config settings) or `devbox COMMAND --help` instead of guessing.

## Procedure

### First time for a project

1. `devbox project list`. If the project is missing, find its local folder and run `devbox project add NAME --path PATH`.

### Choosing a server

Hetzner often runs out of server types in some locations, so pick from what is available right now instead of assuming a type.

1. `devbox server-types --min-cores 2 --min-memory 4` lists what can be ordered now, cheapest first, with location, network zone, architecture and price. Add `--zone eu-central` (or `us-east`, `us-west`, `ap-southeast`) to stay in a region, `--arch x86` if the project needs x86 images.
2. Take the **cheapest** offer that fits, not the first comfortable one. The list is already cheapest first: prefer the user's network zone if you know it, otherwise the first entry.
   - Cost-optimised types (`category: cost_optimized`, e.g. `cx23` at ~€0.011/h (€6.86/mo)) are several times cheaper than `regular_purpose` (`cpx*`, e.g. `cpx22` at ~€0.039/h) at the same 2 vCPU / 4 GB. Take the cheap one unless the project needs the extra disk, more RAM or a faster CPU class.
   - If 4 GB is only available as an expensive type, drop the floor to 2 GB (`--min-memory 2`) and take the cheapest offer there instead of paying for 4 GB. 2 vCPU / 2 GB is enough for a single dev server, a small build and one database.
   - Only size up for a project that actually needs it (large monorepo builds, several containers, a database plus a dev server) and say why in the notes ("needs 8 GB RAM; x86 only because of image X").
   - Say which type, location and price you picked, so a wrong choice is visible before it runs for hours.
3. Tell the user what you picked and the monthly price, and ask before creating it. Ask them to choose if the options differ in a way that matters: a different region, ARM instead of x86, or noticeably higher price.
4. If `up` says the type is not available, it lists the cheapest alternatives. Pick one and ask again if it changes region or price noticeably.
5. If the project needs a certain size or architecture, record it in the notes (for example "needs 8 GB RAM; x86 only because of image X").

Without `--type`/`--location`, `up` uses the config default, or else the project's last server type and location. Check the price in `up`'s "last used" line: a restored snapshot can come back on an expensive `cpx*` type. If it is not the cheapest fit, pass `--type`/`--location` explicitly (when the snapshot's architecture and disk allow it), or at least report the price. Otherwise this is usually what you want: run `devbox up NAME` and only go through the steps above if it says the choice is unavailable or there is no previous server. When restoring a snapshot, the server needs the same architecture and at least the snapshot's disk size; `up` checks this and lists compatible alternatives.

### Starting a project

1. `devbox status NAME`. If there is no server, ask the user, then `devbox up NAME` (add `--type`/`--location` the first time, see above). If the project is paused (`status` shows a snapshot), `up` restores it. `up` waits for the server and syncs the code. It does not start anything, but it tells you what to do: its output ends with `next:` lines and the project's notes (`next` in `--json`: `state`, `steps`, `previews`, `notes`).
   - `state: restored` (from a snapshot): runtimes, dependencies, `.env` and database data are already there. Only run the notes' "every start" steps (start containers, dev servers, `expose`), not the "new server" steps.
   - `state: powered-on`: same, files and data are there, processes and previews stopped.
   - `state: running`: nothing to start.
   - `state: new` (clean server): follow all the steps below.
   - `next.previews` lists the previews the last server had, with the `expose` command for each. Run them once the app answers (add `--wait-for`).
2. Read the notes from `up`'s output (or `devbox project notes NAME`).
   - **Notes exist:** follow them.
   - **No notes:** read the project to work out what it needs: README, `package.json` scripts, `compose.yaml`/`docker-compose.yml`, `Makefile`, `Dockerfile`, `.env.example`, `.tool-versions`, `.nvmrc`, `mise.toml`, `pyproject.toml` and so on.
3. Install runtimes and dependencies with foreground `exec`:
   - `devbox exec NAME -- 'mise use -g node@22'` (mise is installed on the server)
   - `devbox exec NAME -- 'npm ci'`
4. Create env files on the server if needed. Gitignored files such as `.env` are **not** synced, and sync never overwrites them on the server:
   - `devbox exec NAME -- 'cp .env.example .env'` for non-secret defaults.
   - Real secrets (API keys, passwords) go through `devbox secrets set NAME --file PATH --from-file -` with the values on stdin. They are stored locally and written into that env file on every `up`, so you only ask the user once. Never echo secret values back, and never put them in project notes.
   - Ask the user for any secrets you cannot derive. Never invent real credentials.
   - If the project has a database worth keeping, register it once: `devbox db add NAME main --engine postgres --option service=db --option database=app` (or `--engine sqlite --option path=data/app.db`; see `devbox db engines`). Note it in the project notes.
5. Start long-running things as named background processes. Bind dev servers to `0.0.0.0` or `localhost`:
   - `devbox exec NAME -- 'docker compose up -d'` (returns when containers have started)
   - `devbox exec NAME --detach --name web -- 'npm run dev -- --host 0.0.0.0'`
6. Check that it works: `devbox ps NAME`, `devbox logs NAME web`, and `devbox exec NAME -- 'curl -sf localhost:3000 >/dev/null && echo ok'`.
7. `devbox expose NAME PORT --wait-for /` (or a health path such as `/health`) and give the user the URL. `--wait-for` only returns once the app answers, so the URL works when you hand it over. For several apps (web and API) give each a name: `expose NAME 5173 --name web`, `expose NAME 3000 --name api`.
8. If you cannot get it running, tell the user what you tried and ask how the project is normally started.
9. **Save what worked** with `devbox project notes NAME --set-file -` (heredoc on stdin). Keep it short and concrete: runtime versions, commands in order, which steps are only needed on a new server (installs, seeding), env values that are safe to store, ports, and gotchas. Never store secrets in notes. Update the notes whenever you learn something new.

Example notes:

```text
Runtime: `mise use -g node@22`.
New server: `npm ci`, `cp .env.example .env`, `docker compose up -d db`, `npm run db:migrate && npm run db:seed`.
Every start: `docker compose up -d db`, then `exec --detach --name web -- 'npm run dev -- --host 0.0.0.0'`.
Preview: `expose 5173 --wait-for /`. Ready when the log says "Local: http://...".
Gotcha: the API needs DATABASE_URL=postgres://postgres:postgres@localhost:5432/app in .env.
```

### Development loop

1. Edit locally.
2. `devbox sync NAME`. Dev servers in watch mode pick up the change. For long editing sessions you can run `devbox sync NAME --watch` in the background instead, which syncs on every save.
3. If dependencies changed: `devbox exec NAME -- 'npm ci'`, then restart the process with the same `exec --detach --name web ...` command (it replaces the old one).
4. Run tests with foreground `exec`: `devbox exec NAME -- 'npm test'`. Add `--timeout SECONDS` for anything that might hang; the server stops the command when time is up and `exec` exits with 124.
5. Debug with `devbox logs NAME PROC` and `devbox exec`. For a broken preview, check `devbox logs NAME tunnel` (`tunnel-PREVIEW` for named previews, `named-tunnel` with a stable domain, `preview-auth-PREVIEW` for the password proxy).
6. When commands on the server change files (lockfiles after `npm install`, generated migrations, formatter output), bring them back with `devbox pull NAME`. It only stages them; review with the printed `diff` command and copy what belongs in the repo. Never pull over local changes without checking.
7. If the user wants to inspect the database or debug with a local tool, `devbox forward NAME 5432` (runs until stopped).

### Finishing

1. Default: `devbox pause NAME`. It shuts the server down, saves a snapshot and deletes the server. Pass on the summary: how long it ran, its cost, the snapshot's size and monthly cost, and the project total, plus the `tell the user:` line (how to continue and how long it takes).
2. If you learned anything about running the project this session, update the notes before or right after (`pause`/`down` remind you).
3. If the user wants nothing kept, remind them that `down` deletes the server and its data (for example the database), then `devbox down NAME` when they confirm. `down` refuses while a registered database has no dump since the server was last used. `devbox down NAME --dump` saves registered databases locally first; only pass `--no-dump` when the user says the data can go; `devbox db restore NAME --yes` puts the data back on a later server (ask before restoring: it replaces the data). Existing snapshots stay unless deleted with `devbox snapshot delete IMAGE_ID`.
4. Code is always safe because it lives on this machine.

### Idle shutdown

- Servers without devbox activity for the idle timeout (default 8h) are paused automatically by `devbox idle-check`, if the user has scheduled it. `devbox status` shows how long is left. If `status` or `up` warns that idle shutdown is not enforced, tell the user: servers keep running (and costing money) until paused by hand. Suggest `devbox autostop enable` (it only shows the plan) and let the user decide; never run `autostop enable --yes` or `disable --yes` without asking. `devbox autostop status` shows whether checks really run and when each server will be checked.
- Commands you run against the server (`exec`, `sync`, `ps`, `logs`, `ssh`, ...) count as activity; `status` does not. Viewing the preview in a browser does not count either. When the user is about to test by hand, hold the server for that long: `devbox touch NAME --for 2h` (max 24h; `--clear` ends it early). Tell them when the hold ends.
- Running foreground `exec` commands are never interrupted. For long builds or test runs in the background, start them with `devbox exec NAME --detach --keep-alive --name build -- '...'` so idle shutdown waits for them. Don't use `--keep-alive` for dev servers; they would keep the server alive forever.
- If `status` says the server is powered off (for example after a pause that couldn't delete it), tell the user it is still billed and either finish with `devbox pause NAME` or start it again with `devbox up NAME`.
- For projects that need longer: `devbox project set NAME --idle-timeout 1d` (or `off`).

### Short jobs and the paid hour

Hetzner bills every started hour from when a server is created. A new server per 5-minute job pays a full hour each time; reusing the server within the hour costs nothing extra.

- When you finish a short job and more may follow, use `devbox pause NAME --at-hour-end` (or `down --at-hour-end` if nothing needs to be kept) instead of `pause`. The server stays usable until just before its next billing hour; the next `up`/`exec` reuses it and cancels the stop. Tell the user when it will be paused (the command prints it). If it warns that nothing will carry it out, autostop isn't working: tell the user and suggest `devbox autostop enable`, or pause right away instead.
- For projects with many short jobs, suggest `devbox project set NAME --idle-timeout hour-end` to the user.
- Run parallel work for a project on its one server with `exec --detach` rather than asking for a second server.
- If `status` says the project's servers are on track to cost more than one server kept running all month, pass that on and let the user decide.

### Costs

- `devbox status` shows each running server's cost so far and capacity (load, free memory and disk), kept snapshots and their monthly cost, the total for this month and the budget left.
- `devbox snapshot list` shows snapshots with size and cost; old ones beyond the configured number are deleted automatically on the next pause.
- `devbox costs` answers questions like "what did this cost?": `--month 2026-10`, `--days 7`, `--since 2026-10-01 --until 2026-11-01`, `--project NAME`, and `--by project|month|day|type|server`.
- Costs are estimates (incl. VAT): servers and their IPv4 per started hour, snapshots per GB, all capped monthly; traffic is not included. Say so when reporting them.

## Examples

| The user says | Do |
|---|---|
| "Start the app so I can test it" | If no server is running, pick an available server type, ask, `up`; then follow or work out the notes, `expose`, give the URL |
| "Change the dashboard layout" | Edit locally; `sync` if a server is running |
| "Run the tests" | `devbox exec NAME -- 'npm test'` (or whatever the notes say) |
| "Which servers are running?" | `devbox status`. It only covers this controller; also report servers it lists under other controllers, and never treat `no devbox servers for controller ...` alone as "nothing is running in Hetzner" |
| "Start shop too" (while crm runs and `up` says `server_limit`) | Show crm's capacity from `devbox status`, then ask: share crm, raise `max_servers`, or `up shop --additional` |
| "What have I spent this month?" | `devbox costs --month YYYY-MM` |
| "What did we run on this project?" | `devbox history NAME` |
| "Something's broken with devbox" | `devbox doctor` |
| "We're done for today" | `devbox pause NAME`, report the cost summary |
| "Done with this job" (more jobs likely this hour) | `devbox pause NAME --at-hour-end`, say when it will be paused |
| "Delete everything for this project" | Confirm, `devbox down NAME`, `devbox snapshot delete` for its snapshots |
| "Continue where we left off" | `devbox up NAME` (restores the snapshot), then the notes' every-start steps |

## Pitfalls

- Without `preview_domain` in config, the preview URL changes every time the tunnel restarts, and after `down`/`up`. Always give the user the current URL from `devbox status NAME`. With `preview_domain` the URL stays the same, but the preview still has to be started again after a restore (`status` says "not running").
- Vite and similar dev servers may reject the preview's host name. Don't allow every host; use the env value devbox sets on the server: `allowedHosts: [process.env.DEVBOX_PREVIEW_HOST]`. `DEVBOX_PREVIEW_URL` / `DEVBOX_PREVIEW_HOST` (plus `_NAME` variants for named previews) are there in every `exec` and background process. With `preview_domain` they are set from `up` on, so start the app after `up` and it has the right URL. With quick tunnels they are set by `expose`, so restart the app afterwards if it needs its URL. For a frontend with an API or WebSocket backend, expose only the frontend and proxy the rest through the dev server (Vite `server.proxy`, `ws: true`), so one preview URL covers everything. Put this in the project notes. See `docs/previews.md`.
- A project's registered path can be a git worktree of the repo you are working in, with the branch you need checked out there (`devbox project list` shows the path). Do the edits in that path, not in the main clone, so `sync` carries them. The server copy has no `.git`, so run git commands locally, never in `exec`.
- Formatting on the server: run the formatter in `exec` on the files you changed (keep the list in a file that is synced, since git is not available there), then `devbox pull` immediately and copy back only the source files you touched. `pull` also stages junk (tool temp dirs), and a later `sync` overwrites the server copy with the local one.
- Verifying UI plus IndexedDB/data migrations in a real browser: run the dev server with `exec --detach`, then run a Playwright script via foreground `exec` (the notes say where Playwright lives). To test a schema migration, write a legacy-shaped record into a database at the old version through raw `indexedDB` in the page, reload the app so the upgrade runs, then read the record back.
- When another project's server is already running and has room (`devbox status` shows its load, free memory and disk), and the user agrees to reuse it for a scratch job, stream a tarball into a directory outside `/srv/app`: `tar czf - . | devbox exec OTHER --timeout 120 -- 'mkdir -p /tmp/x && tar xzf - -C /tmp/x'`. stdin is forwarded, and the other project's files stay untouched. Using `exec` cancels a pending pause, so tell the user the server stays up until the idle timeout. Start a server of its own instead when the job needs its own containers, databases or ports, or will run for long.
- A project whose `package.json` has `devEngines.packageManager` with a newer pnpm breaks the mise `pnpm` shim (corepack cannot fetch it). Install that exact version into a prefix (`npm i --prefix /tmp/pn pnpm@X`) and put `/tmp/pn/node_modules/.bin` FIRST on PATH, or call a workspace's `node_modules/.bin/<tool>` directly.
- Foreground `exec` blocks until the command exits. Never use it for dev servers or `compose up` without `-d`; use `--detach`.
- A single argument after `--` is run as a shell script, so quote it: `-- 'npm ci && npm test'`. Several arguments are quoted individually, so `&&` between them is passed literally.
- `exec` flags (`--timeout`, `--detach`, `--name`) go BEFORE `--`. After it they are treated as part of the script and the whole line fails with "No such file or directory".
- Never `pkill -f PATTERN` inside a foreground `exec` when PATTERN appears in the exec's own command line: it matches and kills the exec shell itself. Use a bracket trick (`pkill -f '[/]tmp/app'`) or restart via `exec --detach --name web`, which replaces the old process.
- One-off scripts that import app source (path aliases, workspace packages) only resolve when placed inside the package that owns them; run them from there. Delete the local file afterwards; the next `sync` removes the server copy.
- To get a file back from the server, `devbox exec NAME -- 'cat FILE' > local` or a tar stream. Do not chain the tar after a command that also prints to stdout, because the output corrupts the archive.
- Projects synced to the server live in `/srv/app`; runtimes installed with `mise use -g` are only on PATH through `export PATH=$HOME/.local/share/mise/shims:$PATH` in non-login `exec` shells. Put that line in the project notes.
- Commands like `go mod tidy` can bump the language version in the lockfile/manifest (e.g. go.mod `go 1.26`). After `devbox pull`, check that the Dockerfile base image matches before assuming the production build works.
- Apps whose auth is bound to the origin (WebAuthn/passkeys RP ID, OAuth redirect URIs, cookie `Secure`) need the preview host: run `expose` FIRST to learn the host, then (re)start the app with that host in its env.
- Processes do not restart on their own. After a crash, check `logs` and run the same `exec --detach` again.
- A clean server has nothing installed: runtimes, `node_modules`, databases and `.env` must be set up again. Notes should separate "new server" steps from "every start" steps, because a server restored from a snapshot only needs the latter.
- If `up` fails, read its message: it names the stage, says whether the server was kept (it is still billed) and points to a diagnostics file. Read that file, tell the user what went wrong, and either run `devbox up NAME` again (it continues on the same server) or, if they don't want to keep it, `devbox down NAME`. A slow first boot can be given more time with `--startup-timeout 10m`. Use `--cleanup-on-failure` when nothing on a fresh server is worth keeping.
- If `exec` fails with an SSH error, run `devbox status`; the server may have been deleted.
- `up`/`doctor` failing with "Hetzner SSH key 'devbox-<controller>' does not match the local devbox key" means the local key was regenerated. When no servers exist, delete the stale Hetzner key (`HCLOUD_TOKEN=$(cat ~/.config/devbox/hcloud.token) hcloud ssh-key list`, then `hcloud ssh-key delete <id>` for the one named `devbox-<controller>`) and run `up` again; it uploads the current key.
- When `up` or another command fails in a way you don't understand (token, SSH key, firewall), run `devbox doctor` and follow its fixes.
- `devbox cleanup` only lists unused resources. Show the list to the user and only run `devbox cleanup --yes` after they agree.
- Without `--timeout`, a foreground command keeps running on the server if the connection drops. `--timeout` does not stop containers started with `-d`; stop those with `docker compose down`.
- Sync follows the project's Git ignore rules. Ignored files (such as `.env`) never reach the server, and files you delete locally are deleted on the server at the next sync.
- ARM servers (`cax*`) are often cheaper, but some Docker images and native dependencies only exist for x86.
- If `devbox status` shows a server that has been up a long time, remind the user that it still costs money.

## Verification

- `devbox status NAME` shows the server as running with the expected processes active.
- The preview URL loads.
- The project notes describe how to start it from a fresh server.
