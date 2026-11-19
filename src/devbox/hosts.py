"""Own machines devbox runs workspaces on: registry entries, SSH access and `host bootstrap`.

A host is a machine reached over SSH (`devbox host add NAME --ssh USER@HOST`). devbox never creates,
powers off or deletes it. Its host key is pinned when the host is added, so every later connection
(and every jump through it to a workspace) verifies that key and nothing else.
"""

import base64
import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from devbox import DevboxError
from devbox.remote import Jump

HOST_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._:\[\]-]+$")
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa")
BRIDGE = "incusbr0"
# Incus releases before this one are what Debian/Ubuntu archives ship (6.0.x); on them Docker with a newer
# runc cannot start containers inside a workspace (devbox falls back to runc 1.2.3 there).
OLD_INCUS_BELOW = (6, 1)
ZABBLY_URL = "https://pkgs.zabbly.com/incus/stable"
ZABBLY_KEY_URL = "https://pkgs.zabbly.com/key.asc"
# Fingerprint of the Zabbly archive signing key (the key is checked against it before it is trusted).
ZABBLY_KEY_FPR = "4EFC 5906 96CB 15B8 7C73 A3AD 82CC 8797 C838 DCFD".replace(" ", "")
ZABBLY_KEYRING = "/etc/apt/keyrings/zabbly.asc"
ZABBLY_SOURCES = "/etc/apt/sources.list.d/zabbly-incus-stable.sources"
ZABBLY_SUITES = {"bookworm", "trixie", "jammy", "noble", "resolute"}


def validate_host_name(name: str) -> None:
    if not HOST_NAME_RE.match(name):
        raise DevboxError(f"invalid host name '{name}': use lowercase letters, digits and dashes")


def validate_ssh_target(target: str) -> None:
    if not SSH_TARGET_RE.match(target):
        raise DevboxError(f"invalid SSH target '{target}': use USER@HOST (a name from your ssh config works)")


def fingerprint(host_key: str) -> str:
    """OpenSSH-style SHA256 fingerprint of a public key given as 'TYPE BASE64'."""
    try:
        blob = base64.b64decode(host_key.split()[1])
    except (IndexError, ValueError):
        raise DevboxError(f"not a public host key: '{host_key[:40]}'")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class Host:
    name: str
    ssh: str  # user@host
    host_key: str  # "TYPE BASE64"
    fingerprint: str
    key: Path | None = None

    @classmethod
    def from_row(cls, row) -> "Host":
        return cls(row["name"], row["ssh"], row["host_key"], row["fingerprint"],
                   Path(row["key"]) if row["key"] else None)

    @property
    def alias(self) -> str:
        return f"devbox-host-{self.name}"

    @property
    def key_type(self) -> str:
        return self.host_key.split()[0]

    def known_hosts(self, state_dir: Path) -> Path:
        """known_hosts file holding only this host's pinned key (rewritten on use)."""
        path = state_dir / "known_hosts" / f"host-{self.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{self.alias} {self.host_key}\n")
        return path

    def jump(self, state_dir: Path) -> Jump:
        return Jump(self.ssh, self.known_hosts(state_dir), self.alias, self.key_type, self.key)

    def ssh_cmd(self, state_dir: Path, remote: str | None = None, connect_timeout: int = 10) -> list[str]:
        cmd = ["ssh"]
        if self.key is not None:
            cmd += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        cmd += [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"HostKeyAlias={self.alias}",
            "-o", f"HostKeyAlgorithms={self.key_type}",
            "-o", f"UserKnownHostsFile={self.known_hosts(state_dir)}",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "ServerAliveInterval=30",
            "-o", "LogLevel=ERROR",
            self.ssh,
        ]
        if remote is not None:
            cmd += ["--", remote]
        return cmd


