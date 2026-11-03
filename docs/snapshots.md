# Snapshots, pause and resume

- `pause` (or `down --snapshot`) first cleans up what is safe to lose (apt cache, journal beyond
  50 MB, dangling Docker images; stopped containers and the build cache stay) and discards free disk
  blocks, so the snapshot is smaller and restores faster. If that fails, it snapshots as is. Then it
  shuts the server down cleanly, saves its disk as a Hetzner snapshot and deletes the server. If the snapshot fails, the server is kept and powered on again.
  If the snapshot succeeds but deleting the server fails, the server stays shut down (and billed),
  `status` shows it as powered off, and running `pause` again finishes the job with the same
  snapshot; `up` powers it back on instead.
- devbox records each server's power state from Hetzner. Commands that need a running server refuse
  with a clear message when it is powered off.
- The next `up` creates the server from the latest snapshot, so installed runtimes, `node_modules`,
  Docker images and database data are already there. Background processes and previews are not
  running after a restore; start them again. `up --fresh` ignores snapshots; `resume` refuses to
  start without one.
- The restored server must have the snapshot's architecture and at least its disk size; `up` checks
  this.
- `keep_snapshots` (default 1) snapshots are kept per project. Older ones are deleted after a new
  snapshot is available, never before.
- Snapshots cost money while they exist (per GB per month; the price comes from Hetzner's pricing
  API). `status`, `snapshot list` and `costs` show it. `down` without `--snapshot` keeps existing
  snapshots; delete them with `snapshot delete`. A project with snapshots can't be removed until
  they are deleted.
