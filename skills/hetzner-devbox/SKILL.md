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
- **Only one server at a time.** If `up` says another server is running, ask the user whether to `devbox down` that one. Never pass `--force` without their approval.
- **Prefer `devbox pause` over `devbox down`** when the user is done. Pause keeps everything in a snapshot (a few cents a month) and the next `up` restores it. Ask before `devbox down`: it deletes the server and everything that exists only there, such as database contents.
- **Mind the budget.** If `up` warns about the monthly budget, tell the user. If it refuses, only pass `--over-budget` when the user says so.
- **Previews are public.** Anyone with the URL can open it. Do not expose admin tools or real data.
- **Do not commit or push** unless the user asks.
- Add `--json` when you need to parse output.

## Procedure

### First time for a project

1. `devbox project list`. If the project is missing, find its local folder and run `devbox project add NAME --path PATH`.

### Choosing a server

Hetzner often runs out of server types in some locations, so pick from what is available right now instead of assuming a type.

1. `devbox server-types --min-cores 2 --min-memory 4` lists what can be ordered now, cheapest first, with location, network zone, architecture and price. Add `--zone eu-central` (or `us-east`, `us-west`, `ap-southeast`) to stay in a region, `--arch x86` if the project needs x86 images.
2. Pick a sensible default: the cheapest available type with at least 2 vCPU and 4 GB RAM, close to the user (same network zone as before, if you know it). Use more for heavy projects (large builds, several containers, a database plus a dev server); the notes can say what the project needs.
3. Tell the user what you picked and the monthly price, and ask before creating it. Ask them to choose if the options differ in a way that matters: a different region, ARM instead of x86, or noticeably higher price.
4. If `up` says the type is not available, it lists the cheapest alternatives. Pick one and ask again if it changes region or price noticeably.
5. If the project needs a certain size or architecture, record it in the notes (for example "needs 8 GB RAM; x86 only because of image X").

Without `--type`/`--location`, `up` uses the config default, or else the project's last server type and location. That is usually what you want: run `devbox up NAME` and only go through the steps above if it says the choice is unavailable or there is no previous server. When restoring a snapshot, the server needs the same architecture and at least the snapshot's disk size; `up` checks this and lists compatible alternatives.

### Starting a project

1. `devbox status NAME`. If there is no server, ask the user, then `devbox up NAME` (add `--type`/`--location` the first time, see above). If the project is paused (`status` shows a snapshot), `up` restores it. `up` waits for the server and syncs the code. It does not start anything.
   - **Restored from a snapshot:** runtimes, dependencies, `.env` and database data are already there. Only run the notes' "every start" steps (start containers, dev servers, `expose`), not the "new server" steps.
   - **Clean server:** follow all the steps below.
2. `devbox project notes NAME`.
   - **Notes exist:** follow them.
   - **No notes:** read the project to work out what it needs: README, `package.json` scripts, `compose.yaml`/`docker-compose.yml`, `Makefile`, `Dockerfile`, `.env.example`, `.tool-versions`, `.nvmrc`, `mise.toml`, `pyproject.toml` and so on.
3. Install runtimes and dependencies with foreground `exec`:
   - `devbox exec NAME -- 'mise use -g node@22'` (mise is installed on the server)
   - `devbox exec NAME -- 'npm ci'`
4. Create env files on the server if needed. Gitignored files such as `.env` are **not** synced, and sync never overwrites them on the server:
   - `devbox exec NAME -- 'cp .env.example .env'`, then edit values with `sed` or a heredoc.
   - Ask the user for any secrets you cannot derive. Never invent real credentials.
5. Start long-running things as named background processes. Bind dev servers to `0.0.0.0` or `localhost`:
   - `devbox exec NAME -- 'docker compose up -d'` (returns when containers have started)
   - `devbox exec NAME --detach --name web -- 'npm run dev -- --host 0.0.0.0'`
6. Check that it works: `devbox ps NAME`, `devbox logs NAME web`, and `devbox exec NAME -- 'curl -sf localhost:3000 >/dev/null && echo ok'`.
7. `devbox expose NAME PORT` and give the user the URL.
8. If you cannot get it running, tell the user what you tried and ask how the project is normally started.
9. **Save what worked** with `devbox project notes NAME --set-file -` (heredoc on stdin). Keep it short and concrete: runtime versions, commands in order, which steps are only needed on a new server (installs, seeding), env values that are safe to store, ports, and gotchas. Never store secrets in notes. Update the notes whenever you learn something new.

Example notes:

