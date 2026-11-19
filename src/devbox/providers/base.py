"""The provider interface: what devbox needs from the place servers come from.

A provider manages hosts (today: Hetzner Cloud servers). Commands ask a
provider's `capabilities` what it can do instead of checking its name, so a
provider that cannot create, delete or snapshot machines (or does not bill)
is refused or skipped where that matters.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do. Commands branch on these, never on provider names."""

    can_create: bool = False  # create servers (`up` on a clean host)
    can_delete: bool = False  # delete servers (`down`, `pause`, `cleanup`)
    has_snapshots: bool = False  # machine images (`pause`, restore on `up`, `snapshot ...`)
    has_billing: bool = False  # bills by time: costs, billing hours, `--at-hour-end`
    has_offers: bool = False  # lists orderable server types and prices (`server-types`)


@dataclass(frozen=True)
class Access:
    """Ids from `ensure_access` that `create_server` attaches to a new server."""

    ssh_key_id: int
    firewall_id: int


# One check of `devbox doctor`: {"check", "status" ("ok"/"warn"/"fail"), "detail", "fix"}.
Check = dict


@runtime_checkable
class Provider(Protocol):
    """Host-level operations. Server dicts are those of `hetzner.server_info`:
    id, name, ip, status, server_type, location, created, project, controller."""

    name: ClassVar[str]  # stored in servers.provider
    display_name: ClassVar[str]  # for text that is genuinely about the provider
    capabilities: ClassVar[Capabilities]

    controller_id: str
    deadline: object | None  # set by `up`: provider calls end by the startup deadline
    stage: str

    @classmethod
    def from_config(cls, cfg) -> "Provider": ...

    @property
    def resource_name(self) -> str: ...

    def version(self) -> str | None: ...

    # Servers
    def list_servers(self) -> list[dict]: ...
    def list_all_servers(self) -> list[dict]: ...
    def create_server(
        self, name: str, project: str, server_type: str, image: str, location: str,
        user_data: Path, ssh_key_id: int, firewall_id: int,
    ) -> dict: ...
    def delete_server(self, server_id: int) -> None: ...
    def poweron(self, server_id: int) -> None: ...
    def shutdown(self, server_id: int) -> None: ...

    # Snapshots (has_snapshots)
    def list_snapshots(self) -> list[dict]: ...
    def create_snapshot(self, server_id: int, project: str, description: str) -> dict: ...
    def delete_image(self, image_id: int) -> None: ...

    # Offers and pricing (has_offers / has_billing)
    def offers(self) -> list[dict]: ...
    def snapshot_price_per_gb_month(self) -> float | None: ...
    def ipv4_prices(self) -> dict[str, tuple[float | None, float | None]]: ...

    # Access: SSH key and firewall that new servers get
    def ensure_access(self, public_key: str, ssh_cidrs: list[str]) -> Access: ...

    # Housekeeping for `doctor` and `cleanup`
    def check_connection(self, cfg) -> tuple[list[Check], bool]:
        """Checks of the tool, credentials and API. False as second value stops `doctor` early."""
        ...

    def check_access(self, public_key: str | None, ssh_cidrs: Callable[[], list[str]]) -> list[Check]: ...
    def leftovers(self, local_public_key: str | None, notes: list[str]) -> list[dict]:
        """Unused resources devbox created at the provider (not servers or snapshots)."""
        ...

    def delete_resource(self, kind: str, resource_id) -> None: ...


def require(provider_or_caps, capability: str, action: str) -> None:
    """Raise a DevboxError unless the provider has `capability`."""
    from devbox import DevboxError

    caps = getattr(provider_or_caps, "capabilities", provider_or_caps)
    if not getattr(caps, capability):
        name = getattr(provider_or_caps, "display_name", None) or getattr(provider_or_caps, "name", "this provider")
        raise DevboxError(f"{name} does not support {action} ({capability.replace('_', ' ')} is off)")
