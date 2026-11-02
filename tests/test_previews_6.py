"""Tests for issue #6: stable, protected, multiple previews with health checks."""

import json
import re
import subprocess

import pytest

from devbox import DevboxError, cli, config, db, previews
from devbox.remote import Remote

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_bugfixes_24_29 import add_project, running_server


class FakeServer:
    """Background processes and HTTP answers on the server."""

    def __init__(self, monkeypatch):
        self.units: dict[str, str] = {}
        self.http: dict[tuple[int, str], str] = {}
        self.files: dict[str, str] = {}
        self.next_url = 0
        self.next_port = 40000
        server = self

        def start_tunnel(r, port, unit="tunnel", timeout=60):
            server.next_url += 1
            server.units[unit] = f"quick -> {port}"
            return f"https://random-{server.next_url}.trycloudflare.com"

        def start_named_tunnel(r, unit, token, timeout=60):
            server.units.setdefault(unit, f"named {token}")

        def start_auth_proxy(r, unit, port, password, timeout=120):
            server.next_port += 1
            server.units[unit] = f"auth {server.next_port} -> {port} ({password})"
            return server.next_port

        def stop_unit(r, name):
            return server.units.pop(name, None) is not None

        def wait_http(r, port, path, timeout, accept="2|3"):
            code = server.http.get((port, path), "none")
            return code[0] in accept.split("|"), code

        monkeypatch.setattr(Remote, "start_tunnel", start_tunnel)
        monkeypatch.setattr(Remote, "start_named_tunnel", start_named_tunnel)
        monkeypatch.setattr(Remote, "start_auth_proxy", start_auth_proxy)
        monkeypatch.setattr(Remote, "stop_unit", stop_unit)
        monkeypatch.setattr(Remote, "wait_http", wait_http)
        monkeypatch.setattr(Remote, "list_units", lambda r: [
            {"name": n, "active": "active", "sub": "running", "keep_alive": False} for n in server.units
        ])


class FakeCloudflare:
    def __init__(self):
        self.zones = {"example.com": {"id": "zone1", "account": {"id": "acc1"}}}
        self.tunnels: dict[str, str] = {}
        self.ingress: dict[str, list] = {}
        self.dns: dict[str, dict] = {}
        self.tokens_used: list[str] = []

    def __call__(self, token):
        self.tokens_used.append(token)
        return self

    def _request(self, method, path, body=None, query=None):
        if path == "/zones":
            z = self.zones.get(query["name"])
            return [{"id": z["id"], "account": z["account"]}] if z else []
        m = re.match(r"^/accounts/acc1/cfd_tunnel(?:/([^/]+))?(?:/(\w+))?$", path)
        if m:
            tid, sub = m.groups()
            if tid is None and method == "GET":
                return [{"id": self.tunnels[query["name"]]}] if query["name"] in self.tunnels else []
            if tid is None and method == "POST":
                self.tunnels[body["name"]] = f"tid-{len(self.tunnels) + 1}"
                return {"id": self.tunnels[body["name"]]}
            if sub == "token":
                return f"token-for-{tid}"
            if sub == "configurations":
                self.ingress[tid] = body["config"]["ingress"]
                return {}
        m = re.match(r"^/zones/zone1/dns_records$", path)
        if m:
            if method == "GET":
                return [self.dns[query["name"]]] if query["name"] in self.dns else []
            self.dns[body["name"]] = body
            return body
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def box(capsys, isolated, cloud, monkeypatch):
    add_project(capsys, isolated)
    running_server(cloud)
    run(capsys, "status")
    return FakeServer(monkeypatch)


@pytest.fixture
def cf(monkeypatch, isolated):
    fake = FakeCloudflare()
    monkeypatch.setattr(previews.Cloudflare, "_request", lambda self, *a, **kw: fake._request(*a, **kw))
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (isolated / "cf-token").write_text("secret-token\n")
    (cfg_dir / "config.toml").write_text(
        f'preview_domain = "Example.com"\ncloudflare_token_file = "{isolated / "cf-token"}"\n'
    )
    return fake


def expose(capsys, *args):
    code, out, err = run(capsys, "expose", "crm", *args, "--json")
    return code, (json.loads(out) if code == 0 else json.loads(out)["error"])


def tunnel_name():
    return f"devbox-{config.load().get_controller_id()}-crm"


def conn():
    return db.connect(config.load().db_path)


# Multiple previews (quick tunnels)