```text
Runtime: `mise use -g node@22`.
New server: `npm ci`, `cp .env.example .env`, `docker compose up -d db`, `npm run db:migrate && npm run db:seed`.
Every start: `docker compose up -d db`, then `exec --detach --name web -- 'npm run dev -- --host 0.0.0.0'`.
Preview: `expose 5173`. Ready when the log says "Local: http://...".
Gotcha: the API needs DATABASE_URL=postgres://postgres:postgres@localhost:5432/app in .env.
```

### Development loop

1. Edit locally.
2. `devbox sync NAME`. Dev servers in watch mode pick up the change.
3. If dependencies changed: `devbox exec NAME -- 'npm ci'`, then restart the process with the same `exec --detach --name web ...` command (it replaces the old one).
4. Run tests with foreground `exec`: `devbox exec NAME -- 'npm test'`. Add `--timeout SECONDS` for anything that might hang; the server stops the command when time is up and `exec` exits with 124.
5. Debug with `devbox logs NAME PROC` and `devbox exec`. For a broken preview, check `devbox logs NAME tunnel`.

### Finishing

1. Default: `devbox pause NAME`. It shuts the server down, saves a snapshot and deletes the server. Pass on the summary: how long it ran, its cost, the snapshot's size and monthly cost, and the project total.
2. If the user wants nothing kept, remind them that `down` deletes the server and its data (for example the database), then `devbox down NAME` when they confirm. Existing snapshots stay unless deleted with `devbox snapshot delete IMAGE_ID`.
3. Code is always safe because it lives on this machine.

### Idle shutdown

- Servers without devbox activity for the idle timeout (default 8h) are paused automatically by `devbox idle-check`, if the user has scheduled it. `devbox status` shows how long is left.
- Viewing the preview in a browser does not count as activity. While the user is testing by hand for a long time, run `devbox touch NAME` now and then.
- For projects that need longer: `devbox project set NAME --idle-timeout 1d` (or `off`).

### Costs

- `devbox status` shows each running server's cost so far, kept snapshots and their monthly cost, the total for this month and the budget left.
- `devbox snapshot list` shows snapshots with size and cost; old ones beyond the configured number are deleted automatically on the next pause.
- `devbox costs` answers questions like "what did this cost?": `--month 2026-10`, `--days 7`, `--since 2026-10-01 --until 2026-11-01`, `--project NAME`, and `--by project|month|day|type|server`.
- Costs are estimates (incl. VAT): servers per started hour, snapshots per GB, both capped monthly; IPv4 and traffic are not included. Say so when reporting them.

## Examples

| The user says | Do |
|---|---|
| "Start the app so I can test it" | If no server is running, pick an available server type, ask, `up`; then follow or work out the notes, `expose`, give the URL |
| "Change the dashboard layout" | Edit locally; `sync` if a server is running |
| "Run the tests" | `devbox exec NAME -- 'npm test'` (or whatever the notes say) |
| "Which servers are running?" | `devbox status` |
| "What have I spent this month?" | `devbox costs --month YYYY-MM` |
| "We're done for today" | `devbox pause NAME`, report the cost summary |
| "Delete everything for this project" | Confirm, `devbox down NAME`, `devbox snapshot delete` for its snapshots |
| "Continue where we left off" | `devbox up NAME` (restores the snapshot), then the notes' every-start steps |

## Pitfalls

- The preview URL changes every time the tunnel restarts, and after `down`/`up`. Always give the user the current URL from `devbox status NAME`.
- Foreground `exec` blocks until the command exits. Never use it for dev servers or `compose up` without `-d`; use `--detach`.
- A single argument after `--` is run as a shell script, so quote it: `-- 'npm ci && npm test'`. Several arguments are quoted individually, so `&&` between them is passed literally.
- Processes do not restart on their own. After a crash, check `logs` and run the same `exec --detach` again.
- A clean server has nothing installed: runtimes, `node_modules`, databases and `.env` must be set up again. Notes should separate "new server" steps from "every start" steps, because a server restored from a snapshot only needs the latter.
- If `exec` fails with an SSH error, run `devbox status`; the server may have been deleted.
- Without `--timeout`, a foreground command keeps running on the server if the connection drops. `--timeout` does not stop containers started with `-d`; stop those with `docker compose down`.
- Sync follows the project's Git ignore rules. Ignored files (such as `.env`) never reach the server, and files you delete locally are deleted on the server at the next sync.
- ARM servers (`cax*`) are often cheaper, but some Docker images and native dependencies only exist for x86.
- If `devbox status` shows a server that has been up a long time, remind the user that it still costs money.

## Verification

- `devbox status NAME` shows the server as running with the expected processes active.
- The preview URL loads.
- The project notes describe how to start it from a fresh server.
