"""Workspaces: one unprivileged Incus system container per project, on a host.

Everything here runs through a `HostShell` (SSH to the host). Containers carry the labels
`user.devbox.managed=true` and `user.devbox.controller=<id>`; devbox only ever starts, stops,
changes, runs commands in or deletes containers that have both, with its own controller id.
"""

import hashlib
import json
import re
import shlex
import subprocess
import time
from importlib import resources

from devbox import DevboxError
from devbox.hosts import HostShell

MANAGED = "user.devbox.managed"
CONTROLLER = "user.devbox.controller"
PROJECT = "user.devbox.project"
BASE_PROP = "user.devbox.base"
NAME_PREFIX = "devbox-"
CONTAINER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
IMAGE_REMOTE = "images:"
BASE_PREFIX = "devbox-base-"  # alias of the cached, provisioned base image
BASE_CACHE_FORMAT = "1"  # bump when the way base images are made changes
PORT_ISOLATION = "security.port_isolation"
NIC = "eth0"
WORKAROUND_MARKER = "devbox: runc workaround registered"


def container_name(project: str) -> str:
    name = NAME_PREFIX + project
    if name.endswith("-"):
        raise DevboxError(f"project name '{project}' ends with a dash, which Incus does not accept in a container "
                          f"name ({name}); register the project under another name")
    if not CONTAINER_NAME_RE.match(name):
        raise DevboxError(f"project name '{project}' is too long for a container name ({name})")
    return name


def parse_cloud_init(text: str) -> tuple[list[dict], list[str]]:
    """(files, commands) from cloud-init.yaml, so containers get the same setup as Hetzner servers.

    Reads only the shapes that file uses: `write_files` entries with path, permissions and a `|`
    block as content, and `runcmd` entries that are one line or a `|` block. Anything else fails
    loudly instead of silently dropping setup steps.
    """
    files: list[dict] = []
    commands: list[str] = []
    lines = text.splitlines()
    section = None
    i = 0

    def block(indent: int) -> list[str]:
        nonlocal i
        out = []
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) >= indent):
            out.append(lines[i][indent:] if lines[i].strip() else "")
            i += 1
        while out and not out[-1]:
            out.pop()
        return out

    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        top = re.match(r"^([a-z_]+):\s*$", line)
        if top:
            section = top[1]
            i += 1
            continue
        if section == "write_files" and line.startswith("  - path:"):
            entry = {"path": line.split(":", 1)[1].strip(), "mode": "0644", "content": ""}
            i += 1
            while i < len(lines) and lines[i].startswith("    "):
                key, _, value = lines[i].strip().partition(":")
                value = value.strip()
                i += 1
                if key == "permissions":
                    entry["mode"] = value.strip('"')
                elif key == "content" and value == "|":
                    entry["content"] = "\n".join(block(6)) + "\n"
                else:
                    raise DevboxError(f"cloud-init.yaml: unsupported write_files key '{key}'")
            files.append(entry)
        elif section == "runcmd" and line.startswith("  - "):
            value = line[4:].rstrip()
            i += 1
            commands.append("\n".join(block(4)) if value == "|" else value)
        else:
            raise DevboxError(f"cloud-init.yaml: unsupported line '{line.strip()[:60]}'")
    if not files or not commands:
        raise DevboxError("cloud-init.yaml: found no write_files/runcmd to reuse")
    return files, commands


def cloud_init_steps() -> tuple[list[dict], list[str]]:
    return parse_cloud_init(resources.files("devbox").joinpath("cloud-init.yaml").read_text())


# Extra setup a container needs that a Hetzner server gets from its image or from cloud-init modules.
SSHD_SETUP = (
    "dpkg-query -W -f='${Status}' openssh-server 2>/dev/null | grep -q 'install ok installed' || "
    "{ apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server; } || exit 1; "
    "systemctl enable ssh >/dev/null 2>&1; true"
)
NETWORK_CHECK = (
    "for i in 1 2 3 4 5 6; do getent hosts archive.ubuntu.com >/dev/null && exit 0; sleep 5; done; exit 1"
)


