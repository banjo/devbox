"""The `ssh` provider: machines you own, reached over SSH (`devbox host add`).

devbox does not create, power off, snapshot or bill these machines; it only checks they are
reachable. The work happens in Incus containers on them (see `devbox.incus`).
"""

from pathlib import Path
from typing import Callable, ClassVar

from devbox import DevboxError, db
from devbox.hosts import Host, HostShell
from devbox.providers.base import Access, Capabilities, Check, require


class SshProvider:
    name: ClassVar[str] = "ssh"
    display_name: ClassVar[str] = "your own hosts"
    capabilities: ClassVar[Capabilities] = Capabilities(
        can_create=False, can_delete=False, has_snapshots=False, has_billing=False, has_offers=False,
        workspaces=True,
    )

    def __init__(self, controller_id: str, state_dir: Path, conn_factory: Callable):
        self.controller_id = controller_id
        self.state_dir = state_dir
        self._conn_factory = conn_factory
        self.deadline = None
        self.stage = "prepare"
        self.runner = None  # tests replace the subprocess runner of every HostShell

    @classmethod
    def from_config(cls, cfg) -> "SshProvider":
        return cls(cfg.get_controller_id(), cfg.state_dir, lambda: db.connect(cfg.db_path))

    @property
    def resource_name(self) -> str:
        return f"devbox-{self.controller_id}"

    def version(self) -> str | None:
        return None

    # Hosts

    def host(self, name: str) -> Host:
        row = db.get_host(self._conn_factory(), name)
        if row is None:
            known = ", ".join(r["name"] for r in db.list_hosts(self._conn_factory())) or "none"
            raise DevboxError(f"unknown host '{name}' (known hosts: {known}); add one with "
                              f"`devbox host add {name} --ssh USER@HOST`")
        return Host.from_row(row)

    def shell(self, host: Host) -> HostShell:
        return HostShell(host, self.state_dir, self.runner)

    def ensure_host(self, name: str) -> Host:
        """The host's registry entry, after checking that it answers with the pinned key."""
        host = self.host(name)
        self.shell(host).run("true", timeout=30)
        return host

    # Machines are not managed here: there is nothing to list, create, delete or snapshot.

    def list_servers(self) -> list[dict]:
        return []

    def list_all_servers(self) -> list[dict]:
        return []

    def create_server(self, *args, **kwargs) -> dict:
        require(self, "can_create", "creating machines")
        raise AssertionError("unreachable")

    def delete_server(self, server_id) -> None:
        require(self, "can_delete", "deleting machines")

    def poweron(self, server_id) -> None:
        require(self, "can_delete", "powering machines on")

    def shutdown(self, server_id) -> None:
        require(self, "can_delete", "powering machines off")

    def list_snapshots(self) -> list[dict]:
        return []

    def create_snapshot(self, *args, **kwargs) -> dict:
        require(self, "has_snapshots", "machine snapshots")
        raise AssertionError("unreachable")

    def delete_image(self, image_id) -> None:
        require(self, "has_snapshots", "machine snapshots")

    def offers(self) -> list[dict]:
        return []

    def snapshot_price_per_gb_month(self) -> float | None:
        return None

    def ipv4_prices(self) -> dict:
        return {}

    def ensure_access(self, public_key: str, ssh_cidrs: list[str]) -> Access:
        require(self, "can_create", "creating machines")
        raise AssertionError("unreachable")

    def check_connection(self, cfg) -> tuple[list[Check], bool]:
        checks = []
        for row in db.list_hosts(self._conn_factory()):
            host = Host.from_row(row)
            try:
                self.shell(host).run("true", timeout=30)
                checks.append({"check": f"host {host.name}", "status": "ok", "detail": f"reachable ({host.ssh})", "fix": None})
            except DevboxError as e:
                checks.append({"check": f"host {host.name}", "status": "fail", "detail": str(e),
                               "fix": f"check the machine and its SSH access; `devbox host remove {host.name}` forgets it"})
        return checks, True

    def check_access(self, public_key, ssh_cidrs) -> list[Check]:
        return []

    def leftovers(self, local_public_key, notes) -> list[dict]:
        return []

    def delete_resource(self, kind: str, resource_id) -> None:
        raise DevboxError(f"{self.display_name} have no resources devbox deletes")
