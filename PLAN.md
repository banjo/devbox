# Devbox Implementation Plan

> **For Hermes:** Use the subagent-driven-development skill to implement this plan task-by-task. This document is a plan, not an implementation or permission to provision resources.

**Goal:** Build a standalone CLI that manages disposable Hetzner development environments while keeping code editing and orchestration lightweight on the Hermes server.

**Architecture:** A Python CLI/library owns policy, SQLite state and recoverable workflows. Hetzner, SSH, S3-compatible backups and Cloudflare are explicit integrations; a thin Hermes skill invokes the same CLI a human would use. Source code and runtime state are stored separately.

**Tech stack:** Python 3.11+ (stdlib argparse, sqlite3, tomllib, subprocess where suitable), official hcloud CLI, OpenSSH, rsync, remote Docker Compose, cloud-init, cloudflared, pytest; a maintained S3 SDK such as boto3 rather than hand-written request signing.

## 1. Scope and agreed decisions

- Project lives at `/root/code/devbox` on the existing Hermes server.
- This first deliverable creates only this plan and a local Git repository. No cloud resources, services, installed skill or executable implementation yet.
- Standalone CLI first; no dependency on Hermes internals or chat history.
- Local-first editing; expensive execution happens on disposable remote servers.
- Hetzner snapshots are the fast-resume mechanism, with separate S3-compatible backups for persistent application data.
- Cloudflare previews are included in v1, using public HTTPS URLs without Cloudflare Access. This supersedes the source prompt's mandatory Access requirement.
- Default idle archival threshold is **3 days**, overridable per project or disabled explicitly.
- At most one existing devbox VM across all projects by default, including powered-off VMs. A second requires explicit approval.
- No first billable deployment until Anton explicitly approves it. No approval is implied by this plan.
- Do not create a GitHub repository or choose its visibility implicitly; initially this is local Git only.

### Recommendations awaiting practical validation

- Local workspace is authoritative. Remote Git clone supplies a clean baseline; sync overlays local uncommitted changes before execution.
- Snapshots plus S3 backups are complementary, not interchangeable.
- No automatic commit/push by default. Code preservation must work without inventing commits.
- Stable project hostname and named Cloudflare tunnel rather than ephemeral preview URLs.

## 2. Source requirements and documentation

Original requirements: https://gist.github.com/banjo/961e2c548c74d249119b0732e8ce28d5

Use the source requirements except where the decisions above deliberately supersede them. Consult current documentation and installed CLI help before implementing provider operations; do not treat example commands, products or prices as current facts.

- Hermes skills: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/
- Hetzner snapshot semantics: https://docs.hetzner.com/cloud/servers/backups-snapshots/overview/
- hcloud: https://github.com/hetznercloud/cli and installed `hcloud --help`.
- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- S3 SDK: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html

Documentation checked during planning confirms that Hetzner snapshots copy server disks and exclude attached Volumes. They do not replace independent database backups. Exact provider commands and a public-tunnel API implementation remain implementation-time verification tasks.

## 3. Ownership, configuration and state

### Filesystem layout

- `/root/code/devbox/`: source, tests, templates, docs and the versioned skill.
- `~/.config/devbox/config.toml`: trusted operator policy and credential references.
- `~/.local/state/devbox/registry.sqlite3`: project registry and operation journal (respect XDG overrides).
- `~/.local/state/devbox/locks/`: global and project locks.
- `~/.local/state/devbox/known_hosts`: isolated SSH host-key registry.
- `~/.local/state/devbox/recovery/`: temporary recovery artifacts, with configurable disk limits.
- Each managed application's `devbox.toml`: declarative runtime settings without secrets.
- Encrypted external backup: consistent registry export, code recovery artifacts and application data as applicable.

CLI owns registry updates. Cloud providers remain authoritative for actual resources; `reconcile` compares registry intent with provider observations. Hermes memory never stores operational state as the source of truth.

### Registry model

Use versioned SQLite migrations, transactions, foreign keys and constrained statuses. Store:

- Projects: immutable UUID, unique name, canonical local path, repo URL, branch and trusted config digest.
- Resources: provider ID, ownership labels, type, region, IPs, SSH identity reference and observed state.
- Operations: UUID, project, kind, stage, intended request fingerprint, external action ID, timestamps and error category.
- Snapshots: image ID, parent operation, creation time, disk size, architecture, readiness and restore-test results.
- Backups: object key/version, manifest, checksum, encryption metadata reference and verification result.
- Runtime jobs: remote job ID, PID/start identity or runner record, heartbeat, lease and exit status.
- Preview mapping, lifecycle, last meaningful activity, last successful save and pricing observations.
- Approval receipts tied to a specific plan/config digest, not a universal unchecked boolean.

