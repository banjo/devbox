import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from typing import Callable

from devbox import DevboxError
from devbox.providers.base import Access, Capabilities
from devbox.startup import StageTimeout

MANAGED_LABEL = "managed-by=devbox"
CONTROLLER_LABEL = "devbox-controller"


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


class HetznerProvider:
    """Hetzner Cloud servers through the official `hcloud` CLI.

    Every resource devbox creates carries `managed-by=devbox` and
    `devbox-controller=<id>`, where the id identifies this machine's devbox
    install. Resources without both labels are never modified.
    """

    name = "hetzner"
    display_name = "Hetzner"
    capabilities = Capabilities(
        can_create=True, can_delete=True, has_snapshots=True, has_billing=True, has_offers=True,
    )

    @classmethod
    def from_config(cls, cfg) -> "HetznerProvider":
        # An unreadable token file is reported by the first call, so that
        # `doctor` can still say that the hcloud CLI is missing.
        try:
            token, error = cfg.hcloud_token(), None
        except DevboxError as e:
            token, error = None, e
        provider = cls(token, cfg.get_controller_id())
        provider.token_error = error
        return provider

    def __init__(self, token: str | None, controller_id: str, binary: str = "hcloud"):
        self.binary = binary
        self.controller_id = controller_id
        # Set by `up`: every hcloud call then ends by the startup deadline.
        self.deadline = None
        self.token_error: DevboxError | None = None
        self.stage = "prepare"
        self._pricing: dict | None | bool = False
        self.env = dict(os.environ)
        if token:
            self.env["HCLOUD_TOKEN"] = token

    @property
    def resource_name(self) -> str:
        """Name for the shared SSH key and firewall of this controller."""
        return f"devbox-{self.controller_id}"

    @property
    def labels(self) -> dict[str, str]:
        return {"managed-by": "devbox", CONTROLLER_LABEL: self.controller_id}

    def label_args(self, **extra: str) -> list[str]:
        args = []
        for k, v in {**self.labels, **extra}.items():
            args += ["--label", f"{k}={v}"]
        return args

    def owns(self, resource: dict) -> bool:
        labels = resource.get("labels") or {}
        return all(labels.get(k) == v for k, v in self.labels.items())

    @staticmethod
    def is_legacy(resource: dict) -> bool:
        """Created by a devbox version before controller ids."""
        labels = resource.get("labels") or {}
        return labels.get("managed-by") == "devbox" and CONTROLLER_LABEL not in labels

    def version(self) -> str | None:
        try:
            out = self.run("version", parse=False)
        except DevboxError:
            return None
        match = re.search(r"v?(\d+\.\d+\.\d+)", out)
        return match.group(1) if match else out.strip() or None

    def list_ssh_keys(self) -> list[dict]:
        return self.run("ssh-key", "list", "-o", "json") or []

    def list_firewalls(self) -> list[dict]:
        return self.run("firewall", "list", "-o", "json") or []

    def _deletable(self, resource: dict, kind: str) -> None:
        if not (self.owns(resource) or self.is_legacy(resource)):
            raise DevboxError(
                f"{kind} {resource['id']} is not owned by this devbox install; refusing to delete it"
            )

    def delete_firewall(self, firewall_id: int) -> None:
        fw = self.run("firewall", "describe", str(firewall_id), "-o", "json")
        self._deletable(fw or {"id": firewall_id}, "firewall")
        if (fw or {}).get("applied_to"):
            raise DevboxError(f"firewall {firewall_id} is still attached to resources; refusing to delete it")
        self.run("firewall", "delete", str(firewall_id), parse=False)

    def delete_ssh_key(self, key_id: int) -> None:
        key = self.run("ssh-key", "describe", str(key_id), "-o", "json")
        self._deletable(key or {"id": key_id}, "SSH key")
        self.run("ssh-key", "delete", str(key_id), parse=False)

    def run(self, *args: str, input: str | None = None, parse: bool = True) -> Any:
        if self.token_error is not None:
            raise self.token_error
        cmd = [self.binary, *args]
        timeout = 600 if self.deadline is None else self.deadline.bound(self.stage, 600)
        try:
            proc = subprocess.run(
                cmd,
                env=self.env,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise DevboxError("hcloud CLI not found; install it from https://github.com/hetznercloud/cli")
        except subprocess.TimeoutExpired:
            if self.deadline is not None:
                raise StageTimeout(
                    self.stage,
                    f"hcloud {' '.join(args[:2])} did not finish before the startup deadline; "
                    f"the action may still complete at Hetzner",
                )
            raise DevboxError(f"timed out: hcloud {' '.join(args)}")
        if proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip()
            raise DevboxError(f"hcloud {' '.join(args[:2])} failed: {msg}")
        if not parse:
            return proc.stdout
        out = proc.stdout.strip()
        return json.loads(out) if out else None

    # Servers

    def list_servers(self) -> list[dict]:
        """Servers of this controller, plus servers from before controller labels existed."""
        return [s for s in self.list_all_servers() if s["controller"] in (None, self.controller_id)]

    def list_all_servers(self) -> list[dict]:
        """Every devbox server in the Hetzner project, including other controllers' (read only).

        `controller` is the owning controller id, None for servers from before
        controller labels existed.
        """
        data = self.run("server", "list", "-l", MANAGED_LABEL, "-o", "json") or []
        return [server_info(s) for s in data]

    def create_server(
        self,
        name: str,
        project: str,
        server_type: str,
        image: str,
        location: str,
        user_data: Path,
        ssh_key_id: int,
        firewall_id: int,
    ) -> dict:
        data = self.run(
            "server", "create",
            "--name", name,
            "--type", server_type,
            "--image", image,
            "--location", location,
            "--ssh-key", str(ssh_key_id),
            "--firewall", str(firewall_id),
            *self.label_args(project=project),
            "--user-data-from-file", str(user_data),
            "-o", "json",
        )
        server = data.get("server", data) if isinstance(data, dict) else data
        return server_info(server)

    def delete_server(self, server_id: int) -> None:
        self.run("server", "delete", str(server_id), parse=False)

    def shutdown(self, server_id: int) -> None:
        """Graceful shutdown; hard power-off if the server does not stop in time."""
        try:
            self.run("server", "shutdown", "--wait", "--wait-timeout", "120s", str(server_id), parse=False)
        except DevboxError:
            self.run("server", "poweroff", str(server_id), parse=False)

    def poweron(self, server_id: int) -> None:
        self.run("server", "poweron", str(server_id), parse=False)

    # Snapshots

    def list_snapshots(self) -> list[dict]:
        """Snapshots of this controller, plus snapshots without a controller label."""
        data = self.run("image", "list", "-t", "snapshot", "-l", MANAGED_LABEL, "-o", "json") or []
        result = []
        for image in data:
            owner = (image.get("labels") or {}).get(CONTROLLER_LABEL)
            if owner in (None, self.controller_id):
                result.append(image_info(image))
        return result

    def create_snapshot(self, server_id: int, project: str, description: str) -> dict:
        """Create a snapshot and wait until it is available."""
        op = secrets.token_hex(6)
        self.run(
            "server", "create-image", "--type", "snapshot",
            "--description", description,
            *self.label_args(project=project, **{"devbox-op": op}),
            str(server_id),
            parse=False,
        )
        # create-image has no JSON output; find the image by its unique label.
        deadline = time.monotonic() + 1800
        while True:
            data = self.run(
                "image", "list", "-t", "snapshot", "-l", f"devbox-op={op}", "-o", "json"
            ) or []
            info = image_info(data[0]) if data else None
            if info and info["status"] == "available":
                return info
            if time.monotonic() > deadline:
                state = info["status"] if info else "missing"
                raise DevboxError(f"snapshot did not become available in time (status: {state})")
            time.sleep(5)

    def delete_image(self, image_id: int) -> None:
        data = self.run("image", "describe", str(image_id), "-o", "json")
        if not data:
            raise DevboxError(f"image {image_id} not found")
        # Like firewalls and SSH keys: this install's, or from a version before controller ids.
        self._deletable(data, "image")
        self.run("image", "delete", str(image_id), parse=False)

    def pricing(self) -> dict | None:
        """Hetzner's pricing API (read once per run), or None if it cannot be read."""
        if self._pricing is False:
            self._pricing = None
            token = self.env.get("HCLOUD_TOKEN")
            if token:
                req = urllib.request.Request(
                    "https://api.hetzner.cloud/v1/pricing", headers={"Authorization": f"Bearer {token}"}
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        self._pricing = json.load(resp)["pricing"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        return self._pricing

    def snapshot_price_per_gb_month(self) -> float | None:
        """Gross snapshot price from Hetzner's pricing API, or None if it cannot be read."""
        try:
            return _price(self.pricing()["image"]["price_per_gb_month"])
        except (KeyError, TypeError):
            return None

    def ipv4_prices(self) -> dict[str, tuple[float | None, float | None]]:
        """Gross (hourly, monthly) price of a primary IPv4 per location; empty if unknown."""
        try:
            ips = self.pricing()["primary_ips"]
            ipv4 = next(p for p in ips if p.get("type") == "ipv4")
            return {p["location"]: (_price(p.get("price_hourly")), _price(p.get("price_monthly")))
                    for p in ipv4.get("prices") or [] if p.get("location")}
        except (KeyError, TypeError, StopIteration):
            return {}

    # Server types and locations

    def offers(self) -> list[dict]:
        types = self.run("server-type", "list", "-o", "json") or []
        locations = self.run("location", "list", "-o", "json") or []
        return build_offers(types, locations)

    # SSH key and firewall

    def _find_owned(self, kind: str, name: str) -> dict | None:
        """Find a resource by name, refusing to touch it if devbox does not own it."""
        items = self.run(kind, "list", "-o", "json") or []
        for item in items:
            if item["name"] != name:
                continue
            if not self.owns(item):
                raise DevboxError(
                    f"a Hetzner {kind} named '{name}' (id {item['id']}) exists but is not "
                    f"labeled as owned by this devbox install; refusing to modify it"
                )
            return item
        return None

    def find_ssh_key(self, public_key: str) -> dict:
        """Where the local public key stands in Hetzner, without changing anything.

        Returns {"status": "owned" | "legacy" | "missing", "key": dict | None}.
        Raises if the key name is taken by something else, or the same public
        key is registered under an owner devbox must not use.
        """
        name = self.resource_name
        key = self._find_owned("ssh-key", name)
        if key:
            if not _same_key(key["public_key"], public_key):
                raise DevboxError(f"Hetzner SSH key '{name}' does not match the local devbox key")
            return {"status": "owned", "key": key}

        # Hetzner allows each public key only once per project, so the local key
        # may already be registered under another name.
        keys = self.list_ssh_keys()
        same = [k for k in keys if _same_key(k["public_key"], public_key)]
        for k in same:
            if self.owns(k):
                return {"status": "owned", "key": k}
            if self.is_legacy(k):
                # Created by a devbox version before controller ids; reuse it unchanged.
                return {"status": "legacy", "key": k}
        if same:
            k = same[0]
            labels = k.get("labels") or {}
            if labels.get("managed-by") == "devbox":
                owner = f"another devbox install (controller '{labels.get(CONTROLLER_LABEL)}')"
            else:
                owner = "something other than devbox"
            raise DevboxError(
                f"the local devbox SSH key is already registered in Hetzner as '{k['name']}' "
                f"(id {k['id']}), owned by {owner}, and Hetzner does not allow the same key twice. "
                f"Either point `ssh_key` in the devbox config at a different key, or, if that key "
                f"may be used by this install, label it: "
                f"hcloud ssh-key add-label --overwrite {k['id']} managed-by=devbox "
                f"{CONTROLLER_LABEL}={self.controller_id}"
            )
        return {"status": "missing", "key": None}

    def ensure_ssh_key(self, public_key: str) -> int:
        found = self.find_ssh_key(public_key)
        if found["key"]:
            return found["key"]["id"]
        name = self.resource_name
        data = self.run(
            "ssh-key", "create",
            "--name", name,
            "--public-key", public_key,
            *self.label_args(),
            "-o", "json",
        )
        return self._created_id(data, "ssh_key", "ssh-key", name)

    def find_firewall(self) -> dict | None:
        """This install's firewall, or None. Raises if the name is taken by something else."""
        return self._find_owned("firewall", self.resource_name)

    def ensure_firewall(self, ssh_cidrs: list[str]) -> int:
        name = self.resource_name
        wanted = firewall_rules(ssh_cidrs)
        rules = json.dumps(wanted)
        firewall = self._find_owned("firewall", name)
        if firewall:
            # Replacing rules also re-applies them to every attached server; right after `pause` that can
            # include the server just deleted (#34). Skip it when nothing changes.
            if same_rules(firewall.get("rules"), wanted):
                return firewall["id"]
            try:
                self.run(
                    "firewall", "replace-rules", str(firewall["id"]), "--rules-file", "-",
                    input=rules, parse=False,
                )
            except DevboxError as e:
                # Hetzner sets the rules first, then applies them per server. Only the apply to a
                # deleted server failed if the rules are in place.
                if "resource_no_longer_exists" not in str(e):
                    raise
                current = self.run("firewall", "describe", str(firewall["id"]), "-o", "json") or {}
                if not same_rules(current.get("rules"), wanted):
                    raise
            return firewall["id"]
        data = self.run(
            "firewall", "create",
            "--name", name,
            *self.label_args(),
            "--rules-file", "-",
            "-o", "json",
            input=rules,
        )
        return self._created_id(data, "firewall", "firewall", name)

    def ensure_access(self, public_key: str, ssh_cidrs: list[str]) -> Access:
        return Access(ssh_key_id=self.ensure_ssh_key(public_key), firewall_id=self.ensure_firewall(ssh_cidrs))

    # Housekeeping for `doctor` and `cleanup`

    def check_connection(self, cfg) -> tuple[list[dict], bool]:
        checks: list[dict] = []
        add = checks.append
        if shutil.which(self.binary) is None:
            add(_check("hcloud", "fail", "hcloud CLI not found", "install it from https://github.com/hetznercloud/cli"))
            return checks, False
        if self.token_error is not None:
            raise self.token_error
        add(_check("hcloud", "ok", f"version {self.version() or 'unknown'}"))
        try:
            token = cfg.hcloud_token()
        except DevboxError as e:
            add(_check("token", "fail", str(e), "check hcloud_token_file in the config"))
            return checks, False
        if not token:
            add(_check("token", "warn", "no hcloud_token_file or HCLOUD_TOKEN; relying on the active hcloud context",
                       "set hcloud_token_file in ~/.config/devbox/config.toml"))
        else:
            add(_check("token", "ok", "token found"))
        try:
            servers = self.list_servers()
        except DevboxError as e:
            add(_check("hetzner api", "fail", str(e), "check the token and network access"))
            return checks, False
        add(_check("hetzner api", "ok", f"reachable, {len(servers)} devbox server(s)"))
        try:
            offers = self.offers()
            if offers and all(o["available"] is None for o in offers):
                add(_check("availability", "warn", "hcloud does not report per-location availability",
                           "update hcloud to a recent version"))
            else:
                add(_check("availability", "ok", f"{sum(1 for o in offers if o['available'])} type/location offers available"))
        except DevboxError as e:
            add(_check("availability", "warn", str(e)))
        return checks, True

    def check_access(self, public_key: str | None, ssh_cidrs: Callable[[], list[str]]) -> list[dict]:
        checks: list[dict] = []
        add = checks.append
        if public_key:
            try:
                found = self.find_ssh_key(public_key)
                key = found["key"]
                if found["status"] == "owned":
                    add(_check("hetzner ssh key", "ok", f"'{key['name']}' (id {key['id']})"))
                elif found["status"] == "legacy":
                    add(_check("hetzner ssh key", "ok", f"using the key '{key['name']}' (id {key['id']}) from an older devbox version"))
                else:
                    add(_check("hetzner ssh key", "ok", f"not registered yet; `up` creates '{self.resource_name}'"))
            except DevboxError as e:
                add(_check("hetzner ssh key", "fail", str(e)))
        try:
            fw = self.find_firewall()
            if fw is None:
                add(_check("firewall", "ok", f"not created yet; `up` creates '{self.resource_name}'"))
            else:
                current = set(ssh_cidrs())
                allowed = {ip for rule in fw.get("rules") or [] for ip in rule.get("source_ips") or []}
                if current <= allowed:
                    add(_check("firewall", "ok", f"'{fw['name']}' allows SSH from {', '.join(sorted(current))}"))
                else:
                    add(_check("firewall", "warn",
                               f"'{fw['name']}' allows {', '.join(sorted(allowed)) or 'nothing'}, this machine is "
                               f"{', '.join(sorted(current))}",
                               "the next `devbox up` updates the rules; running servers can't be reached until then"))
        except DevboxError as e:
            add(_check("firewall", "fail", str(e)))
        return checks

    def leftovers(self, local_public_key: str | None, notes: list[str]) -> list[dict]:
        """Firewalls and SSH keys of this install that nothing uses. Servers and snapshots are found by reconcile."""
        plan: list[dict] = []
        for fw in self.list_firewalls():
            if fw["name"] == self.resource_name or not (self.owns(fw) or self.is_legacy(fw)):
                continue
            if fw.get("applied_to"):
                continue
            plan.append({"kind": "firewall", "id": fw["id"], "name": fw["name"],
                         "reason": "devbox firewall not attached to any server" + (" (older version)" if self.is_legacy(fw) else "")})
        if local_public_key is None:
            notes.append("SSH keys not checked: the local devbox key doesn't exist, so the key in use is unknown")
            return plan
        try:
            self.find_ssh_key(local_public_key)
        except DevboxError as e:
            # Which key is in use is ambiguous; listing any key could remove the one in use.
            notes.append(f"SSH keys not checked: {e}")
            return plan
        for key in self.list_ssh_keys():
            if not (self.owns(key) or self.is_legacy(key)):
                continue
            if _same_key(key.get("public_key", ""), local_public_key):
                continue  # never the key devbox itself uses
            plan.append({"kind": "ssh-key", "id": key["id"], "name": key["name"],
                         "reason": "devbox SSH key that is not the local key"})
        return plan

    def delete_resource(self, kind: str, resource_id) -> None:
        if kind == "firewall":
            self.delete_firewall(resource_id)
        elif kind == "ssh-key":
            self.delete_ssh_key(resource_id)
        else:
            raise DevboxError(f"unknown {self.display_name} resource kind '{kind}'")

    def _created_id(self, data: Any, key: str, kind: str, name: str) -> int:
        if isinstance(data, dict):
            obj = data.get(key, data)
            if isinstance(obj, dict) and "id" in obj:
                return obj["id"]
        item = self._find_owned(kind, name)
        if item is None:
            raise DevboxError(f"created {kind} '{name}' but cannot find it")
        return item["id"]


def firewall_rules(ssh_cidrs: list[str]) -> list[dict]:
    return [
        {
            "direction": "in",
            "protocol": "tcp",
            "port": "22",
            "source_ips": ssh_cidrs,
            "description": "ssh from devbox controller",
        }
    ]


def same_rules(current: list[dict] | None, wanted: list[dict]) -> bool:
    """Whether a firewall's rules (as hcloud lists them) are `wanted`, ignoring order and description."""
    def key(rules):
        return sorted(
            (r.get("direction"), r.get("protocol"), r.get("port"), tuple(sorted(r.get("source_ips") or [])),
             tuple(sorted(r.get("destination_ips") or [])))
            for r in rules or []
        )
    return key(current) == key(wanted)


def build_offers(server_types: list[dict], locations: list[dict]) -> list[dict]:
    """One entry per (server type, location) that can be ordered, cheapest first.

    `available` is Hetzner's current stock indicator; it is not a guarantee, and
    is None when the hcloud version does not report it.
    """
    loc_info = {loc["name"]: loc for loc in locations}
    offers = []
    for st in server_types:
        if st.get("deprecation") or st.get("deprecated"):
            continue
        prices = {p["location"]: p for p in st.get("prices") or []}
        for loc in st.get("locations") or []:
            if loc.get("deprecation"):
                continue
            name = loc["name"]
            price = prices.get(name, {})
            info = loc_info.get(name, {})
            offers.append({
                "type": st["name"],
                "location": name,
                "city": info.get("city"),
                "country": info.get("country"),
                "network_zone": info.get("network_zone"),
                "cores": st.get("cores"),
                "memory_gb": st.get("memory"),
                "disk_gb": st.get("disk"),
                "architecture": st.get("architecture"),
                "cpu_type": st.get("cpu_type"),
                "category": st.get("category"),
                "price_hourly": _price(price.get("price_hourly")),
                "price_monthly": _price(price.get("price_monthly")),
                "available": loc.get("available"),
                "recommended": loc.get("recommended"),
            })
    offers.sort(key=lambda o: (o["price_monthly"] is None, o["price_monthly"] or 0, o["type"], o["location"]))
    # Older hcloud versions do not know the field and report false everywhere.
    if offers and not any(o["available"] for o in offers):
        for o in offers:
            o["available"] = None
    return offers


def filter_offers(
    offers: list[dict],
    min_cores: int | None = None,
    min_memory: float | None = None,
    architecture: str | None = None,
    location: str | None = None,
    network_zone: str | None = None,
    include_unavailable: bool = False,
) -> list[dict]:
    result = []
    for o in offers:
        if not include_unavailable and o["available"] is False:
            continue
        if min_cores and (o["cores"] or 0) < min_cores:
            continue
        if min_memory and (o["memory_gb"] or 0) < min_memory:
            continue
        if architecture and o["architecture"] != architecture:
            continue
        if location and o["location"] != location:
            continue
        if network_zone and o["network_zone"] != network_zone:
            continue
        result.append(o)
    return result


def _price(p: dict | None) -> float | None:
    if not p:
        return None
    value = p.get("gross") or p.get("net")
    return float(value) if value is not None else None


def image_info(i: dict) -> dict:
    return {
        "id": i["id"],
        "description": i.get("description"),
        "status": i.get("status"),
        "size_gb": i.get("image_size"),
        "disk_gb": i.get("disk_size"),
        "architecture": i.get("architecture"),
        "project": (i.get("labels") or {}).get("project"),
        "created": i.get("created"),
    }


def server_info(s: dict) -> dict:
    """Pick out the fields devbox cares about from an hcloud server object."""
    location = s.get("location") or (s.get("datacenter") or {}).get("location") or {}
    ipv4 = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip")
    return {
        "id": s["id"],
        "name": s.get("name"),
        "status": s.get("status"),
        "ip": ipv4,
        "server_type": (s.get("server_type") or {}).get("name"),
        "location": location.get("name"),
        "project": (s.get("labels") or {}).get("project"),
        "controller": (s.get("labels") or {}).get(CONTROLLER_LABEL),
        "created": s.get("created"),
    }


def _same_key(a: str, b: str) -> bool:
    return a.split()[:2] == b.split()[:2]


# Name used before the provider interface existed.
Hcloud = HetznerProvider
