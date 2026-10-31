# Devbox Implementation Plan

> **For Hermes:** Use the subagent-driven-development skill to implement this plan milestone by milestone. This document is a plan, not permission to provision billable resources.

**Goal:** A small CLI, `devbox`, that creates, uses, saves, deletes and restores temporary Hetzner development servers, plus a thin Hermes skill that calls it. Code is edited on the Hermes host; anything heavy runs on the devbox. Hetzner snapshots make the next session fast and cheap.

**Original requirements:** https://gist.github.com/banjo/961e2c548c74d249119b0732e8ce28d5. This plan follows them except where section 1 says otherwise.

**Tech stack:** Python 3.11+ using only the standard library (`argparse`, `sqlite3`, `tomllib`, `subprocess`, `urllib`, `fcntl`). External tools are called as subprocesses: `hcloud`, `ssh`, `rsync`, `age`, `rclone`. Tests use `pytest` and fake executables. No Python runtime dependencies, so the 4 GB Hermes host stays light.

## 1. Decisions

| Topic | Decision | Differs from gist? |
|---|---|---|
| Shape | Standalone CLI in `/root/code/devbox` (GitHub: `banjo/devbox`, private). The Hermes skill is a thin wrapper that only calls the CLI. | Gist put the script inside the skill folder |
| State paths | `~/.config/devbox/` for config and secrets, `~/.local/state/devbox/` for the registry and logs | Gist suggested `~/.hermes/devboxes/` |
| Code transfer | Hermes is the source of truth. Code is pushed with rsync; the devbox never clones from GitHub and holds no Git credentials. | Gist cloned on the devbox |
| Git | No automatic commit or push. Remote-only changes are pulled into a staging folder, never committed by the tool. | Gist allowed commit/push to a dev branch |
| `save` | Backup + snapshot. Afterwards the VM is powered back on (or left off with `--off`). | Gist marked the project archived |
| `archive` | `save`, then delete the VM. This is "Vi är klara för idag". | Same |
| Idle timeout | 3 days by default, configurable per project or disabled | Gist said 60 minutes |
| Previews | Cloudflare Tunnel with a public HTTPS hostname, no Cloudflare Access. Each project must opt in, in operator config. | Gist required Access |
| VM limit | At most one devbox VM in total, powered-off VMs included. A second one needs `--allow-second-vm`. | Same |
| Databases | PostgreSQL dumps are built in. Other data uses declared file paths or a custom dump/restore command. | Narrower than "detected databases" |
| Volumes | Hetzner Volumes are not supported in v1. Data lives on the root disk and in S3 backups. | Gist said not to assume Volumes are in snapshots |
| Live tests | No billable resource is created until Anton approves it explicitly | Same |

## 2. How it works

```text
Hermes host (4 GB VPS)                      Hetzner devbox (disposable)
- devbox CLI + SQLite registry              - Ubuntu LTS, Docker + Compose
- project source (authoritative)  --rsync-> - /srv/app (copy of source)
- secrets (age-encrypted)         --ssh---> - /srv/app/.env (tmpfs-backed, removed before snapshot)
- age + rclone ---------> S3 <---- pg_dump/tar streamed over ssh, encrypted on Hermes
- hcloud CLI ----------> Hetzner API (servers, snapshots, firewall, ssh keys, all labeled)
- Cloudflare API ------> tunnel + DNS          cloudflared (outbound only) -> app port
```

- Every Hetzner resource the tool creates gets the labels `managed-by=devbox` and `devbox-project=<project-uuid>`. The tool only ever reads, changes or deletes resources that carry both labels.
- One Hetzner Cloud Firewall (`devbox`, labeled) allows inbound SSH only from the Hermes host's IP. No other ports are open; previews leave the devbox outbound through cloudflared.
- S3 credentials, the Hetzner token and the Cloudflare token stay on Hermes. The devbox only gets the app's runtime secrets and its own tunnel token.