Never store API keys, SSH private keys, database passwords or tunnel tokens in the registry or JSON logs. Back up SQLite with its backup API rather than copying a live database file blindly.

### Configuration trust

Repository configuration and scripts are untrusted input. Operator policy caps instance size/count, spending and remote capabilities. A repository cannot authorize public exposure, raise limits or request credential access. Import/approval explicitly records trusted configuration; changes to security-sensitive settings invalidate that approval.

## 4. Code workflow: local primary, remote runtime

1. Register the existing local repository; do not move or rewrite it.
2. Bootstrap a remote project directory from its Git baseline. Use short-lived read-only repository credentials when necessary; never copy a personal GitHub token or forward the host SSH agent.
3. Build a sync manifest including tracked files, eligible untracked files, deletions, commit/branch and content digests.
4. Exclude `.git`, dependency directories, build output and credentials by default. Provision explicitly approved runtime secrets separately.
5. Validate canonical local and remote roots. Preview deletions; never blindly run broad rsync `--delete` on an unchecked directory.
6. Detect remote changes against the last successful sync manifest. Fail on divergent edits rather than silently overwriting either copy.
7. On remote changes, offer a diff and explicit `sync pull`; stage downloaded files separately before applying them locally.
8. Preserve selected dirty/untracked source files, binary changes, deletion manifest and Git metadata to a verified recovery bundle before archiving. Do not depend on a text patch alone.
9. Git commits and pushes remain explicit operations. Never force-push or reset dirty worktrees automatically.

Builds, dependency installation, application servers, Compose and database tools execute remotely. CLI never silently falls back to local execution. Local execution is reserved for management, lightweight validation and bounded streaming/transfer work.

## 5. CLI contract (proposed, not implemented)

All commands support stable exit codes and structured `--json` output. Mutations support a side-effect-free `--dry-run`; provider reads may occur but no writes. Non-interactive invocations fail clearly if approval is missing.

- `devbox init PROJECT --path PATH`: register and validate configuration without provisioning.
- `devbox create PROJECT`: create once and bootstrap; reject duplicates.
- `devbox start PROJECT`: reuse, start a stopped VM, or restore/create when absent.
- `devbox resume PROJECT`: restore the latest eligible snapshot; fail clearly if none exists.
- `devbox status [PROJECT]`: lifecycle, observed resources, backups, snapshots, preview, runtime and estimated costs.
- `devbox sync PROJECT [push|pull]`: manifest-based synchronization with conflict protection.
- `devbox exec PROJECT -- COMMAND [ARG ...]`: tracked remote execution with timeout and exit status.
- `devbox logs PROJECT`: bounded remote application/runner logs.
- `devbox backup PROJECT`: independently back up and verify configured persistent data.
- `devbox save PROJECT`: preserve code/data, stop cleanly, create snapshot; keep VM, mark stopped/saved.
- `devbox stop PROJECT`: graceful service/VM shutdown without a new save; warn it remains billable.
- `devbox archive PROJECT`: save and verify, then delete owned VM; retain persistent resources.
- `devbox destroy PROJECT`: show an exact deletion plan and require explicit confirmation; persistent deletions need separately enumerated approval.
- `devbox reconcile [PROJECT]`: recover interrupted operations and discover only owned resources.
- `devbox maintenance`: deterministic idle/cost/recovery checks for a systemd timer.
- `devbox backup restore-test PROJECT`: restore selected backups to an isolated remote test database.

Validate remote arguments and use a fixed remote runner protocol rather than trusting shell interpolation. General remote execution is intentionally capable of running arbitrary application code, not a claimed security sandbox. Reject management-host targets, localhost/loopback, host IPs, unexpected project roots and unowned cloud resources.

## 6. Lifecycle, idempotency and recovery

Lifecycle: `registered -> provisioning -> bootstrapping -> running -> saving -> stopped -> archiving -> archived`. `restoring` transitions from archived to running; failures record a recoverable operation stage rather than pretending completion.

- Persist operation intent before provider mutations.
- Acquire global capacity/provisioning lock plus project lock in fixed order.
- Label all supported cloud resources with tool owner, project UUID and operation UUID.
- When an API request times out after submission, reconcile by action ID/ownership labels before any retry. Ambiguous outcomes block new provisioning.
- Recover cloud actions already completed if the process died before recording success.
- Never delete unrelated resources or infer ownership from a display name alone.
- Reconcile a successful delete followed by a local crash to archived without issuing unrelated cleanup.

