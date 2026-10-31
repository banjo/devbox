"""Per-project secrets, kept on this machine and written to the server's env files.

Stored as JSON in ~/.config/devbox/secrets/<project>.json with mode 0600:
{"files": {".env": {"KEY": "value"}, "apps/api/.env": {...}}}

The values are not encrypted; like SSH private keys they are protected by
file permissions. Values are never printed or logged.
"""

import json
import os
import re
from pathlib import Path, PurePosixPath

from devbox import DevboxError

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@+,=-]*$")
DEFAULT_FILE = ".env"


def secrets_dir(config_dir: Path) -> Path:
    return config_dir / "secrets"


def validate_file(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise DevboxError(f"invalid env file '{path}': use a path inside the project, like .env or apps/api/.env")
    return str(p)


def parse_assignment(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    key = key.strip()
    if not sep or not KEY_RE.match(key):
        raise DevboxError(f"invalid secret '{key or text}': use KEY=VALUE with KEY like DATABASE_URL")
    return key, value


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file (comments, `export` and quotes allowed)."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = parse_assignment(line)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def format_value(value: str) -> str:
    if SAFE_VALUE_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def merge_env(existing: str, values: dict[str, str]) -> str:
    """Set `values` in an env file's text, keeping every other line as it is."""
    lines = existing.splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        body = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        key = body.split("=", 1)[0].strip() if "=" in body and not body.startswith("#") else None
        if key in remaining:
            prefix = "export " if stripped.startswith("export ") else ""
            out.append(f"{prefix}{key}={format_value(remaining.pop(key))}")
        else:
            out.append(line)
    out += [f"{k}={format_value(v)}" for k, v in remaining.items()]
    return "\n".join(out) + "\n"


class SecretStore:
    def __init__(self, config_dir: Path, project: str):
        self.path = secrets_dir(config_dir) / f"{project}.json"

    def load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except ValueError as e:
            raise DevboxError(f"{self.path} is not valid JSON: {e}")
        return data.get("files", {})

    def save(self, files: dict[str, dict[str, str]]) -> None:
        files = {f: v for f, v in files.items() if v}
        if not files:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"files": files}, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def set(self, file: str, values: dict[str, str]) -> None:
        files = self.load()
        files.setdefault(validate_file(file), {}).update(values)
        self.save(files)

    def unset(self, file: str, keys: list[str]) -> list[str]:
        files = self.load()
        current = files.get(validate_file(file), {})
        removed = [k for k in keys if current.pop(k, None) is not None]
        self.save(files)
        return removed

    def summary(self) -> dict[str, list[str]]:
        """Env file -> key names. Never values."""
        return {f: sorted(v) for f, v in sorted(self.load().items())}

    def delete_all(self) -> None:
        self.path.unlink(missing_ok=True)
