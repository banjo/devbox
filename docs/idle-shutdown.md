# Idle shutdown

- `devbox idle-check` pauses (`idle_action = "pause"`, default) or deletes (`"down"`) servers whose
  last activity is older than the idle timeout (`idle_timeout = "8h"` by default, or per project
  with `project set --idle-timeout`).
- Activity is recorded by the commands a person or agent runs against a server: `up`, `sync` (and
  every `sync --watch` round), `exec`, `ps`, `logs`, `stop`, `ssh`, `expose`, `pull`, `forward`,
  `secrets set/push`, `db dump/restore` and `touch`. Read-only overviews (`status`, `history`,
  `costs`, `snapshot list`, `doctor`) do not count, so an automated loop polling `status` can't keep
  a server alive forever. Browsing the preview doesn't count either; hold the server while testing
  by hand (below).
- `devbox touch NAME --for 2h` holds the server: idle shutdown leaves it alone until the hold ends
  (at most 24h ahead), with any policy (a timeout, `hour-end` or `pause/down --at-hour-end`). After
  that the normal rules apply again, counted from the last real activity, so an idle server is
  stopped at the next check (with `hour-end`, in the window before the next billing hour) instead
  of after another full timeout. `touch NAME --clear` ends the hold early. `status`, `touch`,
  `idle-check` and `autostop status` show when it ends (`hold_until` in `--json`). The hold belongs
  to the server, so a restored server starts without one.
- Running work is never interrupted. Before acting, `idle-check` asks the server whether a
  foreground `exec` is still running (every `exec` runs in its own `devbox-exec-*` scope) or a
  background job started with `exec --detach --keep-alive` is active, and leaves the server alone if
  so, or if it can't check. Ordinary background processes such as dev servers don't keep a server
  alive; use `--keep-alive` for long builds or test runs.
- It only runs when scheduled. The supported way is `devbox autostop`:

  ```sh
  devbox autostop enable          # shows the cron entry it would add
  devbox autostop enable --yes    # test-runs it like cron would, then installs it
  devbox autostop status          # health, coverage and when each server is checked
  devbox autostop disable --yes
  ```

  - The entry runs `idle-check` every 5 minutes with this install's `XDG_CONFIG_HOME`,
    `XDG_STATE_HOME` and a `PATH` that finds `hcloud`, `ssh` and `rsync`, so it covers exactly this
    config, whichever folder you run it from. Output goes to `idle-check.log` in the state folder. A
    marker comment above the entry lets devbox find, replace and remove only its own line; other
    crontab lines are never touched. Several config homes (controllers) each get their own entry.
  - Cron doesn't see your shell's `HCLOUD_TOKEN`, so `enable` requires `hcloud_token_file` in the
    config (a file with only the token, `chmod 600`) and refuses otherwise. The token never appears
    in the crontab or the log.
  - Before installing, `enable --yes` runs `idle-check --dry-run` with a cron-like environment and
    installs nothing if that fails. It warns when `idle_action = "down"` (servers are deleted, not
    paused) and when another idle-check schedule is already installed.
  - Runs never overlap: a run that starts while another is still going is skipped. Every run (not
    `--dry-run`) is recorded in `idle-check.json` in the state folder.
  - `autostop status` (exit 1 unless healthy) reports the schedule, the credentials, the last run,
    success and failure, and for each server the idle timeout (including per-project overrides and
    `off`), when it becomes eligible and the first check after that. A running foreground `exec` or
    `--keep-alive` job postpones the action. It lists which config homes and controllers have
    entries on this machine, and warns about other controllers that have servers in the Hetzner
    project but no entry here. Those servers are only reported, never changed.
  - It is healthy only when the entry exists, the token file works, the last run succeeded and a
    check succeeded within two intervals plus 5 minutes (15 minutes for the 5-minute entry; entries
    from older versions run every 15 minutes and get 35), or the entry is newer than that. A failed
    or overdue check, or a missing token file, counts as not enforced.

  A hand-made schedule still works: a cron entry such as

  ```cron
  */15 * * * * $HOME/.local/bin/devbox idle-check >> $HOME/.local/state/devbox/idle.log 2>&1
  ```

  or the systemd user units in [`contrib/systemd`](../contrib/systemd):

  ```sh
  cp contrib/systemd/devbox-idle-check.* ~/.config/systemd/user/
  systemctl --user enable --now devbox-idle-check.timer
  ```

  For these, devbox can only see that a schedule exists (and whether the last recorded run failed),
  not which config it covers.

- When a server has an idle timeout but idle shutdown is not enforced (no schedule, or an autostop
  entry that is failing or overdue), `status` and `up` print a warning (`idle_enforcement` in
  `status --json`), and `doctor` reports it.
