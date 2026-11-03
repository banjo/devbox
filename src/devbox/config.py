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


HOUR_END = "hour-end"


def parse_idle_timeout(value: str) -> timedelta | str | None:
    """A duration, None for off, or HOUR_END (stop before the next billing hour)."""
    if value.strip().lower() == HOUR_END:
        return HOUR_END
    try:
        return parse_duration(value)
    except DevboxError:
        raise DevboxError(f"invalid idle timeout '{value}': use e.g. 30m, 4h, 2d, {HOUR_END} or off")


@dataclass
class Config:
    hcloud_token_file: Path | None = None
    # Optional defaults for `devbox up`. When unset, the project's last choice
    # is used, or the agent picks from `devbox server-types`.
    server_type: str | None = None
    location: str | None = None
    # Hetzner's Docker CE app: Ubuntu 24.04 with Docker and Compose, so cloud-init skips installing them.
    image: str = "docker-ce"
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
    # With idle_timeout = "hour-end": how long a server must be idle before it
    # is stopped, and the safety margin before the next billing hour.
    min_idle: str = "5m"
    billing_margin: str = "5m"
    # Deadline for `up`: creating the server, first-boot setup and the first sync.
    startup_timeout: str = "5m"
    # Monthly spending limit in EUR, and what `up` does when it is reached.
    monthly_budget: float | None = None
    budget_action: str = "warn"
    # Stable preview URLs: a named Cloudflare tunnel per project on this domain
    # (a zone in the Cloudflare account of the token). Unset: random quick-tunnel URLs.
    preview_domain: str | None = None
    cloudflare_token_file: Path | None = None
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

    def cloudflare_token(self) -> str:
        if self.cloudflare_token_file is None:
            token = os.environ.get("CLOUDFLARE_API_TOKEN")
            if not token:
                raise DevboxError("preview_domain needs a Cloudflare API token: set cloudflare_token_file "
                                  "in config or CLOUDFLARE_API_TOKEN")
            return token
        try:
            return self.cloudflare_token_file.read_text().strip()
        except OSError as e:
            raise DevboxError(f"cannot read Cloudflare token file {self.cloudflare_token_file}: {e}")

    def hcloud_token(self) -> str | None:
        if self.hcloud_token_file is None:
            return os.environ.get("HCLOUD_TOKEN")
        try:
            return self.hcloud_token_file.read_text().strip()
        except OSError as e:
            raise DevboxError(f"cannot read hcloud token file {self.hcloud_token_file}: {e}")


_PATH_KEYS = {"hcloud_token_file", "ssh_key", "cloudflare_token_file"}
_STR_KEYS = {
    "server_type", "location", "image", "controller_id", "idle_timeout", "idle_action", "budget_action",
    "startup_timeout", "min_idle", "billing_margin", "preview_domain",
}
_FLOAT_KEYS = {"snapshot_price_per_gb_month", "monthly_budget"}
CONTROLLER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


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
        parse_idle_timeout(cfg.idle_timeout)
    except DevboxError as e:
        raise DevboxError(f"{path}: idle_timeout: {e}")
    for key in ("min_idle", "billing_margin"):
        try:
            if parse_duration(getattr(cfg, key)) is None:
                raise DevboxError("can't be off; use 0m")
        except DevboxError as e:
            raise DevboxError(f"{path}: {key}: {e}")
    try:
        if parse_duration(cfg.startup_timeout) is None:
            raise DevboxError("startup_timeout can't be off")
    except DevboxError as e:
        raise DevboxError(f"{path}: startup_timeout: {e}")
    if cfg.idle_action not in ("pause", "down"):
        raise DevboxError(f"{path}: idle_action must be 'pause' or 'down'")
    if cfg.budget_action not in ("warn", "block"):
        raise DevboxError(f"{path}: budget_action must be 'warn' or 'block'")
    if cfg.preview_domain is not None:
        cfg.preview_domain = cfg.preview_domain.strip().lower().strip(".")
        if not DOMAIN_RE.match(cfg.preview_domain):
            raise DevboxError(f"{path}: preview_domain must be a domain name like dev.example.com")
    return cfg
