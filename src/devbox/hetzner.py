import json
import os
import subprocess
from pathlib import Path
from typing import Any

from devbox import DevboxError

MANAGED_LABEL = "managed-by=devbox"
CONTROLLER_LABEL = "devbox-controller"


class Hcloud:
    """Thin wrapper around the official `hcloud` CLI.

    Every resource devbox creates carries `managed-by=devbox` and
    `devbox-controller=<id>`, where the id identifies this machine's devbox
    install. Resources without both labels are never modified.
    """

    def __init__(self, token: str | None, controller_id: str, binary: str = "hcloud"):
        self.binary = binary
        self.controller_id = controller_id
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

    def run(self, *args: str, input: str | None = None, parse: bool = True) -> Any:
        cmd = [self.binary, *args]
        try:
            proc = subprocess.run(
                cmd,
                env=self.env,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            raise DevboxError("hcloud CLI not found; install it from https://github.com/hetznercloud/cli")
        except subprocess.TimeoutExpired:
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
        data = self.run("server", "list", "-l", MANAGED_LABEL, "-o", "json") or []
        result = []
        for s in data:
            owner = (s.get("labels") or {}).get(CONTROLLER_LABEL)
            if owner in (None, self.controller_id):
                result.append(server_info(s))
        return result

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

    def ensure_ssh_key(self, public_key: str) -> int:
        name = self.resource_name
        key = self._find_owned("ssh-key", name)
        if key:
            if not _same_key(key["public_key"], public_key):
                raise DevboxError(f"Hetzner SSH key '{name}' does not match the local devbox key")
            return key["id"]
        data = self.run(
            "ssh-key", "create",
            "--name", name,
            "--public-key", public_key,
            *self.label_args(),
            "-o", "json",
        )
        return self._created_id(data, "ssh_key", "ssh-key", name)

    def ensure_firewall(self, ssh_cidrs: list[str]) -> int:
        name = self.resource_name
        rules = json.dumps(firewall_rules(ssh_cidrs))
        firewall = self._find_owned("firewall", name)
        if firewall:
            self.run(
                "firewall", "replace-rules", str(firewall["id"]), "--rules-file", "-",
                input=rules, parse=False,
            )
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
        "created": s.get("created"),
    }


def _same_key(a: str, b: str) -> bool:
    return a.split()[:2] == b.split()[:2]
