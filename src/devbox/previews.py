"""Preview URLs: Cloudflare quick tunnels, or a named tunnel on your own domain.

- Quick tunnel (default): a random `*.trycloudflare.com` URL per preview that
  changes whenever the tunnel restarts. No account needed.
- Named tunnel (`preview_domain` in config): one Cloudflare tunnel per project,
  reused by every server of that project, with a fixed hostname per preview
  (`<project>.<domain>`, `<project>-<preview>.<domain>`). devbox creates the
  tunnel and the DNS records through the Cloudflare API.
- `--protect` puts a Caddy reverse proxy with HTTP basic auth (run in Docker)
  between the tunnel and the app.
"""

import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from devbox import DevboxError

DEFAULT = "default"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,20}$")
QUICK_UNIT = "tunnel"
NAMED_UNIT = "named-tunnel"
AUTH_UNIT_PREFIX = "preview-auth-"
CADDY_IMAGE = "caddy:2"
AUTH_USER = "devbox"
API = "https://api.cloudflare.com/client/v4"
TUNNEL_TARGET = ".cfargotunnel.com"


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise DevboxError(f"invalid preview name '{name}': use up to 21 lowercase letters, digits and dashes")


def is_reserved(proc: str) -> bool:
    """Background process names used by previews."""
    return proc == QUICK_UNIT or proc.startswith(QUICK_UNIT + "-") or proc == NAMED_UNIT \
        or proc.startswith(AUTH_UNIT_PREFIX)


def quick_unit(name: str) -> str:
    return QUICK_UNIT if name == DEFAULT else f"{QUICK_UNIT}-{name}"


def auth_unit(name: str) -> str:
    return AUTH_UNIT_PREFIX + name


def hostname(project: str, name: str, domain: str) -> str:
    label = project if name == DEFAULT else f"{project}-{name}"
    if len(label) > 63:
        raise DevboxError(f"hostname label '{label}' is longer than 63 characters; use a shorter preview name")
    return f"{label}.{domain}"


def tunnel_name(controller: str, project: str) -> str:
    return f"devbox-{controller}-{project}"


def caddyfile(listen: int, port: int, password_hash: str) -> str:
    return (
        "{\n\tadmin off\n\tauto_https off\n}\n"
        f":{listen} {{\n\tbind 127.0.0.1\n"
        f"\tbasic_auth {{\n\t\t{AUTH_USER} {password_hash}\n\t}}\n"
        f"\treverse_proxy localhost:{port}\n}}\n"
    )


def ingress(rows) -> list[dict]:
    """Cloudflare tunnel ingress rules for a project's named previews."""
    rules = [{"hostname": r["hostname"], "service": f"http://localhost:{r['proxy_port'] or r['port']}"}
             for r in rows if r["kind"] == "named"]
    return rules + [{"service": "http_status:404"}]


def credentials(state_dir: Path, project: str) -> dict:
    """Basic-auth user and password for a project's protected previews (created on first use)."""
    path = state_dir / "preview-auth" / f"{project}.json"
    try:
        data = json.loads(path.read_text())
        if data.get("user") and data.get("password"):
            return data
    except (OSError, ValueError):
        pass
    data = {"user": AUTH_USER, "password": secrets.token_urlsafe(18)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data) + "\n")
    return data


def forget_credentials(state_dir: Path, project: str) -> None:
    (state_dir / "preview-auth" / f"{project}.json").unlink(missing_ok=True)


