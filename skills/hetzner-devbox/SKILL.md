---
name: hetzner-devbox
description: Renamed to the `devbox` skill; run projects on temporary servers or own hosts via devbox
version: 0.2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hetzner, devbox, remote, docker, preview]
    category: devops
    requires_toolsets: [terminal]
---

# hetzner-devbox (renamed: `devbox`)

This skill is now called **`devbox`** (`skills/devbox/SKILL.md` in the devbox repository). It covers temporary Hetzner servers and workspaces on machines you own. Load the `devbox` skill and follow it. This file stays so existing installs that point at `skills/hetzner-devbox` keep working; switch the link to `skills/devbox` when convenient and this one can go.

If the `devbox` skill is not installed yet, these rules are enough to work safely until it is:

- Use the `devbox` CLI for anything heavy (installs, builds, Docker, databases, dev servers, tests). Never run those on the local machine. Run `devbox --help` for the commands.
- `devbox status NAME` first. `devbox up NAME` creates a billed server: ask before creating one and pick the cheapest type that fits (`devbox server-types`). `devbox up NAME --host HOST` starts a free container on a registered own host.
- `exec`, `exec --detach --name P`, `ps`, `logs`, `sync`, `pull`, `expose NAME PORT --wait-for /`, `secrets`, `db` work the same on both. `--json` for parsing; every command ends with `next:` guidance.
- Prefer `pause` over `down`; ask before `down`, `host bootstrap --yes`, `cleanup --yes`, `--additional` or `--over-budget`. Previews are public unless `--protect`.
