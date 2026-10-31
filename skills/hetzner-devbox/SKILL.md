---
name: hetzner-devbox
description: Run projects on temporary Hetzner servers via devbox
version: 0.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [hetzner, devbox, remote, docker, preview]
    category: devops
    requires_toolsets: [terminal]
---

# Hetzner devbox

The `devbox` CLI gives each project a temporary Hetzner server. Code is edited here on the Hermes host and copied to the server, where installs, builds, Docker Compose, databases, dev servers and tests run. The CLI only manages the server; you decide how to run each project and keep notes on it.

## When to Use

- Anton wants to run, test, build or preview a project ("starta appen", "kör testerna", "jag vill se ändringarna").
- A task needs Docker, a database, a dev server, a production build, a large install or integration tests.
- Anton is done for now ("vi är klara", "stäng ner").

Do **not** use it for reading or editing code, Git, or small checks such as a quick lint of one file. Those stay local, and must not start a server.

## Rules

- **Never run heavy commands on the Hermes host.** No `docker`, `npm install`, `npm run build`, dev servers or test suites locally. This host has 4 GB RAM and runs Hermes itself. Use `devbox exec`.
- **Ask before `devbox up` creates a server.** It costs money. Check `devbox status NAME` first; if a server is already running, use it without asking.
- **Only one server at a time.** If `up` says another server is running, ask Anton whether to `devbox down` that one. Never pass `--force` without his approval.
- **Ask before `devbox down`.** It deletes the server and everything that exists only there, such as database contents.
- **Previews are public.** Anyone with the URL can open it. Do not expose admin tools or real data.
- **Do not commit or push** unless Anton asks.
- Add `--json` when you need to parse output.

## Procedure

### First time for a project

1. `devbox project list`. If the project is missing, find its local folder and run `devbox project add NAME --path PATH`.

### Starting a project

1. `devbox status NAME`. If there is no server, ask Anton, then `devbox up NAME`. `up` waits for the server and syncs the code. It does not start anything.
2. `devbox project notes NAME`.
   - **Notes exist:** follow them.
   - **No notes:** read the project to work out what it needs: README, `package.json` scripts, `compose.yaml`/`docker-compose.yml`, `Makefile`, `Dockerfile`, `.env.example`, `.tool-versions`, `.nvmrc`, `mise.toml`, `pyproject.toml` and so on.
3. Install runtimes and dependencies with foreground `exec`:
   - `devbox exec NAME -- 'mise use -g node@22'` (mise is installed on the server)
   - `devbox exec NAME -- 'npm ci'`
4. Create env files on the server if needed. Gitignored files such as `.env` are **not** synced, and sync never overwrites them on the server:
   - `devbox exec NAME -- 'cp .env.example .env'`, then edit values with `sed` or a heredoc.
   - Ask Anton for any secrets you cannot derive. Never invent real credentials.
5. Start long-running things as named background processes. Bind dev servers to `0.0.0.0` or `localhost`:
   - `devbox exec NAME -- 'docker compose up -d'` (returns when containers have started)
   - `devbox exec NAME --detach --name web -- 'npm run dev -- --host 0.0.0.0'`
6. Check that it works: `devbox ps NAME`, `devbox logs NAME web`, and `devbox exec NAME -- 'curl -sf localhost:3000 >/dev/null && echo ok'`.
7. `devbox expose NAME PORT` and give Anton the URL.
8. If you cannot get it running, tell Anton what you tried and ask how the project is normally started.
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
4. Run tests with foreground `exec`: `devbox exec NAME -- 'npm test'`.
5. Debug with `devbox logs NAME PROC` and `devbox exec`. For a broken preview, check `devbox logs NAME tunnel`.

### Finishing

1. Remind Anton that `down` deletes the server and its data (for example the database). Code is safe because it lives on the Hermes host.
2. When he confirms: `devbox down NAME`.

## Phrases

| Anton says | Do |
|---|---|
| "Starta CRM så jag kan testa" | Ask if no server is running, `up`, follow or work out the notes, `expose`, give the URL |
| "Ändra layouten på dashboarden" | Edit locally; `sync` if a server is running |
| "Kör testerna" | `devbox exec crm -- 'npm test'` (or whatever the notes say) |
| "Vilka servrar är igång?" | `devbox status` |
| "Vi är klara för idag" | Remind about data loss, then `devbox down crm` |

## Pitfalls

- The preview URL changes every time the tunnel restarts, and after `down`/`up`. Always give Anton the current URL from `devbox status NAME`.
- Foreground `exec` blocks until the command exits. Never use it for dev servers or `compose up` without `-d`; use `--detach`.
- A single argument after `--` is run as a shell script, so quote it: `-- 'npm ci && npm test'`. Several arguments are quoted individually, so `&&` between them is passed literally.
- Processes do not restart on their own. After a crash, check `logs` and run the same `exec --detach` again.
- A new server has nothing installed: runtimes, `node_modules`, databases and `.env` must be set up again. Notes should say which steps that means.
- If `exec` fails with an SSH error, run `devbox status`; the server may have been deleted.
- If `devbox status` shows a server that has been up a long time, remind Anton that it still costs money.

## Verification

- `devbox status NAME` shows the server as running with the expected processes active.
- The preview URL loads.
- The project notes describe how to start it from a fresh server.