## 3. Configuration

### Operator config: `~/.config/devbox/config.toml` (trusted)

```toml
[hetzner]
token_file = "~/.config/devbox/hcloud.token"   # mode 600
location = "fsn1"                              # fallback: nbg1
default_server_type = "cx23"                   # validated against `hcloud server-type list` at run time
max_server_type = "cx33"
image = "ubuntu-24.04"

[limits]
max_vms = 1
monthly_warning_eur = 20
max_runtime_warning = "7d"
default_idle_timeout = "3d"
allow_billable = false        # Anton flips this after approving the first live test

[s3]
remote = "devbox-backups:"    # rclone remote name; credentials live in rclone config
bucket = "devbox-backups"
age_recipient = "age1..."     # public key; the private key is kept offline

[cloudflare]
api_token_file = "~/.config/devbox/cloudflare.token"
account_id = "..."
zone = "example.com"

[projects.crm]
path = "/root/code/crm"
idle_timeout = "3d"
preview_public = true         # only operator config can turn public previews on
```

### Project config: `devbox.toml` in the repository (untrusted)

```toml
compose_file = "compose.yaml"
health_url = "http://localhost:3000/health"
exclude = ["node_modules", "dist", ".next"]

[preview]
service_port = 3000
hostname = "crm-dev"          # becomes crm-dev.<zone>

[[backup]]
type = "postgres"
service = "db"
database = "crm"

[[backup]]
type = "files"
path = "uploads"

[secrets]
required = ["DATABASE_URL", "SESSION_SECRET"]
```

The repository config describes how to run the app. It cannot raise limits, choose a server type above the operator maximum, enable public previews, or point at credentials. Unknown keys are an error.

### Secrets

The values are stored on Hermes in `~/.config/devbox/secrets/<project>.env.age`. On `start`/`resume` they are decrypted in memory and written over SSH to `/run/devbox/<project>.env` on the devbox (tmpfs), where Compose reads them. `/run` is cleared on shutdown, so snapshots do not contain them. The tunnel token is handled the same way.

## 4. Registry

SQLite at `~/.local/state/devbox/registry.sqlite3`, with a schema version and simple numbered migrations.

- `projects`: uuid, name, local path, git remote and branch (informational), status, idle timeout, last activity, current server id, server type, location, ipv4, preview hostname.
- `operations`: uuid, project, kind, stage, started/finished, error. Written before each provider call and updated after it.
- `snapshots`: hetzner image id, project, operation uuid, created, size, `ready` / `restore_tested`, role (`baseline` / `latest` / `previous`).
- `backups`: project, operation uuid, object key, sha256, size, kind, verified.
- `jobs`: detached remote jobs (systemd unit name, started, last seen).

No secrets in the registry. `devbox registry export` writes a consistent copy (SQLite backup API), which `archive` uploads to S3 next to the backups so the registry can be rebuilt if the Hermes host is lost.

**Locking:** every mutating command takes one global `flock` (`~/.local/state/devbox/lock`). With at most one VM, running all mutations one at a time is enough and rules out duplicate servers. Read-only commands (`status`, `logs`) do not lock.

## 5. Lifecycle

States: `registered`, `running`, `stopped`, `archived`, `failed`, `destroyed`.

| Command | From | To | What happens |
|---|---|---|---|
| `init` | – | registered | Validate path and both configs. No cloud calls. |
| `create` | registered | running | Estimate cost, create SSH key, firewall and server with cloud-init, wait for cloud-init, push code, write secrets, `compose up`, health check. A `baseline` snapshot is taken on the first successful `archive`. |
| `start` | registered | running | Same as `create`. |
| `start` | stopped | running | Power on, push code, write secrets, `compose up`, health check. |
| `start` / `resume` | archived | running | Create a server from the latest `ready` snapshot, then as above. If health passes, mark the snapshot `restore_tested`. |
| `start` | running | running | Reuse. Never create a second server. |
| `stop` | running | stopped | `compose down`, power off. Warns that a stopped server is still billed. |
| `save` | running/stopped | same | See section 6. |
| `archive` | running/stopped | archived | `save`, then delete the server and verify it is gone. |
| `destroy` | any | destroyed | Prints the exact list of resources; needs `--confirm <project>`. Snapshots, backups and DNS/tunnel are only deleted with `--delete-snapshots`, `--delete-backups`, `--delete-preview`. |