def _scan(cmd: list[str], runner) -> subprocess.CompletedProcess:
    try:
        return runner(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise DevboxError(f"{cmd[0]} not found; install OpenSSH client tools") from e
    except subprocess.TimeoutExpired:
        raise DevboxError(f"{cmd[0]} timed out")


def scan_host_key(target: str, runner=subprocess.run) -> str:
    """The host key `target` (USER@HOST as ssh resolves it, incl. ssh config aliases) presents.

    Returns 'TYPE BASE64', preferring ed25519. This is only what the machine answers now: the caller
    shows the fingerprint and asks before pinning it.
    """
    proc = _scan(["ssh", "-G", target], runner)
    if proc.returncode != 0:
        raise DevboxError(f"cannot resolve {target}: {(proc.stderr or '').strip()}")
    options = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        options.setdefault(key.lower(), value.strip())
    hostname, port = options.get("hostname"), options.get("port", "22")
    if not hostname:
        raise DevboxError(f"cannot resolve {target}")
    proc = _scan(["ssh-keyscan", "-T", "10", "-p", port, "-t", "ed25519,ecdsa,rsa", hostname], runner)
    found = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1] in KEY_TYPES:
            found.setdefault(parts[1], f"{parts[1]} {parts[2]}")
    for key_type in KEY_TYPES:
        if key_type in found:
            return found[key_type]
    raise DevboxError(f"no SSH host key received from {hostname}:{port} (is sshd running and reachable?)")


class HostShell:
    """Runs shell scripts on a host over SSH (as the host's SSH user)."""

    def __init__(self, host: Host, state_dir: Path, runner: Callable | None = None):
        self.host = host
        self.state_dir = state_dir
        self.runner = runner or subprocess.run

    def run(self, script: str, input: str | None = None, timeout: float | None = 120,
            check: bool = True, connect_timeout: int = 10) -> subprocess.CompletedProcess:
        cmd = self.host.ssh_cmd(self.state_dir, f"bash -c {shlex.quote(script)}", connect_timeout)
        try:
            proc = self.runner(
                cmd, capture_output=True, text=True, timeout=timeout, input=input,
                **({} if input is not None else {"stdin": subprocess.DEVNULL}),
            )
        except subprocess.TimeoutExpired:
            raise DevboxError(f"command on host {self.host.name} timed out after {timeout}s")
        if proc.returncode == 255:
            detail = (proc.stderr or "").strip()
            raise DevboxError(f"ssh to host {self.host.name} ({self.host.ssh}) failed{': ' + detail if detail else ''}")
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise DevboxError(f"command on host {self.host.name} failed (exit {proc.returncode})"
                              f"{': ' + detail if detail else ''}")
        return proc


# --- bootstrap -------------------------------------------------------------------------------

FACTS_MARKER = "== ufw status"
# Read-only. Prints key=value facts, then the output of `ufw status` after FACTS_MARKER.
FACTS_SCRIPT = f"""
. /etc/os-release 2>/dev/null
echo "os=${{ID:-unknown}}-${{VERSION_ID:-}}"
echo "codename=${{VERSION_CODENAME:-}}"
echo "keys_maxkeys=$(cat /proc/sys/kernel/keys/maxkeys 2>/dev/null)"
echo "arch=$(dpkg --print-architecture 2>/dev/null)"
[ -e {ZABBLY_SOURCES} ] && echo zabbly_repo=1 || echo zabbly_repo=0
echo "user=$(id -un)"
id -nG | tr ' ' '\\n' | grep -qx incus-admin && echo in_incus_admin=1 || echo in_incus_admin=0
sudo -n true >/dev/null 2>&1 && echo sudo=1 || echo sudo=0
command -v apt-get >/dev/null && echo apt=1 || echo apt=0
command -v docker >/dev/null && echo docker=1 || echo docker=0
command -v ufw >/dev/null && echo ufw_installed=1 || echo ufw_installed=0
if command -v incus >/dev/null; then
  echo incus_installed=1
  echo "incus_version=$(incus --version 2>/dev/null)"
  if incus list --format csv -c n </dev/null >/dev/null 2>&1; then INC='incus'; echo incus_access=direct
  elif sudo -n incus list --format csv -c n </dev/null >/dev/null 2>&1; then INC='sudo -n incus'; echo incus_access=sudo
  else INC=''; echo incus_access=none; fi
  if [ -n "$INC" ]; then
    echo "storage_pools=$($INC storage list --format csv </dev/null 2>/dev/null | wc -l)"
    echo "running_containers=$($INC list --format csv -c s </dev/null 2>/dev/null | grep -c RUNNING)"
    $INC network list --format csv </dev/null 2>/dev/null | cut -d, -f1 | grep -qx {BRIDGE} && echo bridge=1 || echo bridge=0
  fi
else
  echo incus_installed=0
fi
echo "forward_policy=$(sudo -n iptables -S FORWARD 2>/dev/null </dev/null | head -n 1)"
echo "{FACTS_MARKER}"
sudo -n ufw status 2>/dev/null </dev/null
true
"""