# Docker inside an Incus container: newer runc releases fail to start any container on older Incus/LXC
# ("open sysctl net.ipv4.ip_unprivileged_port_start ... permission denied", seen with runc 1.5 on Incus 6.0.0).
# Check that containers start; if not, register runc 1.2.3 as Docker's default runtime.
DOCKER_CHECK = r'''
command -v docker >/dev/null || exit 1
systemctl is-active --quiet docker || systemctl start docker
log=$(mktemp)
# Docker may still be settling right after the container started (cached image): try a few times.
check() { for i in 1 2 3; do timeout 120 docker run --rm hello-world >"$log" 2>&1 && return 0; sleep 3; done; return 1; }
if check; then docker rmi hello-world >/dev/null 2>&1; rm -f "$log"; exit 0; fi
arch=$(dpkg --print-architecture)
curl -fsSL --connect-timeout 20 --max-time 120 --retry 2 -o /usr/local/bin/runc-compat \
  "https://github.com/opencontainers/runc/releases/download/v1.2.3/runc.$arch" || exit 1
chmod +x /usr/local/bin/runc-compat
python3 - <<'PY'
import json, os
path = "/etc/docker/daemon.json"
data = json.load(open(path)) if os.path.exists(path) else {}
data.setdefault("runtimes", {})["runc-compat"] = {"path": "/usr/local/bin/runc-compat"}
data["default-runtime"] = "runc-compat"
json.dump(data, open(path, "w"), indent=2)
PY
systemctl restart docker
sleep 3
check || { echo "docker cannot start containers inside this container: $(tail -n 3 "$log" | tr '\n' ' ')" >&2; exit 1; }
rm -f "$log"
docker rmi hello-world >/dev/null 2>&1
echo "devbox: runc workaround registered"
exit 0
'''


def base_fingerprint(image: str, incus_version: str) -> str:
    """Identifies what a provisioned base image contains.

    It changes when the setup files or scripts change, the source image changes, or the host's Incus
    changes (a cached image may carry the runc workaround that a newer Incus does not need).
    """
    parts = [BASE_CACHE_FORMAT, image, incus_version, resources.files("devbox").joinpath("cloud-init.yaml").read_text(),
             SSHD_SETUP, NETWORK_CHECK, DOCKER_CHECK]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


def base_alias(image: str, incus_version: str) -> str:
    return BASE_PREFIX + base_fingerprint(image, incus_version)


# Run in the base container just before it is published: nothing that belongs to one workspace may be in the image.
BASE_CLEAN = (
    "rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub /root/.ssh/authorized_keys; "
    "apt-get clean; rm -rf /var/lib/apt/lists/*; truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id; "
    "docker image prune -af >/dev/null 2>&1; sync; true"
)