Any step that fails moves the project to `failed` with the operation stage recorded. Running `reconcile` (or the next mutating command, which runs it first) works out what really happened.

**Reconcile:** list all labeled servers and images in Hetzner and compare them with the registry and any unfinished operation. Typical cases:
- Server created but not recorded: adopt it.
- Snapshot created after a crash: find it by its `devbox-operation` label and record it instead of creating a new one.
- Server deleted after a crash: mark the project archived.
- Unexpected labeled server: report it and block provisioning; never delete it automatically.

Provider calls that time out are never retried blindly. Reconcile first.

## 6. Save, archive and restore

`save`:
1. Lock, reconcile, check the server is reachable. If the app is unhealthy, continue only with `--force-unhealthy`, so data can still be rescued.
2. Refuse if a detached job is running, unless `--cancel-jobs` is given.
3. Compare remote files with the manifest from the last push. Remote-only changes are copied to `~/.local/state/devbox/recovery/<project>/<timestamp>/` and listed. Local files are never overwritten.
4. For each `[[backup]]`: `pg_dump -Fc` (via `docker compose exec`) or `tar` of the path, streamed over SSH, through `age` on Hermes, straight into `rclone rcat`. Nothing is written to Hermes's disk.
5. Verify every backup: `rclone cat` it back, compare sha256, decrypt and run `pg_restore --list` or `tar -t`. If any backup fails, stop here; the server is not touched.
6. `compose down`, power off, create a snapshot labeled with the operation uuid, and wait until it is `available`.
7. Record the snapshot as `latest` and move the old `latest` to `previous`. Delete snapshots older than that only if they are neither `baseline` nor the newest `restore_tested` snapshot. The only known-good restore point is never deleted.
8. Power the server back on unless `--off` was given.

`archive` is `save --off`, then registry export to S3, then server delete, then verifying the server is gone. If any step before the delete fails, the server stays and the error is reported.

**Restore** (`resume`, or `start` on an archived project): create a server from the newest `ready` snapshot. If it fails to come up healthy, offer the previous snapshot. Data in the snapshot already matches its backups (they were taken in the same operation), so backups are only restored with `devbox backup restore PROJECT --from <operation>`, for example after a broken database.

## 7. Remote execution

- `devbox exec PROJECT [--timeout 30m] [--detach] -- CMD ARGS...` runs over SSH as the `dev` user in `/srv/app`. Arguments are quoted with `shlex.join`; the project and server come from the registry, never from the arguments.
- SSH runs with `-o StrictHostKeyChecking=yes` and a project-specific `known_hosts`. The host key is generated by Hermes per project and injected through cloud-init (`ssh_keys`), so it is known before the first connection and stays the same after a snapshot restore.
- The SSH target must be a server whose id and IP match the registry and that carries the project's labels. Loopback, the Hermes host's own IPs and unknown hosts are rejected.
- If SSH fails, the command fails with a clear error and a non-zero exit code. There is no code path that runs the command locally.
- `--detach` starts the command with `systemd-run --unit devbox-job-<id>` on the devbox, so long builds survive a dropped connection and the idle check can see them.
- `devbox logs PROJECT [--service X] [--lines 200]` runs `docker compose logs --tail`.
- `devbox sync PROJECT` pushes code without restarting anything. `devbox pull PROJECT` copies remote-only changes into the recovery folder (see section 6).
- All commands write one JSON line per operation to `~/.local/state/devbox/log.jsonl`, with secret values masked. All commands support `--json` and `--dry-run` (reads allowed, no writes to provider, registry or devbox).