# Every workspace's root is the same host uid, so all of them share one kernel key quota (200 keys by default).
# systemd and every Docker container in a workspace use some; when it runs out, Docker fails with
# "unable to create session key: disk quota exceeded". Incus's production setup guide recommends 2000.
KEYS_RECOMMENDED = 2000
KEYS_LOW_BELOW = 1000

UFW_RULES = {
    "in": ["ufw", "allow", "in", "on", BRIDGE],
    "route_in": ["ufw", "route", "allow", "in", "on", BRIDGE],
    "route_out": ["ufw", "route", "allow", "out", "on", BRIDGE],
}
_UFW_ROW = re.compile(r"^(?P<to>.+?)\s{2,}(?P<action>ALLOW(?: FWD)?(?: IN| OUT)?)\s{2,}(?P<frm>.+?)\s*$")


def parse_facts(output: str) -> dict:
    """Facts from FACTS_SCRIPT output: strings, with the ufw status table as 'ufw_status'."""
    head, _, ufw = output.partition(FACTS_MARKER + "\n")
    facts: dict = {}
    for line in head.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.match(r"^[a-z_]+$", key):
            facts[key] = value.strip()
    facts["ufw_status"] = ufw
    return facts


def ufw_rules_present(ufw_status: str) -> set[str]:
    """Which of the three IPv4 rules for the Incus bridge exist, from `ufw status` output."""
    present = set()
    for line in ufw_status.splitlines():
        m = _UFW_ROW.match(line)
        if not m or "(v6)" in line:
            continue
        to, action, frm = m["to"], m["action"], m["frm"]
        on_bridge = f"on {BRIDGE}"
        if action in ("ALLOW", "ALLOW IN") and to.endswith(on_bridge):
            present.add("in")
        elif action == "ALLOW FWD" and frm.endswith(on_bridge):
            present.add("route_in")
        elif action == "ALLOW FWD" and to.endswith(on_bridge):
            present.add("route_out")
    return present


@dataclass
class Plan:
    """What `host bootstrap` would change on a host. Empty `steps` and `blockers`: nothing to do."""

    host: str
    facts: dict
    steps: list[dict] = field(default_factory=list)  # {"id", "title", "commands": [shell strings]}
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)  # things devbox will not do itself

    @property
    def nothing_to_do(self) -> bool:
        return not self.steps and not self.blockers

    def as_dict(self) -> dict:
        return {"host": self.host, "nothing_to_do": self.nothing_to_do, "steps": self.steps,
                "blockers": self.blockers, "notes": self.notes, "manual": self.manual,
                "facts": {k: v for k, v in self.facts.items() if k != "ufw_status"}}

    def lines(self) -> list[str]:
        if self.nothing_to_do:
            if self.manual:
                out = [f"{self.host}: Incus is installed and initialised; devbox has nothing left to change, "
                       f"{len(self.manual)} item{'s' if len(self.manual) != 1 else ''} below need{'' if len(self.manual) != 1 else 's'} "
                       "you (devbox does not change those)"]
            else:
                out = [f"{self.host}: Incus is installed and initialised, nothing to do"]
        else:
            out = [f"plan for host {self.host}:"]
            for i, step in enumerate(self.steps, 1):
                out.append(f"  {i}. {step['title']}")
                out += [f"       $ {c}" for c in step["commands"]]
        out += [f"  note: {n}" for n in self.notes]
        out += [f"  manual: {m}" for m in self.manual]
        out += [f"  blocked: {b}" for b in self.blockers]
        return out


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2])) if m else None


def is_old_incus(version: str) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed < OLD_INCUS_BELOW


