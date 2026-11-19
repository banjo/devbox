# Own hosts

A host is a machine you already own (a mini PC, a home server, a VM) that devbox reaches over SSH. On
it, every project runs in its own unprivileged [Incus](https://linuxcontainers.org/incus/) system
container, a **workspace** ([Workspaces](workspaces.md)). devbox never creates, powers off, snapshots
or deletes the machine, and it costs nothing: there is no billing, so `costs`, billing hours and
`--at-hour-end` do not apply.

Hetzner servers keep working exactly as before. Choose per project with `--host`.

```text
devbox host add NAME --ssh USER@HOST [--key PATH]   register a host and pin its SSH host key
devbox host bootstrap NAME [--zabbly] [--yes]       show (then apply) what the host needs
devbox host list                                    hosts and the workspaces on them
devbox host remove NAME [--force]                   forget a host; the machine is never touched
devbox up PROJECT --host NAME [--cpus N --memory SIZE] [--isolate]
```

Every command takes `--json`. `host add` and `host bootstrap` end with `next:` lines saying what to do
next (`next.steps` in `--json`), so an agent can follow them.

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
`nothing to do`. The plan always names the Incus version it found.

What it checks and may do:

1. **Incus**: installs the `incus` package from the Ubuntu/Debian archive (`apt-get`, needs
   `sudo -n`) and runs `incus admin init --minimal` (bridge `incusbr0`, `dir` storage) when it is
   missing or uninitialised.
2. **A newer Incus (`--zabbly`)**: the archive ships Incus 6.0.x on Ubuntu 24.04. On it, current
   Docker (docker-ce with runc 1.5) cannot start containers inside a workspace
   (`open sysctl net.ipv4.ip_unprivileged_port_start ... permission denied`). The plan flags versions
   older than 6.1 and says so. With `--zabbly`, the plan instead adds the
   [Zabbly](https://github.com/zabbly/incus) package repository
   (`https://pkgs.zabbly.com/incus/stable`, suites `bookworm`, `trixie`, `jammy`, `noble`, `resolute`)
   and installs, or upgrades to, its `incus` package. The signing key is downloaded to
   `/etc/apt/keyrings/zabbly.asc` and compared with the published fingerprint
   (`4EFC 5906 96CB 15B8 7C73 A3AD 82CC 8797 C838 DCFD`) before it is trusted; a different key is deleted
   and the step fails. This is never done automatically: it needs `--zabbly` **and** `--yes`, and
   `--yes` alone leaves Incus as it is. An upgrade restarts the Incus daemon on the host; the plan says
   how many containers are running. A host whose Incus is already new enough is left alone. Checked on a
   Hetzner server: Incus 6.0.0 from the archive fails with runc 1.5.3 and needs the fallback; after
   `--zabbly --yes` (about 50 s, running containers kept running) Incus 7.6 starts Docker containers with
   the stock runc and no workaround. The Zabbly package also sets `kernel.keys.maxkeys=2000` (below).
   Without it, provisioning keeps its fallback: if Docker cannot start containers it registers runc
   1.2.3 as Docker's default runtime in that workspace, and `up` warns that a newer Incus avoids this.
3. **Docker + UFW**: on a host with Docker, `FORWARD` is `DROP`, and UFW blocks the Incus bridge
   as well, so containers get no IPv4 and no internet. If UFW is active, bootstrap adds only the
   missing ones of `ufw allow in on incusbr0`, `ufw route allow in on incusbr0` and
   `ufw route allow out on incusbr0`. It never edits Docker's own rules. If `FORWARD` is `DROP` but
   UFW is not active, bootstrap only reports it (a `manual:` line with the rules to add): the host's
   firewall is yours, and `--zabbly` or `--yes` never change that.
4. **Kernel key quota** (report only): all unprivileged containers on a host share one kernel key quota
   (`kernel.keys.maxkeys`, 200 by default). A workspace uses 15 to 30 keys depending on what runs in it;
   when the quota is used up Docker fails with `unable to create session key: disk quota exceeded`
   (seen on a host with 12 workspaces). The plan prints a `manual:` line when the value is below 1000;
   devbox does not change kernel settings. The Zabbly Incus package raises it to 2000, and
   `echo 'kernel.keys.maxkeys = 2000' | sudo tee /etc/sysctl.d/99-devbox-incus.conf && sudo sysctl --system`
   does the same for the archive package.
5. **Access**: the SSH user needs to be in the `incus-admin` group or have passwordless `sudo -n`.
   devbox uses the group when it can and `sudo -n incus` otherwise. If neither works, bootstrap
   says so and changes nothing. It never writes global files on the host except the Zabbly repository
   files above, and only when asked.

`host remove` forgets the host. It refuses while workspaces exist; `--force` deletes the workspaces
devbox created, and devbox's cached base image ([Workspaces](workspaces.md#base-image-cache)). It never
touches the machine itself. If the machine can no longer be reached, `--force` still forgets the host and
its workspaces in the registry (with a warning; any containers stay on the machine).

## A Hetzner server as a host

A server devbox created can also be registered as a host (`host add NAME --ssh root@IP --key ~/.ssh/devbox`),
so one project runs on the server directly and others in containers on the same machine. Two things to know:

- Such a server has Docker, so its `FORWARD` policy is `DROP` and UFW is not active: containers get DNS but
  no other connectivity. Bootstrap prints the `manual:` rules (`iptables -I DOCKER-USER ...`); they were
  checked to restore the network, and `up` fails after about a minute with a pointer to them instead of
  waiting for a timeout.
- `down` and `pause` of that server refuse while workspaces are registered on the same address, because
  deleting or snapshotting the machine would destroy or freeze them. Remove the workspaces first.

## Where things are

- Containers: `incus list` on the host shows `devbox-PROJECT`, labelled `user.devbox.managed=true`,
  `user.devbox.controller=<id>` and `user.devbox.project=PROJECT`.
- The cached base image: alias `devbox-base-<hash>`, properties `user.devbox.base=true` and the
  controller id.
- `devbox cleanup` and `devbox doctor` list containers and images of this devbox that the registry no
  longer knows ([Housekeeping](housekeeping.md)).