## 8. Cost management

- Prices come from `hcloud server-type list -o json` and the snapshot price, not hardcoded.
- `create`/`start`/`resume` print the hourly and monthly estimate before provisioning. If `allow_billable = false` they stop there.
- `status` shows uptime, cost so far this month (from registry operation timestamps), snapshot storage cost, and warns when the server is stopped but still billed.
- Warnings for runtime over `max_runtime_warning` and for an estimated month over `monthly_warning_eur`. These are warnings, not a hard budget.
- Server types above `max_server_type` are refused.

## 9. Idle archival

- A systemd timer runs `devbox maintenance` every 15 minutes.
- Activity is any devbox command for the project, or a detached job that `systemctl is-active` reports as running.
- When a project has been inactive longer than its `idle_timeout`, maintenance runs `archive` with exactly the same checks as the manual command.
- If archive fails, the server is kept, the failure is logged, and maintenance waits 24 hours before trying that project again.
- `idle_timeout = "off"` disables it per project.

## 10. Cloudflare previews

- Optional. Without `[cloudflare]` config, everything else works.
- On first `start` with `[preview]`: create a remotely managed tunnel `devbox-<project>` through the API, set its ingress to `http://localhost:<service_port>`, and create a proxied CNAME `<hostname>.<zone>`. Refuse if that DNS name already exists and was not created by devbox (tracked in the registry). No wildcard records.
- Refuse unless `preview_public = true` is set for the project in operator config. `status` shows the URL marked `PUBLIC, no authentication`.
- The tunnel token is written to the devbox like other secrets, and cloudflared runs as a systemd service there.
- `archive` leaves the tunnel and DNS in place; the connector stops with the VM. `resume` reconnects it.
- `start` reports the preview as working only after an HTTPS request to the hostname succeeds.
- The Cloudflare token needs only Tunnel edit on the account and DNS edit on the zone.

## 11. Hermes skill

`skills/hetzner-devbox/SKILL.md` in this repository, symlinked to `~/.hermes/skills/hetzner-devbox/`. It contains:

- **When to start a devbox:** only to run things (Compose, servers, databases, production builds, integration tests, large installs) or when the user asks. Editing, reading code, Git and small checks stay local.
- **Never run heavy commands on the Hermes host.** Use `devbox exec` instead.
- **Ask Anton first** before using `--allow-second-vm`, `--force-unhealthy`, `--delete-*`, `destroy`, or changing `allow_billable` / `preview_public`.
- **Phrase mapping:**

| Anton says | Hermes runs |
|---|---|
| "Starta utvecklingsmiljön för CRM" | `devbox start crm`, then gives the preview URL |
| "Ändra layouten på CRM-dashboarden" | Edits locally, no devbox command |
| "Starta appen så jag kan testa ändringarna" | `devbox start crm` (pushes code), then gives the preview URL |
| "Vi är klara för idag" | `devbox archive crm`, then reports what was saved and the remaining storage cost |
| "Fortsätt med CRM" | `devbox start crm` (restores from snapshot) |

## 12. Files

```text
PLAN.md
README.md
pyproject.toml
src/devbox/__main__.py
src/devbox/cli.py          # argparse, --json, --dry-run, exit codes
src/devbox/config.py       # operator + project config, trust rules
src/devbox/registry.py     # SQLite schema, migrations, export, lock
src/devbox/hcloud.py       # hcloud subprocess wrapper, labels, prices
src/devbox/remote.py       # ssh, rsync, exec, logs, secrets push
src/devbox/backup.py       # dump -> age -> rclone, verification, restore
src/devbox/lifecycle.py    # create/start/stop/save/archive/resume/destroy/reconcile
src/devbox/cloudflare.py   # tunnel + DNS via urllib
src/devbox/maintenance.py  # idle check, cost warnings
src/devbox/templates/cloud-init.yaml
skills/hetzner-devbox/SKILL.md
contrib/systemd/devbox-maintenance.{service,timer}
examples/config.toml
examples/devbox.toml
docs/setup.md              # tokens, rclone, age keys, firewall IP, install
docs/recovery.md           # rebuilding Hermes state from S3 + labels
tests/fakes/hcloud         # fake hcloud executable backed by a JSON file
tests/fakes/ssh            # fake ssh/rsync that run against a temp directory
tests/test_*.py
```