### Save/archive sequence

1. Lock, reconcile and validate server identity; report health and degraded state.
2. Block new remote jobs; wait for active work or require explicit cancellation.
3. Detect and preserve local/remote code differences without overwriting either copy.
4. Quiesce writers and run database-native consistent dumps plus declared file-data backups.
5. Upload encrypted backups and code recovery bundles; verify manifest and read-back checksums.
6. Stop services gracefully, shut down VM, and create a snapshot through the verified provider workflow.
7. Wait for action success/image readiness, then record `ready` (not `restore-tested`).
8. Keep baseline and previous known-good restore point; no automatic persistent deletions without approved retention policy.
9. For `save`, keep the stopped, billable server. For `archive`, delete only the exact owned VM after persistence gates pass; verify deletion before marking archived.
10. Report saved artifacts, restore-test status, deleted resources and remaining storage charges.

Failed backup/snapshot/code preservation blocks deletion. Permit saving an unhealthy application through an explicit recovery path; health failure must not make data rescue impossible. Attached Volumes are unsupported in v1 unless explicit backup/restore hooks cover them; never imply the VM snapshot protects them.

## 7. Snapshots and independent S3 backups

Snapshots capture a stopped root disk, not RAM/process state. Restore restarts services and verifies health. Match architecture and minimum disk constraints; never assume restoration to a smaller machine is supported.

- Track `ready`, `restore-tested`, `failed` and previous eligible snapshot separately.
- Prefer latest restore-tested snapshot; permit a newer ready-only image with an honest warning and fallback path.
- Provide an explicitly approved, billable restore drill to prove recoverability.
- Restore only backups newer than or explicitly paired with a snapshot; never overwrite newer snapshot data with an older dump blindly.

S3 backend is included in v1, with configurable endpoint, region, bucket and prefix. Selection/creation of a real bucket is a separate setup step.

- Start with explicit PostgreSQL dump/restore support and declared file paths; detect other databases and require hooks instead of assuming a generic copy is safe.
- Every persistent dataset must have a declared strategy or be explicitly marked disposable before archival.
- Use bounded streams or direct scoped uploads; do not fill the small Hermes host disk or load dumps into RAM.
- Keep broad S3 credentials on the controller; avoid permanent storage keys in devbox snapshots.
- Use maintained encryption tooling (e.g. age) with a public encryption recipient and recovery-key handling documented before deployment; never invent cryptography.
- Verify an uploaded object by read-back checksum and archive readability; an ETag alone is not a general content checksum. Distinguish integrity verification from an actual database restore test.
- Export registry and code recovery artifacts to external storage. Document how to recover a lost Hermes host using registry backup and cloud labels.
- Automatic maintenance never prunes the only known-good recovery point.

Snapshots may contain application secrets on disk. Minimize persistent secret material, restrict snapshot access and document rotation/reinjection after restore. Never claim snapshots are secret-free without verification.

## 8. Hetzner bootstrap, security and costs

- Dedicated Hetzner project/token; least privilege available for required operations.
- Discover current economical EU server types, images, availability and prices; prefer an economical 2 vCPU/4 GB option without hardcoding obsolete products.
- Ubuntu LTS, Docker Engine/Compose, cloud-init completion checks and non-root application/runner account.
- SSH key-only access restricted to controller addresses, no public database/Docker API ports, no host Docker socket inside untrusted containers.
- Pin/verify SSH host identity through a documented bootstrap mechanism; fail on unexpected changes. Snapshot clones require host-key identity handling, not global StrictHostKeyChecking bypass.
- Do not copy cloud-management credentials onto the devbox. Use scoped preview/repository credentials only where required.
- Resolve live estimates before provisioning and show server, snapshot and any other relevant charges separately, with currency/tax assumptions.
- Configurable maximum instance class, concurrency, monthly warning threshold and optional maximum runtime. Threshold warning is not a provider-enforced hard budget.
- Stopped VMs remain billable; deletion stops ordinary VM charges, not retained storage charges.
- Credentials and command output receive best-effort redaction; arbitrary application output cannot be guaranteed secret-free. Avoid logging sensitive arguments altogether.

## 9. Cloudflare v1: deliberately public previews

