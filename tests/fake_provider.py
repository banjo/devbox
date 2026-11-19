"""A provider fake for tests: implements the Provider protocol in memory."""

from devbox.providers import Access, Capabilities
from devbox.providers.hetzner import HetznerProvider


def offer(type, location, available=True, price=5.0, cores=2, memory=4.0):
    return {
        "type": type, "location": location, "city": "X", "country": "DE",
        "network_zone": "eu-central", "cores": cores, "memory_gb": memory, "disk_gb": 40,
        "architecture": "x86", "cpu_type": "shared", "category": "cost_optimized",
        "price_hourly": 0.01, "price_monthly": price, "available": available, "recommended": False,
    }


class FakeProvider:
    """In-memory provider implementing `devbox.providers.Provider`.

    Registered under its `name` (default "hetzner") by the `cloud` fixture in
    test_cli.py, so the CLI talks to it exactly as it would to a real provider.
    Set `capabilities` to try providers that cannot create, delete, snapshot or bill.
    """

    name = "hetzner"
    display_name = "Hetzner"
    capabilities = Capabilities(
        can_create=True, can_delete=True, has_snapshots=True, has_billing=True, has_offers=True,
    )
    binary = "hcloud"
    token_error = None
    deadline = None
    stage = "prepare"

    def __init__(self, token=None, controller_id="test"):
        self.controller_id = controller_id
        self.servers: dict[int, dict] = {}
        self.foreign: list[dict] = []
        self.images: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.next_id = 100
        self.fail_snapshot = False
        self.fail_delete = False
        self.running_work = []
        self.offer_list = [
            offer("cx23", "fsn1", price=4.0),
            offer("cx33", "fsn1", price=7.0, cores=4, memory=8.0),
        ]

    def from_config(self, cfg):
        return self

    def _id(self):
        self.next_id += 1
        return self.next_id

    def list_servers(self):
        return [dict(s) for s in self.servers.values()]

    def list_all_servers(self):
        mine = [{"controller": self.controller_id, **s} for s in self.servers.values()]
        return mine + [dict(s) for s in self.foreign]

    def list_snapshots(self):
        return [dict(i) for i in self.images.values()]

    def offers(self):
        return [dict(o) for o in self.offer_list]

    def ensure_ssh_key(self, public_key):
        return 1

    def ensure_firewall(self, cidrs):
        return 2

    def ensure_access(self, public_key, ssh_cidrs):
        return Access(ssh_key_id=self.ensure_ssh_key(public_key), firewall_id=self.ensure_firewall(ssh_cidrs))

    # Doctor and cleanup work on the fake's firewalls and keys like on Hetzner's.
    check_connection = HetznerProvider.check_connection
    check_access = HetznerProvider.check_access
    leftovers = HetznerProvider.leftovers
    delete_resource = HetznerProvider.delete_resource

    def create_server(self, name, project, server_type, image, location, user_data, ssh_key_id, firewall_id):
        self.calls.append(("create", server_type, location, image))
        sid = self._id()
        self.servers[sid] = {
            "id": sid, "name": name, "status": "running", "ip": f"192.0.2.{sid % 250}",
            "server_type": server_type, "location": location, "project": project, "created": None,
        }
        return dict(self.servers[sid])

    def delete_server(self, server_id):
        self.calls.append(("delete", server_id))
        if self.fail_delete:
            from devbox import DevboxError
            raise DevboxError("locked")
        del self.servers[server_id]

    def shutdown(self, server_id):
        self.calls.append(("shutdown", server_id))
        self.servers[server_id]["status"] = "off"

    def poweron(self, server_id):
        self.calls.append(("poweron", server_id))
        self.servers[server_id]["status"] = "running"

    def create_snapshot(self, server_id, project, description):
        self.calls.append(("snapshot", server_id))
        if self.fail_snapshot:
            from devbox import DevboxError
            raise DevboxError("boom")
        iid = self._id()
        self.images[iid] = {
            "id": iid, "description": description, "status": "available", "size_gb": 2.5,
            "disk_gb": 40, "architecture": "x86", "project": project, "created": None,
        }
        return dict(self.images[iid])

    def delete_image(self, image_id):
        self.calls.append(("delete_image", image_id))
        del self.images[image_id]

    def snapshot_price_per_gb_month(self):
        return 0.012

    # Most tests leave IPv4 out of costs; test_costs_ipv4_36 sets prices.
    ipv4: dict = {}

    def ipv4_prices(self):
        return dict(self.ipv4)

    # Firewalls, SSH keys and ownership, as in Hcloud.
    resource_name = "devbox-test"
    firewalls: list
    keys: list

    def owns(self, r):
        return (r.get("labels") or {}) == {"managed-by": "devbox", "devbox-controller": "test"}

    @staticmethod
    def is_legacy(r):
        labels = r.get("labels") or {}
        return labels.get("managed-by") == "devbox" and "devbox-controller" not in labels

    def version(self):
        return "1.70.2"

    def list_firewalls(self):
        return list(getattr(self, "firewalls", []))

    def list_ssh_keys(self):
        return list(getattr(self, "keys", []))

    def find_firewall(self):
        return next((f for f in self.list_firewalls() if f["name"] == self.resource_name), None)

    def find_ssh_key(self, public_key):
        for k in self.list_ssh_keys():
            if k["public_key"] == public_key:
                return {"status": "owned" if self.owns(k) else "legacy", "key": k}
        return {"status": "missing", "key": None}

    def delete_firewall(self, firewall_id):
        self.calls.append(("delete_firewall", firewall_id))
        self.firewalls = [f for f in self.firewalls if f["id"] != firewall_id]

    def delete_ssh_key(self, key_id):
        self.calls.append(("delete_ssh_key", key_id))
        self.keys = [k for k in self.keys if k["id"] != key_id]