## 13. Milestones

Each milestone is test-first and ends with a commit. Nothing before milestone 7 calls a real provider.

1. **Preflight and skeleton.** Check Python, hcloud, rsync, age and rclone on Hermes; read the current Hermes skill docs and `hcloud --help`. Write findings to `docs/setup.md`. Create the package, CLI with `--help`, `--json`, exit codes, config loading and trust rules, registry with migrations and lock.
2. **Hetzner adapter and reconcile.** The `hcloud` wrapper with labels, JSON parsing and timeouts; the fake hcloud executable; dry-run; reconcile including crash-after-create and crash-after-snapshot cases.
3. **Create, start, stop, status, exec, logs, sync.** Cloud-init template (Docker, `dev` user, firewall, host key), SSH and rsync, secrets push, Compose up/down, health check, detached jobs, cost estimate.
4. **Backups, save, archive, resume, destroy.** The backup pipeline and verification, snapshot rotation, delete gating, restore from snapshot with fallback, destroy confirmation.
5. **Cloudflare previews.** Tunnel and DNS creation, ownership checks, connector service, HTTPS check.
6. **Maintenance, skill and docs.** Idle archival, cost warnings, systemd units, SKILL.md, README with the Swedish phrase examples, setup and recovery docs. Run the full test suite and a dry-run walkthrough.
7. **Live acceptance (only after Anton approves).** Show the estimated cost, server type, location, bucket and hostname first. Then: create, push dirty code, start a sample app with Postgres, check the public URL, write data, archive, confirm the server is gone, resume, check code, data and URL, archive again. Report the remaining snapshots, backups, DNS records and their monthly cost.

## 14. Tests

All run offline with the fake executables. Required cases from the gist, plus the ones this design adds:

- First-time create; start on a running project reuses the server
- Restore from snapshot; fallback to the previous snapshot when the newest is unhealthy
- No duplicate servers: two `start` calls in parallel processes, and two projects against `max_vms = 1`
- SSH connection failure; host key mismatch; remote execution never falls back to localhost
- Compose/Docker build failure; unhealthy app blocks save without `--force-unhealthy`
- Backup failure and checksum mismatch block snapshot and delete
- Snapshot creation failure; crash after snapshot (found by label, not duplicated)
- Interrupted archive at each stage, then reconcile
- Destroy without `--confirm` deletes nothing; snapshots and backups survive without `--delete-*`
- Rotation never removes the baseline or the newest restore-tested snapshot
- Idle: exactly 3 days, override, `off`, running detached job, failed archive backs off
- Repository config cannot enable public preview or raise limits
- Dry-run changes nothing in the fake provider, registry or remote directory
- Secret values do not appear in logs or the registry

## 15. Needed before live setup

- Dedicated Hetzner project and API token
- S3 bucket and rclone remote; age key pair, with the private key stored offline
- Cloudflare account id, zone, and a scoped API token
- A sample project with `devbox.toml`, health endpoint and secrets
- Monthly warning amount and maximum server type
- Anton's explicit approval for the first billable test

## 16. Not in v1

Two-way live sync, Hetzner Volumes, databases other than PostgreSQL without a custom hook, restore drills on a separate VM, Cloudflare Access, notifications, hard spending limits, more than one controller, production deployments.