def test_two_previews_side_by_side(capsys, box):
    code, web = expose(capsys, "5173", "--name", "web")
    code, api = expose(capsys, "3000", "--name", "api")
    assert web["url"] != api["url"] and web["kind"] == "quick"
    assert set(box.units) == {"tunnel-web", "tunnel-api"}

    code, out, _ = run(capsys, "status", "crm")
    assert f"preview web: {web['url']} -> port 5173" in out
    assert f"preview api: {api['url']} -> port 3000" in out
    code, out, _ = run(capsys, "status", "crm", "--json")
    assert [p["name"] for p in json.loads(out)["projects"][0]["previews"]] == ["api", "web"]

    code, out, _ = run(capsys, "unexpose", "crm", "web")
    assert code == 0 and "closed web" in out and set(box.units) == {"tunnel-api"}
    code, out, _ = run(capsys, "stop", "crm", "tunnel-api")
    assert box.units == {} and db.previews(conn(), db.active_server(conn(), "crm")["id"]) == []


def test_default_preview_keeps_the_old_unit_and_columns(capsys, box):
    code, data = expose(capsys, "3000")
    assert data["name"] == "default" and "tunnel" in box.units
    server = db.active_server(conn(), "crm")
    assert server["preview_url"] == data["url"] and server["preview_port"] == 3000
    code, out, _ = run(capsys, "status")
    assert f"  preview: {data['url']} -> port 3000" in out
    # Re-exposing replaces it.
    code, again = expose(capsys, "4000")
    assert len(db.previews(conn(), server["id"])) == 1 and again["url"] != data["url"]


def test_status_flags_a_preview_whose_tunnel_is_gone(capsys, box):
    expose(capsys, "3000", "--name", "web")
    box.units.clear()
    code, out, _ = run(capsys, "status")
    assert "not running: `devbox expose crm 3000 --name web` restarts it" in out


def test_preview_process_names_are_reserved(capsys, box):
    code, _, err = run(capsys, "exec", "crm", "--detach", "--name", "tunnel-web", "--", "sleep 1")
    assert code == 1 and "reserved for previews" in err
    code, _, err = run(capsys, "expose", "crm", "3000", "--name", "Web!")
    assert code == 1 and "invalid preview name" in err


# Health checks


def test_wait_for_opens_the_preview_only_when_the_app_answers(capsys, box):
    code, err = expose(capsys, "3000", "--wait-for", "/health", "--wait-timeout", "1m")
    assert code == 1 and "did not answer" in err and "status: none" in err
    assert box.units == {}

    box.http[(3000, "/health")] = "503"
    code, err = expose(capsys, "3000", "--wait-for", "/health")
    assert code == 1 and "status: 503" in err and box.units == {}

    box.http[(3000, "/health")] = "200"
    code, data = expose(capsys, "3000", "--wait-for", "/health")
    assert code == 0 and data["health"] == {"path": "/health", "status": 200}

    code, err = expose(capsys, "3000", "--wait-for", "health")
    assert code == 1 and "like /health" in err


# Password protection


def test_protect_puts_a_password_proxy_in_front(capsys, box, isolated):
    code, data = expose(capsys, "3000", "--protect")
    auth = data["auth"]
    assert data["protected"] and auth["user"] == "devbox" and len(auth["password"]) > 16
    assert box.units["preview-auth-default"].startswith("auth 40001 -> 3000")
    assert box.units["tunnel"] == "quick -> 40001"
    creds = isolated / "state" / "devbox" / "preview-auth" / "crm.json"
    assert creds.stat().st_mode & 0o777 == 0o600
    # The password stays the same for the project.
    code, again = expose(capsys, "3000", "--protect")
    assert again["auth"] == auth

    code, out, _ = run(capsys, "status")
    assert "password in" in out

    # Exposing without --protect removes the proxy.
    code, open_ = expose(capsys, "3000")
    assert not open_["protected"] and "preview-auth-default" not in box.units
    assert box.units["tunnel"] == "quick -> 3000"


def test_unexpose_stops_the_proxy_too(capsys, box):
    expose(capsys, "3000", "--protect", "--name", "web")
    run(capsys, "unexpose", "crm")
    assert box.units == {}


def test_caddyfile():
    text = previews.caddyfile(40001, 3000, "$2a$14$abc")
    assert ":40001 {" in text and "bind 127.0.0.1" in text
    assert "devbox $2a$14$abc" in text and "reverse_proxy localhost:3000" in text
    assert "admin off" in text


# Stable URLs (named tunnel)


def test_named_preview_gets_a_stable_hostname(capsys, box, cf, monkeypatch):
    code, data = expose(capsys, "3000")
    assert code == 0 and data["url"] == "https://crm.example.com" and data["kind"] == "named"
    tid = cf.tunnels[tunnel_name()]
    assert cf.dns["crm.example.com"]["content"] == f"{tid}.cfargotunnel.com" and cf.dns["crm.example.com"]["proxied"]
    assert cf.ingress[tid] == [{"hostname": "crm.example.com", "service": "http://localhost:3000"},
                               {"service": "http_status:404"}]
    assert box.units == {"named-tunnel": f"named token-for-{tid}"}
    assert config.load().cloudflare_token() == "secret-token"

    code, web = expose(capsys, "5173", "--name", "web", "--protect")
    assert web["url"] == "https://crm-web.example.com"
    assert cf.ingress[tid][:2] == [
        {"hostname": "crm.example.com", "service": "http://localhost:3000"},
        {"hostname": "crm-web.example.com", "service": "http://localhost:40001"},
    ]
    # One connector serves every hostname.
    assert set(box.units) == {"named-tunnel", "preview-auth-web"}

    # The next server of the project gets the same tunnel and URLs.
    run(capsys, "unexpose", "crm", "web")
    assert cf.ingress[tid] == [{"hostname": "crm.example.com", "service": "http://localhost:3000"},
                               {"service": "http_status:404"}]
    run(capsys, "unexpose", "crm")
    assert box.units == {} and cf.ingress[tid] == [{"service": "http_status:404"}]
    code, again = expose(capsys, "3000")
    assert again["url"] == data["url"] and cf.tunnels == {tunnel_name(): tid}


