# Housekeeping

- `devbox doctor` runs read-only checks and exits 1 if something blocks devbox from working (for
  example an SSH key conflict or no `hcloud`). With `preview_domain` set it also checks the Cloudflare
  token, the zone and access to Cloudflare Tunnel; DNS edit rights are only tested by the first
  `expose`. Idle shutdown counts as scheduled for a healthy
  `autostop` entry for this config, an uncommented cron entry that runs `devbox idle-check`, or a
  systemd user timer that is both enabled and active.
- `devbox cleanup` lists what devbox created that is no longer used: labeled servers and snapshots
  that aren't in the registry, devbox firewalls not attached to any server (including the old
  `devbox` firewall), devbox SSH keys other than the one in use, preview tunnels and their DNS
  records of removed projects (with `preview_domain`), and stale local host-key and sync files. Only resources labeled `managed-by=devbox` without another install's controller label are
  considered (including snapshots made by older versions). The SSH key devbox uses is never listed;
  if it can't tell which key is in use (for example the local key is also registered by someone
  else), it skips SSH keys and says why. Nothing is deleted without `--yes`.
- `devbox history` lists every server (newest first) with type, location, run time, cost, activity
  and whether it was restored from or paused into a snapshot, plus all snapshots and the overall
  total. It works offline from the registry.