def zabbly_commands(facts: dict) -> list[str]:
    """Add the Zabbly repository: fetch the key, verify its fingerprint, write the sources file, update."""
    sources = "\n".join([
        "Enabled: yes", "Types: deb", f"URIs: {ZABBLY_URL}", f"Suites: {facts.get('codename')}", "Components: main",
        f"Architectures: {facts.get('arch')}", f"Signed-By: {ZABBLY_KEYRING}"]) + "\n"
    verify = (f"fp=$(gpg --homedir \"$(mktemp -d)\" --show-keys --with-colons {ZABBLY_KEYRING} 2>/dev/null "
              f"| awk -F: '$1==\"fpr\"{{print $10; exit}}'); [ \"$fp\" = {ZABBLY_KEY_FPR} ] || "
              f"{{ sudo -n rm -f {ZABBLY_KEYRING}; echo \"Zabbly key fingerprint mismatch ($fp)\" >&2; exit 1; }}")
    return [
        "sudo -n install -d -m 0755 /etc/apt/keyrings",
        f"sudo -n curl -fsSL {ZABBLY_KEY_URL} -o {ZABBLY_KEYRING}",
        verify,
        f"printf %s {shlex.quote(sources)} | sudo -n tee {ZABBLY_SOURCES} >/dev/null",
        "sudo -n apt-get update",
    ]