- Named per-project tunnel plus explicit hostname under a configured domain; no wildcard DNS.
- Run cloudflared on the devbox with outbound connections; keep application ports private.
- Public HTTPS without Access is approved as the requested v1 design, not permission to expose production or arbitrary services.
- Require trusted per-project `preview.public = true`, an exact hostname/service mapping, and an explicit preview configuration step. Mark status clearly as public/unauthenticated.
- Use synthetic development data. Warn that the URL is not an authentication mechanism; avoid exposing admin/debug endpoints without application protection.
- Archive disconnects the connector but preserves DNS/tunnel configuration for resume. Do not overwrite unrelated DNS records.
- Restore reinjects or rotates scoped connector credentials where needed.
- Verify DNS mapping, tunnel state and HTTPS response before claiming preview availability.
- Cloudflare is optional per project; core CLI works without credentials.
- Domain/zone/account and token scopes remain setup inputs, not guessed values.

## 10. Idle archival and maintenance

- Default `idle_timeout = "3d"`, with per-project override and explicit disable option.
- systemd timer invokes `devbox maintenance`; agent conversation activity is not required.
- Track meaningful CLI activity plus remote runner jobs and heartbeats. Background web servers do not keep a box alive forever.
- Long builds/tests protect against idle archival. Handle stale leases with remote process reconciliation; do not equate a missing heartbeat with permission to delete.
- Preview traffic can optionally count as activity; off by default to avoid crawlers extending lifetime indefinitely.
- Lock and recheck activity immediately before archival to avoid a race with a new job.
- Automatic archival uses exactly the same verified persistence gates as manual archival.
- Report failures, keep the VM and avoid tight repeated retry loops. Keep notifications separate from the CLI core.

## 11. Proposed implementation files

```text
PLAN.md
README.md
pyproject.toml
.gitignore
src/devbox/__init__.py
src/devbox/__main__.py
src/devbox/cli.py
src/devbox/config.py
src/devbox/policy.py
src/devbox/state.py
src/devbox/locks.py
src/devbox/operations.py
src/devbox/reconcile.py
src/devbox/sync.py
src/devbox/remote.py
src/devbox/backups.py
src/devbox/snapshots.py
src/devbox/maintenance.py
src/devbox/costs.py
src/devbox/providers/hetzner.py
src/devbox/providers/cloudflare.py
src/devbox/providers/s3.py
src/devbox/templates/cloud-init.yaml
src/devbox/templates/remote-runner.py
examples/devbox.toml
examples/config.toml
contrib/systemd/devbox-maintenance.service
contrib/systemd/devbox-maintenance.timer
skills/hetzner-devbox/SKILL.md
skills/hetzner-devbox/references/workflows.md
docs/architecture.md
docs/recovery.md
docs/security.md
tests/conftest.py
tests/test_cli.py
tests/test_config.py
tests/test_policy.py
tests/test_state.py
tests/test_lifecycle.py
tests/test_reconcile.py
tests/test_sync.py
tests/test_remote.py
tests/test_backups.py
tests/test_snapshots.py
tests/test_cloudflare.py
tests/test_maintenance.py
tests/test_costs.py
```

Split files only when warranted; these are component boundaries, not a requirement for empty abstractions. No generic provider/plugin framework or daemon in v1.

## 12. Ordered implementation work packages

These packages are sequencing boundaries. Implement each as small test-first changes: write a failing test, run it, add minimal implementation, run focused tests and commit. Expectations below are future acceptance criteria, not claimed test results.

### A. Read-only preflight and contracts

Inspect actual OS/resources, Python, Git, SSH/rsync/hcloud versions, Hermes skill discovery and existing credentials by names/presence only. Verify current official command/API semantics. Record findings in `docs/architecture.md`, including snapshot readiness and billable resource behavior. Do not touch unrelated infrastructure.

### B. Package and CLI skeleton

Create `pyproject.toml`, `src/devbox/{__init__,__main__,cli}.py`, `tests/test_cli.py`. Verify `python -m pytest tests/test_cli.py -q`, editable installation in an isolated environment, `devbox --help`, JSON errors and exit codes. No cloud calls for help/config validation.

### C. Configuration, trust and policy

Create `config.py`, `policy.py`, example TOML and focused tests. Validate schemas, paths, three-day default/project override, public-preview opt-in and policy precedence. Reject repository attempts to elevate policy or select management credentials.

### D. Registry and locking

Create `state.py`, `locks.py`, registry migration tests and subprocess concurrency tests. Prove atomic updates, rollback, consistent registry export and global/project lock ordering. Race two projects against a single-VM capacity limit.

### E. Provider adapter and dry-run

