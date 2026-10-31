import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from devbox import DevboxError


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "devbox"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "devbox"


DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta | None:
    """'30m', '4h', '2d' -> timedelta; 'off' -> None."""
    value = value.strip().lower()
    if value in ("off", "never", "none"):
        return None
    match = DURATION_RE.match(value)
    if not match:
        raise DevboxError(f"invalid duration '{value}': use e.g. 30m, 4h, 2d or off")
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit]: float(amount)})


@dataclass
class Config:
    hcloud_token_file: Path | None = None
    # Optional defaults for `devbox up`. When unset, the project's last choice
    # is used, or the agent picks from `devbox server-types`.
    server_type: str | None = None
    location: str | None = None
    image: str = "ubuntu-24.04"
    ssh_key: Path = field(default_factory=lambda: Path.home() / ".ssh" / "devbox")
    # CIDRs allowed to reach SSH on devboxes. "auto" means this host's public IPv4.
    ssh_allow: list[str] = field(default_factory=lambda: ["auto"])
    # Identifies this devbox install in Hetzner labels. Generated on first use.
    controller_id: str | None = None
    # Snapshots kept per project; older ones are deleted after a new one is taken.
    keep_snapshots: int = 1
    # Used when Hetzner's pricing API cannot be read.
    snapshot_price_per_gb_month: float | None = None
    # `devbox idle-check` pauses (or deletes) servers without activity for this long.
    idle_timeout: str = "8h"
    idle_action: str = "pause"
    # Monthly spending limit in EUR, and what `up` does when it is reached.
    monthly_budget: float | None = None
    budget_action: str = "warn"
    state_dir: Path = field(default_factory=state_dir)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "devbox.db"

    def get_controller_id(self) -> str:
        if self.controller_id:
            return self.controller_id
        path = self.state_dir / "controller-id"
        if path.exists():
            value = path.read_text().strip()
        else:
            value = secrets.token_hex(4)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        if not CONTROLLER_RE.match(value):
            raise DevboxError(f"invalid controller id '{value}' in {path}")
        self.controller_id = value
        return value

    def hcloud_token(self) -> str | None:
        if self.hcloud_token_file is None:
            return os.environ.get("HCLOUD_TOKEN")
        try:
            return self.hcloud_token_file.read_text().strip()
        except OSError as e:
            raise DevboxError(f"cannot read hcloud token file {self.hcloud_token_file}: {e}")


_PATH_KEYS = {"hcloud_token_file", "ssh_key"}
_STR_KEYS = {"server_type", "location", "image", "controller_id", "idle_timeout", "idle_action", "budget_action"}
_FLOAT_KEYS = {"snapshot_price_per_gb_month", "monthly_budget"}
CONTROLLER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    cfg = Config()
    if not path.exists():
        return cfg
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise DevboxError(f"{path}: {e}")

    for key, value in data.items():
        if key in _PATH_KEYS:
            setattr(cfg, key, Path(os.path.expanduser(str(value))))
        elif key in _STR_KEYS:
            setattr(cfg, key, str(value))
        elif key in _FLOAT_KEYS:
            if not isinstance(value, (int, float)):
                raise DevboxError(f"{path}: {key} must be a number")
            setattr(cfg, key, float(value))
        elif key == "keep_snapshots":
            if not isinstance(value, int) or value < 1:
                raise DevboxError(f"{path}: keep_snapshots must be a whole number of at least 1")
            cfg.keep_snapshots = value
        elif key == "ssh_allow":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise DevboxError(f"{path}: ssh_allow must be a list of strings")
            cfg.ssh_allow = value
        else:
            raise DevboxError(f"{path}: unknown key '{key}'")

    if cfg.controller_id and not CONTROLLER_RE.match(cfg.controller_id):
        raise DevboxError(f"{path}: controller_id must be lowercase letters, digits and dashes")
    try:
        parse_duration(cfg.idle_timeout)
    except DevboxError as e:
        raise DevboxError(f"{path}: idle_timeout: {e}")
    if cfg.idle_action not in ("pause", "down"):
        raise DevboxError(f"{path}: idle_action must be 'pause' or 'down'")
    if cfg.budget_action not in ("warn", "block"):
        raise DevboxError(f"{path}: budget_action must be 'warn' or 'block'")
    return cfg
