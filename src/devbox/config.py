import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from devbox import DevboxError


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "devbox"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "devbox"


@dataclass
class Config:
    hcloud_token_file: Path | None = None
    # Optional defaults for `devbox up`. When unset, the agent picks from `devbox server-types`.
    server_type: str | None = None
    location: str | None = None
    image: str = "ubuntu-24.04"
    ssh_key: Path = field(default_factory=lambda: Path.home() / ".ssh" / "devbox")
    # CIDRs allowed to reach SSH on devboxes. "auto" means this host's public IPv4.
    ssh_allow: list[str] = field(default_factory=lambda: ["auto"])
    state_dir: Path = field(default_factory=state_dir)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "devbox.db"

    def hcloud_token(self) -> str | None:
        if self.hcloud_token_file is None:
            return None
        try:
            return self.hcloud_token_file.read_text().strip()
        except OSError as e:
            raise DevboxError(f"cannot read hcloud token file {self.hcloud_token_file}: {e}")


_PATH_KEYS = {"hcloud_token_file", "ssh_key"}
_STR_KEYS = {"server_type", "location", "image"}


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
        elif key == "ssh_allow":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise DevboxError(f"{path}: ssh_allow must be a list of strings")
            cfg.ssh_allow = value
        else:
            raise DevboxError(f"{path}: unknown key '{key}'")
    return cfg