def make_plan(host: str, facts: dict, zabbly: bool = False) -> Plan:
    """What bootstrap would change. `zabbly` asks for Incus from the Zabbly repository (never implied)."""
    plan = Plan(host, facts)
    sudo = facts.get("sudo") == "1"
    installed = facts.get("incus_installed") == "1"
    access = facts.get("incus_access")
    if installed and access == "none":
        plan.blockers.append(
            f"{facts.get('user', 'the SSH user')} cannot run incus: add it to the incus-admin group "
            "(`sudo usermod -aG incus-admin USER`, then log in again) or allow passwordless `sudo -n`")
    version = facts.get("incus_version", "")
    old_incus = installed and is_old_incus(version)
    wants_zabbly = zabbly and (not installed or old_incus or (installed and not version))
    if zabbly and installed and version and not old_incus:
        plan.notes.append(f"Incus {version} is new enough, the Zabbly repository is not needed")
    if wants_zabbly and facts.get("codename") not in ZABBLY_SUITES:
        plan.blockers.append(f"the Zabbly Incus repository has no packages for '{facts.get('codename') or facts.get('os')}' "
                             f"(supported: {', '.join(sorted(ZABBLY_SUITES))})")
        wants_zabbly = False
    elif wants_zabbly and facts.get("apt") != "1":
        plan.blockers.append("the Zabbly repository needs apt-get, which this host does not have")
        wants_zabbly = False
    elif wants_zabbly and not sudo:
        plan.blockers.append(f"{facts.get('user', 'the SSH user')} has no passwordless sudo (`sudo -n true` fails), "
                             "so devbox cannot add the Zabbly repository")
        wants_zabbly = False
    if wants_zabbly:
        if facts.get("zabbly_repo") != "1":
            plan.steps.append({"id": "zabbly-repo",
                               "title": f"add the Zabbly Incus repository ({ZABBLY_URL}), key checked against "
                                        f"{ZABBLY_KEY_FPR}",
                               "commands": zabbly_commands(facts)})
        else:
            plan.steps.append({"id": "apt-update", "title": "refresh the package lists (Zabbly repository is configured)",
                               "commands": ["sudo -n apt-get update"]})
        install = ("sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y "
                   "-o Dpkg::Options::=--force-confold incus")
        plan.steps.append({"id": "upgrade-incus" if installed else "install-incus",
                           "title": ("upgrade Incus" + (f" {version}" if version else "") if installed else "install Incus")
                                    + " from the Zabbly repository",
                           "commands": [install]})
        if installed and facts.get("running_containers", "0") not in ("0", ""):
            plan.notes.append(f"{facts['running_containers']} container(s) are running; the upgrade restarts the "
                              "Incus daemon on the host")
    elif not installed:
        if facts.get("apt") != "1":
            plan.blockers.append(f"incus is not installed and this host has no apt-get (OS: {facts.get('os')}); "
                                 "install Incus yourself, then run bootstrap again")
        elif not sudo:
            plan.blockers.append(f"incus is not installed and {facts.get('user', 'the SSH user')} has no "
                                 "passwordless sudo (`sudo -n true` fails), so devbox cannot install it")
        else:
            plan.steps.append({
                "id": "install-incus", "title": "install Incus from the distribution's package archive",
                "commands": ["sudo -n apt-get update",
                             "sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y incus"]})
            plan.notes.append("the distribution's Incus is old (Ubuntu 24.04 ships 6.0.x); add --zabbly to install the "
                              "current release from the Zabbly repository instead")
    if installed and version and not old_incus:
        plan.notes.append(f"Incus {version}" + (" (Zabbly repository)" if facts.get("zabbly_repo") == "1" else ""))
    if old_incus and not zabbly:
        plan.notes.append(
            f"Incus {version} is older than {OLD_INCUS_BELOW[0]}.{OLD_INCUS_BELOW[1]}: Docker with a current runc cannot "
            "start containers inside workspaces on it, so devbox registers runc 1.2.3 in each workspace as a "
            f"workaround. `devbox host bootstrap {host} --zabbly --yes` upgrades Incus from the Zabbly repository")
    inc = "incus" if access == "direct" else "sudo -n incus"
    initialized = installed and facts.get("storage_pools", "0") != "0" and facts.get("bridge") == "1"
    if not initialized and not plan.blockers:
        plan.steps.append({"id": "init-incus", "title": "initialise Incus with defaults (bridge incusbr0, dir storage)",
                           "commands": [f"{inc} admin init --minimal"]})
    if installed and access == "sudo":
        plan.notes.append(f"{facts.get('user')} is not in incus-admin; devbox will run incus through `sudo -n`")
    elif not installed and sudo:
        plan.notes.append("after the install, devbox will run incus through `sudo -n` unless the user joins incus-admin")

    keys = facts.get("keys_maxkeys", "")
    if installed and keys.isdigit() and int(keys) < KEYS_LOW_BELOW:
        plan.manual.append(
            f"kernel.keys.maxkeys is {keys}. All workspaces share this key quota (15 to 30 keys each, more with "
            "Docker containers running), so around a dozen workspaces exhaust it and Docker then fails with "
            f"'disk quota exceeded'. devbox does not change kernel settings (`--zabbly` brings a package that sets "
            f"it to {KEYS_RECOMMENDED}); or raise it yourself: "
            f"`echo 'kernel.keys.maxkeys = {KEYS_RECOMMENDED}' | sudo tee /etc/sysctl.d/99-devbox-incus.conf && "
            "sudo sysctl --system`")
    ufw_active = facts.get("ufw_status", "").lstrip().startswith("Status: active")
    forward_drop = facts.get("forward_policy", "").endswith("DROP")
    if ufw_active:
        missing = [k for k in UFW_RULES if k not in ufw_rules_present(facts["ufw_status"])]
        if missing and not sudo:
            plan.blockers.append("UFW is active and needs rules for incusbr0, but there is no passwordless sudo")
        elif missing:
            reason = "UFW is active" + (" and the FORWARD policy is DROP (Docker)" if forward_drop else "")
            plan.steps.append({
                "id": "ufw-incusbr0",
                "title": f"allow the Incus bridge through UFW ({reason}); without it containers get no IPv4/internet",
                "commands": ["sudo -n " + " ".join(UFW_RULES[k]) for k in UFW_RULES if k in missing]})
    elif forward_drop:
        plan.manual.append(
            "the iptables FORWARD policy is DROP but UFW is not active, so containers may get no network. "
            "devbox does not edit Docker's or raw iptables rules; allow incusbr0 yourself "
            "(for Docker: `iptables -I DOCKER-USER -i incusbr0 -j ACCEPT` and `-o incusbr0 -m conntrack "
            "--ctstate RELATED,ESTABLISHED -j ACCEPT`)")
    return plan


def collect_facts(shell: HostShell) -> dict:
    return parse_facts(shell.run(FACTS_SCRIPT, timeout=60).stdout)


def apply_plan(shell: HostShell, plan: Plan, say: Callable[[str], None] = lambda m: None) -> dict:
    """Run the steps of a plan, then read the facts again. Raises on the first failing command."""
    if plan.blockers:
        raise DevboxError("cannot bootstrap " + plan.host + ": " + "; ".join(plan.blockers))
    for step in plan.steps:
        say(step["title"])
        for command in step["commands"]:
            proc = shell.run(f"( {command} ) </dev/null", timeout=900, check=False)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise DevboxError(f"step '{step['id']}' failed on {plan.host}: `{command}` exited "
                                  f"{proc.returncode}{': ' + detail[-500:] if detail else ''}")
    return collect_facts(shell)