class Incus:
    """Incus on one host, as the SSH user (directly or via `sudo -n`)."""

    def __init__(self, shell: HostShell, controller_id: str, prefix: str | None = None):
        self.shell = shell
        self.controller_id = controller_id
        self.prefix = prefix  # "incus" or "sudo -n incus", detected on first use

    # --- running incus ---

    def _incus(self) -> str:
        if self.prefix is None:
            probe = self.shell.run(
                "command -v incus >/dev/null || { echo missing; exit 0; }; "
                "if incus list --format csv -c n </dev/null >/dev/null 2>&1; then echo incus; "
                "elif sudo -n incus list --format csv -c n </dev/null >/dev/null 2>&1; then echo sudo; "
                "else echo none; fi", timeout=30)
            answer = probe.stdout.strip().splitlines()[-1] if probe.stdout.strip() else "none"
            host = self.shell.host.name
            if answer == "missing":
                raise DevboxError(f"Incus is not installed on host {host}; run `devbox host bootstrap {host}`")
            if answer == "none":
                raise DevboxError(
                    f"the SSH user on host {host} cannot run incus: add it to the incus-admin group or allow "
                    f"passwordless sudo; `devbox host bootstrap {host}` shows what is missing")
            self.prefix = "incus" if answer == "incus" else "sudo -n incus"
        return self.prefix

    def run(self, args: list[str], input: str | None = None, timeout: float | None = 120,
            check: bool = True) -> subprocess.CompletedProcess:
        """`incus ARGS` on the host. Stdin is /dev/null unless `input` is given."""
        script = f"{self._incus()} {shlex.join(args)}" + ("" if input is not None else " </dev/null")
        proc = self.shell.run(script, input=input, timeout=timeout, check=False)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise DevboxError(f"incus {args[0]} failed on host {self.shell.host.name} (exit {proc.returncode})"
                              f"{': ' + detail if detail else ''}")
        return proc

    # --- containers and ownership ---

    def _list(self, name: str | None = None) -> list[dict]:
        proc = self.run(["list", *([f"^{name}$"] if name else []), "--format", "json"], timeout=60)
        try:
            return json.loads(proc.stdout or "[]")
        except ValueError:
            raise DevboxError(f"unreadable `incus list` output on host {self.shell.host.name}")

    def is_ours(self, container: dict) -> bool:
        config = container.get("config") or {}
        return config.get(MANAGED) == "true" and config.get(CONTROLLER) == self.controller_id

    def list_workspaces(self) -> list[dict]:
        """Containers this controller created on the host."""
        return [c for c in self._list() if self.is_ours(c)]

    def foreign_workspaces(self) -> list[dict]:
        """devbox containers of other controllers (reported, never touched)."""
        return [c for c in self._list()
                if (c.get("config") or {}).get(MANAGED) == "true" and not self.is_ours(c)]

    def get(self, name: str) -> dict | None:
        """The container `name` if it is ours; None if it does not exist. Refuses anyone else's."""
        found = self._list(name)
        if not found:
            return None
        if not self.is_ours(found[0]):
            raise DevboxError(
                f"container {name} exists on host {self.shell.host.name} but was not created by this devbox "
                f"(labels {MANAGED} and {CONTROLLER} are missing or differ); devbox does not touch it")
        return found[0]

    def require(self, name: str) -> dict:
        c = self.get(name)
        if c is None:
            raise DevboxError(f"container {name} does not exist on host {self.shell.host.name}")
        return c

    @staticmethod
    def ipv4(container: dict) -> str | None:
        """The address on the container's bridge (eth0), not Docker's bridges inside it."""
        networks = (container.get("state") or {}).get("network") or {}
        order = sorted((d for d in networks if d != "lo"), key=lambda d: (d != "eth0", d))
        for device in order:
            if device.startswith(("docker", "br-", "veth")):
                continue
            for addr in networks[device].get("addresses", []):
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    return addr["address"]
        return None

    @staticmethod
    def limits(container: dict) -> dict:
        config = container.get("expanded_config") or container.get("config") or {}
        return {"cpus": config.get("limits.cpu"), "memory": config.get("limits.memory")}

    def create(self, name: str, project: str, image: str, cpus: int, memory: str, isolate: bool = False,
               base: str | None = None) -> None:
        """Launch the container, from the cached base image `base` (a local alias) when there is one."""
        if self.get(name) is not None:
            raise DevboxError(f"container {name} already exists on host {self.shell.host.name}")
        source = base or f"{IMAGE_REMOTE}{image}"
        args = ["launch", source, name,
                "-c", f"limits.cpu={cpus}", "-c", f"limits.memory={memory}", "-c", "security.nesting=true",
                "-c", f"{MANAGED}=true", "-c", f"{CONTROLLER}={self.controller_id}", "-c", f"{PROJECT}={project}"]
        if isolate:
            args += ["-d", f"{NIC},{PORT_ISOLATION}=true"]
        self.run(args, timeout=600)

    @staticmethod
    def isolated(container: dict) -> bool:
        nic = (container.get("expanded_devices") or container.get("devices") or {}).get(NIC) or {}
        return nic.get(PORT_ISOLATION) == "true"

    def set_isolation(self, name: str, isolate: bool) -> None:
        """Turn port isolation of the container's NIC on or off (takes effect on the next start if the NIC is not hot-pluggable)."""
        c = self.require(name)
        value = "true" if isolate else "false"
        if NIC in (c.get("devices") or {}):
            self.run(["config", "device", "set", name, NIC, f"{PORT_ISOLATION}={value}"])
        else:
            self.run(["config", "device", "override", name, NIC, f"{PORT_ISOLATION}={value}"])

    def set_limits(self, name: str, cpus: int | None, memory: str | None) -> None:
        self.require(name)
        if cpus:
            self.run(["config", "set", name, f"limits.cpu={cpus}"])
        if memory:
            self.run(["config", "set", name, f"limits.memory={memory}"])

    def start(self, name: str) -> None:
        c = self.require(name)
        if c.get("status") != "Running":
            self.run(["start", name], timeout=120)

    def stop(self, name: str) -> None:
        c = self.require(name)
        if c.get("status") != "Stopped":
            self.run(["stop", name], timeout=180)

    def delete(self, name: str) -> None:
        if self.get(name) is not None:
            self.run(["delete", "--force", name], timeout=180)

    def wait_ipv4(self, name: str, timeout: float, sleep=time.sleep) -> str:
        """The container's IPv4 address, once it has one. Fails when the network never comes up."""
        end = time.monotonic() + timeout
        while True:
            ip = self.ipv4(self.require(name))
            if ip:
                return ip
            if time.monotonic() >= end:
                raise DevboxError(
                    f"container {name} got no IPv4 address; if the host runs Docker or UFW, "
                    f"`devbox host bootstrap {self.shell.host.name}` shows the missing firewall rules")
            sleep(2)

    def exec(self, name: str, script: str, input: str | None = None, timeout: float | None = 600,
             check: bool = True) -> subprocess.CompletedProcess:
        """Run a bash script as root in the container (ownership is checked first)."""
        self.require(name)
        return self.run(["exec", name, "--", "bash", "-c", script], input=input, timeout=timeout, check=check)

    # --- cached base image ---

    def version(self) -> str:
        """The Incus server version on the host (empty if it cannot be read)."""
        out = self.run(["version"], timeout=30, check=False).stdout or ""
        m = re.search(r"Server version:\s*(\S+)", out)
        return m[1] if m else ""

    def _images(self) -> list[dict]:
        proc = self.run(["image", "list", "--format", "json"], timeout=60)
        try:
            return json.loads(proc.stdout or "[]")
        except ValueError:
            raise DevboxError(f"unreadable `incus image list` output on host {self.shell.host.name}")

    def base_images(self) -> list[dict]:
        """Base images this controller published: [{"alias", "fingerprint", "size"}]."""
        found = []
        for img in self._images():
            props = img.get("properties") or {}
            if props.get(BASE_PROP) != "true" or props.get(CONTROLLER) != self.controller_id:
                continue
            for a in img.get("aliases") or []:
                if a.get("name", "").startswith(BASE_PREFIX):
                    found.append({"alias": a["name"], "fingerprint": img.get("fingerprint", ""),
                                  "size": img.get("size")})
        return found

    def has_base(self, alias: str) -> bool:
        return any(i["alias"] == alias for i in self.base_images())

    def publish_base(self, name: str, alias: str, say=lambda m: None) -> None:
        """Publish the (provisioned, not yet personalised) container as the host's base image.

        The container is cleaned of host keys and machine id, stopped for the publish and started again.
        Older base images of this controller are deleted afterwards.
        """
        self.exec(name, BASE_CLEAN, timeout=120)
        say("Publishing the base image for later workspaces...")
        self.run(["stop", name], timeout=180)
        try:
            # Uncompressed: publishing takes half the time and launching from the image a third (measured on a
            # 1.4 GB workspace on dir storage: 26 s vs 56 s to publish, 3 s vs 11 s to launch).
            self.run(["publish", name, "--alias", alias, "--reuse", "--compression", "none", f"{BASE_PROP}=true",
                      f"{CONTROLLER}={self.controller_id}"], timeout=900)
        finally:
            self.run(["start", name], timeout=180)
        for old in self.base_images():
            if old["alias"] != alias:
                self.run(["image", "delete", old["fingerprint"]], check=False)

    def delete_base_image(self, fingerprint: str) -> None:
        mine = {i["fingerprint"] for i in self.base_images()}
        if fingerprint not in mine:
            raise DevboxError(f"image {fingerprint[:12]} is not a base image of this devbox; devbox does not touch it")
        self.run(["image", "delete", fingerprint])

    # --- provisioning ---

    def provision(self, name: str, public_key: str, host_private: str, host_public: str,
                  say=lambda m: None, bound=lambda: 900, publish_as: str | None = None,
                  publish_ok=lambda: True) -> dict:
        """Install what a Hetzner server gets from cloud-init, plus sshd access for devbox. Idempotent.

        With `publish_as`, the container is published as the host's base image once the shared setup is
        done and before anything specific to this workspace (keys) is written. A failure to publish only
        costs the cache, never the workspace, and `publish_ok()` lets the caller skip it when the startup
        deadline leaves too little time (publishing takes about 30 s). Returns {"workaround": bool, "published": bool}.
        """
        say("Checking container network...")
        if self.exec(name, NETWORK_CHECK, check=False, timeout=60).returncode != 0:
            raise DevboxError(
                f"container {name} cannot resolve names (no internet from the container); if the host runs "
                f"Docker or UFW, `devbox host bootstrap {self.shell.host.name}` fixes the firewall")
        files, commands = cloud_init_steps()
        say("Installing base files...")
        for f in files:
            parent = shlex.quote(f["path"].rsplit("/", 1)[0])
            path = shlex.quote(f["path"])
            self.exec(name, f"mkdir -p {parent} && cat > {path} && chmod {f['mode']} {path}", input=f["content"])
        say("Installing sshd...")
        self.exec(name, SSHD_SETUP, timeout=bound())
        for i, command in enumerate(commands, 1):
            say(f"Setup step {i}/{len(commands)}...")
            self.exec(name, command, timeout=bound())
        say("Checking that Docker can run containers...")
        try:
            checked = self.exec(name, DOCKER_CHECK, timeout=bound())
        except DevboxError as e:
            if "disk quota exceeded" in str(e):
                raise DevboxError(
                    f"{e}\nThe host's kernel key quota is used up (kernel.keys.maxkeys, shared by all workspaces); "
                    f"`devbox host bootstrap {self.shell.host.name}` shows the fix, or `devbox down` workspaces "
                    "you no longer need") from e
            raise
        workaround = WORKAROUND_MARKER in (checked.stdout or "")
        if workaround:
            say("Docker needed the runc 1.2.3 workaround on this Incus version; "
                f"`devbox host bootstrap {self.shell.host.name} --zabbly` plans a newer Incus")
        published = False
        if publish_as and not publish_ok():
            say("Skipping the base image: too little startup time left (the next `up` on this host publishes it)")
        elif publish_as:
            try:
                self.publish_base(name, publish_as, say)
                published = True
            except DevboxError as e:
                say(f"Could not publish the base image (workspaces still work, later ones just take longer): {e}")
                self.run(["start", name], timeout=180, check=False)
        say("Installing SSH keys...")
        self.exec(name, "mkdir -p /root/.ssh && chmod 700 /root/.ssh && cat > /root/.ssh/authorized_keys "
                        "&& chmod 600 /root/.ssh/authorized_keys", input=public_key.strip() + "\n")
        self.exec(name, "rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub; "
                        "umask 077; cat > /etc/ssh/ssh_host_ed25519_key", input=host_private.rstrip("\n") + "\n")
        self.exec(name, "chmod 600 /etc/ssh/ssh_host_ed25519_key; cat > /etc/ssh/ssh_host_ed25519_key.pub "
                        "&& chmod 644 /etc/ssh/ssh_host_ed25519_key.pub", input=host_public.strip() + " devbox-host\n")
        self.exec(name, "systemctl restart ssh && systemctl is-active --quiet ssh")
        return {"workaround": workaround, "published": published}
