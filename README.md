<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/devbox-logo-dark.svg">
    <img src="assets/devbox-logo.svg" height="80" alt="devbox">
  </picture>
</h1>

<p align="center">
  Disposable dev servers for coding agents, on your own cloud account, for about a cent an hour. Hetzner Cloud today.
</p>

<p align="center">
  <a href="docs/installation.md"><b>Install</b></a> ·
  <a href="docs/commands.md"><b>Commands</b></a> ·
  <a href="docs/how-it-works.md"><b>How it works</b></a> ·
  <a href="docs/costs.md"><b>Costs</b></a> ·
  <a href="skills/hetzner-devbox/SKILL.md"><b>Agent skill</b></a>
</p>

Your agent edits code on your machine. devbox starts a real server in your own cloud project,
copies the code there, and runs builds, tests, databases and dev servers on it. Your laptop stays
light, and when the work is done the server is paused or deleted.

There is no platform in between and no subscription. The servers, snapshots and API token are yours,
and you pay the provider's hourly price directly. On Hetzner Cloud, a small `cx` server with 2 vCPUs and 4 GB of memory
costs around €0.01 an hour, and every server is capped at its monthly price. Run
`devbox server-types` to see the current prices.

> [!NOTE]
> devbox is early software. Hetzner Cloud is the only supported provider for now; see [#35](https://github.com/banjo/devbox/issues/35) and the [backlog](https://github.com/banjo/devbox/issues/41).

## Highlights

- 💶 Pay the provider's hourly price with no markup. devbox tracks every server and snapshot, can stop at
  a monthly budget, and uses the hour you already paid for before shutting down
- 🔑 Your own account: servers, SSH keys, firewalls and snapshots live in your own cloud project
- 📦 `devbox up` reuses the running server or creates one, from the latest snapshot if there is one
- 🔁 Syncs exactly the files Git sees, once or on every save
- 🧵 Commands, background processes, logs, port forwarding and public preview URLs, which the app
  can read from `$DEVBOX_PREVIEW_URL`
- 🗄️ Dump and restore project databases (Postgres, SQLite)
- 💤 Pauses idle servers to a snapshot, without stopping work that is still running; `touch --for 2h`
  holds a server while you test it
- 🤖 `--json` on every command, made to be driven by an agent

## Getting started

Install [`uv`](https://docs.astral.sh/uv/) and [`hcloud`](https://github.com/hetznercloud/cli), save
a Hetzner Cloud API token, then install the CLI:

```sh
git clone https://github.com/banjo/devbox && cd devbox
uv tool install -e .
devbox doctor                                  # Check the config, token and tools.
```

Then start a server for a project:

```sh
devbox project add myapp --path ~/code/myapp   # Register a project.
devbox up myapp                                # Start a server and sync the code.
devbox exec myapp -- 'npm ci && npm test'      # Run a command on it.
devbox expose myapp 3000                       # Get a public preview URL.
devbox pause myapp                             # Snapshot the server and delete it.
```

See [Installation](docs/installation.md) for the full setup, including the agent skill.

## Documentation

- [Installation](docs/installation.md): tools, API token, config and the agent skill
- [Commands](docs/commands.md): every command and the `--json` output
- [How it works](docs/how-it-works.md): state, labels, server choice, sync and startup
- [Development loop](docs/development-loop.md): watch mode, pulling files, port forwarding, secrets
- [Previews](docs/previews.md): public URLs, stable domains and password protection
- [Databases](docs/databases.md): dumping and restoring Postgres and SQLite
- [Snapshots](docs/snapshots.md): pause and resume
- [Idle shutdown](docs/idle-shutdown.md): what counts as activity, and `devbox autostop`
- [Costs](docs/costs.md): cost tracking, budgets and using the paid hour
- [Housekeeping](docs/housekeeping.md): `doctor`, `cleanup` and `history`

## License

[MIT](LICENSE)

## Development

```sh
uv run --with pytest pytest
```
