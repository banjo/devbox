# Workspaces

A workspace is what a project's server is on Hetzner, minus the machine: one unprivileged
[Incus](https://linuxcontainers.org/incus/) system container per project on a registered
[host](hosts.md), with its own files, processes, Docker, network and resource limits. Root inside it
is not root on the host (user namespace), and Docker works inside because the container has
`security.nesting=true`.

```sh
devbox host add mini --ssh dev@mini
devbox host bootstrap mini --yes
devbox up agent-1 --host mini --cpus 2 --memory 4GiB
devbox up agent-2 --host mini --cpus 4 --memory 8GiB
devbox expose agent-1 5173 --wait-for /      # both can listen on 5173
```

## Lifecycle

`devbox up PROJECT --host NAME` is an orchestrator. First the host step: check that the host answers
with its pinned key (for own hosts this only verifies reachability; the machine is never created or
changed). Then the workspace step:

- creates the container `devbox-PROJECT` from `images:ubuntu/24.04` (or from the host's cached base
  image, see below) with `limits.cpu`, `limits.memory` and `security.nesting=true`, plus the labels
  `user.devbox.managed=true`, `user.devbox.controller=<this install's id>` and
  `user.devbox.project=PROJECT`;
- sets it up like a Hetzner server, from the same `cloud-init.yaml`: sshd with root login by key (your
  devbox key), Docker with Compose, mise, cloudflared, rsync, git, `/srv/app` and
  `/etc/profile.d/devbox.sh`. If Docker cannot start containers inside the container (newer runc on
  Incus older than 6.1) it registers runc 1.2.3 as Docker's default runtime and `up` warns; a newer
  Incus avoids this ([Own hosts](hosts.md#bootstrap));
- pins the container's own host key, as for Hetzner servers, and syncs the code.

| Command | On an own host |
| --- | --- |
| `up` | create or start the container, set it up, sync code |
| `pause` | `incus stop`: the container and its files and volumes are kept |
| `up` after `pause` | `incus start`, files are still there; processes and previews must be started again |
| `down` | delete the container with its files, volumes and databases (same warnings as for servers: `--dump` / `--no-dump`) |
| `status` | host, workspace name, limits, address, cost `none (own hardware)`, previews, processes |
| `idle-check` | leaves workspaces alone (no idle shutdown on own hardware) |
| `pause --at-hour-end` | refused: there are no billing hours |

Everything else (`exec`, `exec --detach`, `ps`, `logs`, `stop`, `sync`, `pull`, `forward`, `ssh`,
`db`, `secrets`, `expose`) is the code used for Hetzner servers, with an SSH jump through the host
(`ProxyCommand`, pinned host key) in front.

Limits default to `incus_cpus = 2` and `incus_memory = "4GiB"` in the config (and `incus_image`);
`--cpus` and `--memory` override them, also on an existing workspace.

## Previews

Previews use cloudflared **inside the container**, which only connects outwards, so nothing on the
host is opened and home hardware needs no router setup. Because each workspace has its own network
namespace, every workspace can listen on the same port (5173, 3000, ...) and each gets its own
tunnel.

- **Without `preview_domain`**: `expose NAME PORT` starts a quick tunnel in that container and prints a
  random `*.trycloudflare.com` URL. It changes whenever the tunnel restarts.
- **With `preview_domain`** (and a Cloudflare token, see [Previews](previews.md)): one named tunnel
  per project and a stable hostname per preview, `<project>.<domain>` for the default preview and
  `<project>-<preview>.<domain>` for named ones, so `agent-1`, `agent-2` and `agent-1`'s `api` preview
  never collide on one host. The ingress rule points at `localhost:PORT` in the container. devbox
  checks the label against the other projects it knows before announcing or creating a hostname, and
  `expose` refuses one that another project already uses (for example project `crm-web` and project
  `crm` with `--name web` would both be `crm-web.<domain>`); it also never replaces a DNS record it did
  not create.
- `DEVBOX_PREVIEW_URL`, `DEVBOX_PREVIEW_HOST` and the `_NAME` variants are written to
  `/etc/profile.d/devbox-preview.sh` **inside the container**, so every `exec`, background process and
  `ssh` session in the workspace sees them. With `preview_domain` they are set from `up` on; with quick
  tunnels once `expose` has a URL.
- `status`, `unexpose` and the "not running" hint after a restart work per workspace: `status NAME`
  lists only that workspace's previews and checks that their tunnel process runs in that container.

## Base image cache

The first `up` on a host installs everything from the network. With `incus_image_cache = true`
(default) devbox then publishes that container as a local Incus image `devbox-base-<hash>`, taken after the
shared setup and before anything specific to the workspace: ssh host keys, authorized keys and the
machine id are removed first. Later workspaces on the host are launched from this image; setup then
only has to verify what is there. `up` reports `image_cache` (`published`, `hit` or `off`). Only one `up` per host builds the image at a time.

The hash covers the source image, `cloud-init.yaml`, devbox's setup and Docker-check scripts and the
host's Incus version. When any of them change, the next `up` builds and publishes a new image and
deletes the old one; a changed Incus version matters because a cached image may carry the runc workaround.
That `up` is a cold start (1 to 3 minutes) and says so before it begins, with the reason (no image yet, or
the Incus version or devbox's setup changed); `host bootstrap --zabbly --yes` mentions it too when it replaced
the Incus of a host that has a cached image. If the runc workaround was needed to build the image, the image
carries the property `user.devbox.runc-workaround=true`, so every `up` from it prints the same warning as
the one that built it.
The image is published uncompressed: on a 16-core mini PC (dir storage) publishing a 1.4 GB workspace took
about 26 s instead of 56 s with gzip, and launching from it 3 s instead of 11 s, at the cost of disk (about
1.4 GB instead of 0.5 GB per host). The first `up` on a host therefore takes about 30 s longer than without
the cache, and later ones are much faster. `up` skips publishing when less than 90 s of the startup limit are
left. If publishing fails, `up` continues and only the cache is missing. `devbox cleanup` lists images that no
longer match, and `host remove` deletes the image. devbox only lists and deletes images that carry its
own properties and this install's controller id. Set `incus_image_cache = false` to turn it off.

## Network isolation between workspaces

Workspaces on one host share the `incusbr0` bridge and can reach each other by address. For code you do
not trust, `devbox up PROJECT --host NAME --isolate` (or `incus_isolate = true` in the config, with
`--no-isolate` to override) sets `security.port_isolation=true` on the workspace's network card.
Incus then marks its bridge port as isolated: isolated ports cannot exchange traffic with each other,
while each can still reach the bridge itself (the host, DHCP, DNS) and the internet. A workspace that
is not isolated can still talk to both kinds. It is off by default. Turning it on or off for an existing
workspace takes effect at once, without a restart. Port isolation affects the Layer 2 bridge only; it is not
a replacement for a firewall on the host.

Checked on Incus 6.0.0 (Ubuntu archive) and 7.6 (Zabbly): two workspaces on `incusbr0` could ping each other
and reach each other's port 5173; after `--isolate` on both, ping and TCP between them failed, while each
still had DNS, internet and (by ping) the gateway; an isolated and a non-isolated workspace could still talk;
`--no-isolate` restored the traffic. The setting survived `pause` and `up`.

## Safety

devbox only starts, stops, changes, runs commands in or deletes containers that carry **both**
labels with this install's controller id. A container with the name `devbox-PROJECT` that lacks them
makes `up` stop with an error. `host remove` refuses while workspaces exist; `--force` deletes the
workspaces (containers devbox created) and never the machine. Containers of other devbox installs on
the same host are not touched. `devbox cleanup` and `doctor` list containers this devbox created that
no workspace in the registry uses; they are only deleted with `cleanup --yes`.

## Limits and `max_servers`

`max_servers` limits billed servers. Workspaces on your own hosts do not count toward it, and
`--additional` is not needed for them.

## Several workspaces at once

`up` of different projects runs in parallel: each `up` holds only a lock for its own project for the long
startup, and takes the short registry lock (the one `down` and the other commands use) only while it
writes to the registry. Three `up PROJECT --host H` started together take about as long as one cold start
plus a little: while the host has no cached base image, one of them builds it and the others wait for that
(they say so), then start from the cached image. Two `up`s of the same project, or an `up` and a `down` of
it, take turns (`waiting for another devbox command on PROJECT to finish...`); the startup limit counts from
the moment the command has its project lock, and a wait for the base image counts. A Hetzner `up` still holds
the registry lock for its whole startup (`max_servers` counts existing servers), so a workspace `up` waits
for it at its first registry write. Each workspace uses 15 to 30 kernel keys from a quota that all containers on the host share
(`kernel.keys.maxkeys`, default 200); about a dozen workspaces exhaust it and Docker fails with
`disk quota exceeded`. `devbox host bootstrap` reports a low value ([Own hosts](hosts.md#bootstrap)).

## Known limits

- One workspace per project, on one host; moving a project means `down`, then `up --host OTHER`.
- No Incus snapshot on `pause` and no machine snapshot; `pause` is stop-and-keep.
- `host bootstrap` only knows `apt`; on other systems install Incus yourself.
- Workspaces share the host's CPU, memory and disk; limits cap CPU and memory only.
