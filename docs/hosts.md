# Own hosts and Incus workspaces

A host is a machine you already own (a mini PC, a home server, a VM) that devbox reaches over SSH.
On it, every project runs in its own unprivileged [Incus](https://linuxcontainers.org/incus/) system
container, a **workspace**. devbox never creates, powers off, snapshots or deletes the machine, and
it costs nothing: there is no billing, so `costs`, billing hours and `--at-hour-end` do not apply.

Hetzner servers keep working exactly as before. Choose per project with `--host`.

```text
devbox host add NAME --ssh USER@HOST [--key PATH]   register a host and pin its SSH host key
devbox host bootstrap NAME [--yes]                  show (then apply) what the host needs
devbox host list                                    hosts and the workspaces on them
devbox host remove NAME [--force]                   forget a host; the machine is never touched
devbox up PROJECT --host NAME [--cpus N --memory SIZE]
```

## Add a host

```sh
devbox host add mini --ssh dev@mini
```

`--ssh` is anything `ssh` understands, including a name from your `~/.ssh/config`. devbox reads the
host key the machine presents and shows its fingerprint. It is pinned only after you confirm:

- interactively, answer `y`; or
- compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the machine and pass
  `--fingerprint SHA256:...` (pinned only if it matches), or `--yes` to accept what was shown.

From then on every connection to the host, and every jump through it to a workspace, runs with
`StrictHostKeyChecking=yes` against that key only. If the key changes, devbox refuses. The key
lives in the registry (`hosts` table in `devbox.db`) next to the other state; there is no separate
config file for hosts. `--key` names an identity file for the host; without it your ssh config and
agent decide.

## Bootstrap

`devbox host bootstrap NAME` inspects the host read-only and prints a plan. Nothing changes until
`--yes`, the same pattern as `autostop enable`. It is idempotent: when everything is in place it says
`nothing to do`.

What it checks and may do:

1. **Incus**: installs the `incus` package from the Ubuntu/Debian archive (`apt-get`, needs
   `sudo -n`) and runs `incus admin init --minimal` (bridge `incusbr0`, `dir` storage) when it is
   missing or uninitialised.
2. **Docker + UFW**: on a host with Docker, `FORWARD` is `DROP`, and UFW blocks the Incus bridge
   as well, so containers get no IPv4 and no internet. If UFW is active, bootstrap adds only the
   missing ones of `ufw allow in on incusbr0`, `ufw route allow in on incusbr0` and
   `ufw route allow out on incusbr0`. It never edits Docker's own rules. If `FORWARD` is `DROP` but
   UFW is not active, it reports the problem and leaves the firewall to you (`manual:` line).
3. **Access**: the SSH user needs to be in the `incus-admin` group or have passwordless `sudo -n`.
   devbox uses the group when it can and `sudo -n incus` otherwise. If neither works, bootstrap
   says so and changes nothing. It never writes global files on the host.

## Workspaces

`devbox up PROJECT --host NAME` is an orchestrator: it first checks that the host answers with its
pinned key (the "provider" step, which for own hosts only verifies reachability), then does the
workspace step:

- creates the container `devbox-PROJECT` from `images:ubuntu/24.04` with `limits.cpu`,
  `limits.memory` and `security.nesting=true` (so Docker works inside), plus the labels
  `user.devbox.managed=true`, `user.devbox.controller=<this install's id>` and
  `user.devbox.project=PROJECT`;
- sets it up like a Hetzner server does: sshd with root login by key (your devbox key), Docker with
  Compose, mise, cloudflared, rsync, git, `/srv/app` and `/etc/profile.d/devbox.sh`, taking the
  steps from the same `cloud-init.yaml` as Hetzner servers. If Docker cannot start containers
  inside the container (newer runc on older Incus), it registers runc 1.2.3 as Docker's default
  runtime;
- pins the container's own host key, as for Hetzner servers, and syncs the code.

The first `up` takes about a minute on a fast connection; `pause` and `up` afterwards take seconds.
Limits default to `incus_cpus = 2` and `incus_memory = "4GiB"` in the config (and `incus_image`);
`--cpus` and `--memory` override them, also on an existing workspace.

Everything else works through the same code as for Hetzner servers, only with an SSH jump through
the host (`ProxyCommand`, pinned host key) in front: `exec`, `exec --detach`, `ps`, `logs`, `stop`,
`sync`, `pull`, `forward`, `ssh`, `db`, `secrets`, `expose`. Previews use cloudflared inside the
container (outbound only). Two workspaces on a host can both listen on the same port, such as
5173, because each has its own network namespace.

| Command | On an own host |
| --- | --- |
| `up` | create or start the container, set it up, sync code |
| `pause` | `incus stop`: the container and its files and volumes are kept |
| `up` after `pause` | `incus start`, files are still there; processes and previews must be started again |
| `down` | delete the container with its files, volumes and databases (same warnings as for servers: `--dump` / `--no-dump`) |
| `status` | host, workspace name, limits, address, cost `none (own hardware)` |
| `idle-check` | leaves workspaces alone (no idle shutdown on own hardware) |
| `pause --at-hour-end` | refused: there are no billing hours |

### Safety

devbox only starts, stops, changes, runs commands in or deletes containers that carry **both**
labels with this install's controller id. A container with the name `devbox-PROJECT` that lacks them
makes `up` stop with an error. `host remove` refuses while workspaces exist; `--force` deletes the
workspaces (containers devbox created) and never the machine. Containers of other devbox installs on
the same host are not touched.

### Limits and `max_servers`

`max_servers` limits billed servers. Workspaces on your own hosts do not count toward it, and
`--additional` is not needed for them.

### Known limits

- One workspace per project, on one host; moving a project means `down`, then `up --host OTHER`.
- Containers can reach each other over `incusbr0`; there is no network isolation between workspaces.
- No Incus snapshot on `pause` and no machine snapshot; `pause` is stop-and-keep.
- The first `up` installs everything from the network; there is no cached image yet.
- `host bootstrap` only knows `apt`; on other systems install Incus yourself.