class Cloudflare:
    """The few Cloudflare API calls named previews need. Token: Account > Cloudflare Tunnel > Edit,
    Zone > DNS > Edit."""

    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read())
            except ValueError:
                raise DevboxError(f"Cloudflare API {method} {path}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise DevboxError(f"cannot reach the Cloudflare API: {e}")
        if not payload.get("success"):
            errors = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in payload.get("errors") or [])
            raise DevboxError(f"Cloudflare API {method} {path} failed: {errors or 'unknown error'}")
        return payload.get("result")

    def zone(self, hostname: str) -> dict:
        """The zone a hostname belongs to (itself or the longest matching parent domain)."""
        labels = hostname.split(".")
        for i in range(0, len(labels) - 1):
            name = ".".join(labels[i:])
            found = self._request("GET", "/zones", query={"name": name})
            if found:
                return {"id": found[0]["id"], "name": name, "account": found[0]["account"]["id"]}
        raise DevboxError(f"no Cloudflare zone for {hostname} that this token can see")

    def find_tunnel(self, account: str, name: str) -> str | None:
        found = self._request("GET", f"/accounts/{account}/cfd_tunnel", query={"name": name, "is_deleted": "false"})
        return found[0]["id"] if found else None

    def ensure_tunnel(self, account: str, name: str) -> str:
        found = self.find_tunnel(account, name)
        if found:
            return found
        created = self._request("POST", f"/accounts/{account}/cfd_tunnel",
                                {"name": name, "config_src": "cloudflare"})
        return created["id"]

    def tunnels(self, account: str, prefix: str) -> list[dict]:
        """Live tunnels whose name starts with `prefix`."""
        found, page = [], 1
        while True:
            batch = self._request("GET", f"/accounts/{account}/cfd_tunnel", query={
                "is_deleted": "false", "include_prefix": prefix, "per_page": 100, "page": page,
            }) or []
            found += [t for t in batch if t.get("name", "").startswith(prefix)]
            if len(batch) < 100:
                return found
            page += 1

    def delete_tunnel(self, account: str, tunnel_id: str) -> None:
        # Connected replicas would block the delete; drop them first.
        self._request("DELETE", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/connections")
        self._request("DELETE", f"/accounts/{account}/cfd_tunnel/{tunnel_id}")

    def tunnel_records(self, zone: str, tunnel_id: str) -> list[dict]:
        """DNS records in the zone that point at the tunnel."""
        target = tunnel_id + TUNNEL_TARGET
        records = self._request("GET", f"/zones/{zone}/dns_records",
                                query={"type": "CNAME", "content": target, "per_page": 100}) or []
        return [r for r in records if r.get("content") == target]

    def delete_record(self, zone: str, record_id: str) -> None:
        self._request("DELETE", f"/zones/{zone}/dns_records/{record_id}")

    def verify_token(self) -> str:
        return (self._request("GET", "/user/tokens/verify") or {}).get("status", "unknown")

    def tunnel_token(self, account: str, tunnel_id: str) -> str:
        return self._request("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/token")

    def set_ingress(self, account: str, tunnel_id: str, rules: list[dict]) -> None:
        self._request("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations",
                      {"config": {"ingress": rules}})

    def check_hostname(self, zone: str, hostname: str, tunnel_id: str | None) -> bool:
        """Whether `hostname` already points at the tunnel. Raises if it has a record devbox didn't make
        for this tunnel; a tunnel that doesn't exist yet (`tunnel_id` None) owns no records."""
        target = tunnel_id + TUNNEL_TARGET if tunnel_id else None
        records = self._request("GET", f"/zones/{zone}/dns_records", query={"name": hostname})
        for r in records or []:
            if target and r.get("type") == "CNAME" and r.get("content") == target:
                return True
            raise DevboxError(
                f"{hostname} already has a DNS record ({r.get('type')} {r.get('content')}) that devbox "
                f"didn't make for this project; remove it or use another preview name"
            )
        return False

    def route_dns(self, zone: str, hostname: str, tunnel_id: str) -> None:
        """Point `hostname` at the tunnel. Never replaces a record that isn't ours."""
        if self.check_hostname(zone, hostname, tunnel_id):
            return
        target = tunnel_id + TUNNEL_TARGET
        self._request("POST", f"/zones/{zone}/dns_records",
                      {"type": "CNAME", "name": hostname, "content": target, "proxied": True,
                       "comment": "devbox preview"})