Create `providers/hetzner.py` with an injected command runner and fake hcloud executable. Parse actual documented JSON contracts, bound timeouts and verify resource ownership. Test uncertain POST outcomes, no duplicate retries and zero writes in dry-run.

### F. Operations and reconciliation

Create `operations.py`, `reconcile.py`, lifecycle tests. Simulate crashes before/after each external mutation, then restart against persistent fake provider state. Prove recovery without duplicate creation and exact-target deletion.

### G. Bootstrap and remote execution

Create cloud-init, remote runner and `remote.py`. Test host validation, SSH key mismatch, timeout, exit propagation, disconnect/reconnect and long-job tracking. Include injection/path traversal tests. No command may fall back to the local machine.

### H. Git baseline and conflict-aware sync

Create `sync.py` and tests using isolated local Git repos/fake remote directories. Cover dirty/untracked/binary files, deletions, excluded credentials, remote divergence, staged pull and resumable code recovery bundles. Prove no force push or implicit commit.

### I. Remote Compose and health

Implement explicit start/stop/log/health actions through the runner, and test Compose failures, unhealthy applications and bounded logs. Host never runs a real application build or database during tests.

### J. S3 backups and recovery

Create `backups.py`, `providers/s3.py`, database/file hooks and tests. Verify encryption invocation, streaming limits, multipart interruption, read-back hash failure, unreadable archives and registry recovery. Archive fails closed on undeclared persistent data or missing/unverified backup.

### K. Snapshot save/restore/archive

Create `snapshots.py` and lifecycle coverage. Test ready-only vs restore-tested images, architecture/disk mismatch, interrupted image creation, clean shutdown, backup/snapshot failure, fallback retention and crash after VM deletion. Stopped state is not archived.

### L. Cloudflare previews

Create `providers/cloudflare.py` and tests with mocked API/connector. Cover hostname ownership, no wildcard records, public-mode approval, existing unrelated DNS, unavailable tunnel, reconnection and verified HTTPS status. No real DNS writes during unit tests.

### M. Idle and cost management

Create `maintenance.py`, `costs.py`, systemd templates and tests with an injected clock. Cover three-day boundary, overrides/disable, active jobs, stale leases, new-job race, stopped-VM billing, cost warnings and failed persistence. Timer installation is an explicit setup step.

### N. Skill, documentation and full offline verification

Create the thin versioned Hermes skill and operator/recovery docs. Skill invokes CLI only; no duplicated lifecycle logic or global terminal backend change. Document explicit installation into the active profile without modifying another profile. Run the full mocked suite and dry-run walkthrough; report exact results and remaining limits.

### O. Explicitly approved live acceptance

Only after approval: show current estimated spend, chosen instance/region, backup location, hostname and planned resource changes. Create one devbox, sync dirty code, start a sample app, verify public HTTPS, write persistent test data, back up/save/archive, confirm VM deletion, restore, check data/code/preview and archive again. A second restore VM must not bypass the one-VM limit. Report residual snapshots/DNS/storage and costs; do not delete persistent artifacts without approval.

## 13. Required acceptance evidence

- Unit/integration tests with mocked cloud services run without real tokens or provider writes.
- Real subprocess concurrency proves no duplicate servers across projects.
- Failure injection demonstrates recoverability for every persistence/destruction boundary.
- Dry-run leaves provider state and project/runtime state unchanged (diagnostic logs may be written separately).
- Command-routing tests prove remote failure cannot execute heavy commands locally.
- Code sync/recovery tests cover uncommitted and remote-only work without data loss.
- Backup integrity and actual restore success are reported separately.
- Public preview creation/restore and exact provider deletions are verified by reading the target back.
- Independent recovery instructions exist for loss of the Hermes controller.
- Final report distinguishes implemented, mocked-tested, live-tested and unimplemented features; no fabricated test output.

## 14. Inputs needed before live setup (not blockers for planning)

- Dedicated Hetzner project/token and SSH identity.
- Cloudflare account/zone/domain, hostname convention and scoped API credentials.
- S3 endpoint/bucket/prefix, credential reference and encryption recipient/recovery key ownership.
- Initial sample project, runtime secrets, health endpoint, persistent datasets and backup hooks.
- Monthly warning amount/currency and maximum approved instance class.
- Explicit approval for the first billable test and any required external account changes.

## 15. Non-goals for v1

No general IDE, agent hosting platform, Kubernetes, arbitrary provider marketplace, production deployment controller, multi-controller HA or automatic database-engine inference claimed as safe backup. The project must remain a small, composable management tool rather than a new cloud platform.