def test_named_preview_never_replaces_someone_elses_dns_record(capsys, box, cf):
    cf.dns["crm.example.com"] = {"type": "A", "content": "203.0.113.5"}
    code, err = expose(capsys, "3000")
    assert code == 1 and "already has a DNS record (A 203.0.113.5)" in err
    assert box.units == {}


def test_refused_hostname_creates_no_tunnel_or_proxy(capsys, box, cf):
    # #32: the refusal used to come after the tunnel (and password proxy) were created.
    cf.dns["crm.example.com"] = {"type": "CNAME", "content": "public.r2.dev"}
    code, err = expose(capsys, "3000", "--protect")
    assert code == 1 and "already has a DNS record (CNAME public.r2.dev)" in err
    assert cf.tunnels == {} and box.units == {}
    assert cf.dns["crm.example.com"] == {"type": "CNAME", "content": "public.r2.dev"}


def test_existing_tunnel_reuses_its_own_record(capsys, box, cf):
    cf.tunnels[tunnel_name()] = "tid-old"
    cf.dns["crm.example.com"] = {"type": "CNAME", "content": "tid-old.cfargotunnel.com"}
    code, data = expose(capsys, "3000")
    assert code == 0 and data["url"] == "https://crm.example.com"
    assert cf.tunnels == {tunnel_name(): "tid-old"} and "tid-old" in cf.ingress


def test_record_of_a_deleted_tunnel_is_refused(capsys, box, cf):
    cf.dns["crm.example.com"] = {"type": "CNAME", "content": "tid-gone.cfargotunnel.com"}
    code, err = expose(capsys, "3000")
    assert code == 1 and "tid-gone.cfargotunnel.com" in err and cf.tunnels == {}


def test_quick_overrides_the_domain_and_switching_cleans_up(capsys, box, cf):
    code, named = expose(capsys, "3000")
    code, quick = expose(capsys, "3000", "--quick")
    assert "trycloudflare.com" in quick["url"]
    tid = cf.tunnels[tunnel_name()]
    assert cf.ingress[tid] == [{"service": "http_status:404"}]
    assert set(box.units) == {"tunnel"}


def test_named_preview_needs_a_zone_and_a_token(capsys, box, cf, isolated, monkeypatch):
    cf.zones.clear()
    code, err = expose(capsys, "3000")
    assert code == 1 and "no Cloudflare zone for crm.example.com" in err

    (isolated / "config" / "devbox" / "config.toml").write_text('preview_domain = "example.com"\n')
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    code, err = expose(capsys, "3000")
    assert code == 1 and "needs a Cloudflare API token" in err


def test_deeper_hostnames_warn_about_certificates(capsys, box, cf, isolated):
    (isolated / "config" / "devbox" / "config.toml").write_text(
        f'preview_domain = "dev.example.com"\ncloudflare_token_file = "{isolated / "cf-token"}"\n'
    )
    code, data = expose(capsys, "3000")
    assert data["url"] == "https://crm.dev.example.com"
    assert "Advanced Certificate" in data["warnings"][0]


def test_preview_domain_is_validated(isolated):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('preview_domain = "https://example.com"\n')
    with pytest.raises(DevboxError, match="preview_domain"):
        config.load()


def test_down_forgets_the_previews(capsys, box):
    expose(capsys, "3000", "--name", "web")
    server_id = db.active_server(conn(), "crm")["id"]
    run(capsys, "down", "crm")
    assert db.previews(conn(), server_id) == []


# The shell side of the health check


def test_wait_http_script_reports_the_last_status(monkeypatch):
    r = Remote("192.0.2.1", "/key", "/kh")
    seen = {}

    def fake_run(script, **kw):
        seen["script"] = script
        return subprocess.CompletedProcess([], 1, "fail 000\n", "")

    monkeypatch.setattr(r, "run", fake_run)
    assert r.wait_http(3000, "/health", 0) == (False, "none")
    assert "http://localhost:3000/health" in seen["script"] and "2*|3*)" in seen["script"]
    out = subprocess.run(["bash", "-c", seen["script"].replace("curl", "echo 000; true")],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.startswith("fail")
