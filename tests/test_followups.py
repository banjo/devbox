"""Tests for the follow-ups: pinned SSH host keys, Cloudflare cleanup and doctor check, reuse savings."""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devbox import admin, cli, config, db, previews
from devbox.remote import Remote
from devbox.startup import Deadline, Progress, StageTimeout

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_bugfixes_24_29 import add_project, running_server
from test_previews_6 import FakeCloudflare, FakeServer, box, cf, tunnel_name  # noqa: F401  (fixtures)

UTC = timezone.utc


def conn():
    return db.connect(config.load().db_path)


# Pinned host keys


def test_new_servers_get_a_host_key_devbox_knows(capsys, isolated, cloud, monkeypatch):
    seen = {}
    create = cloud.create_server

    def capture(name, project, server_type, image, location, user_data, **kw):
        seen["user_data"] = Path(user_data).read_text()
        return create(name, project, server_type, image, location, user_data, **kw)

    monkeypatch.setattr(cloud, "create_server", capture)
    add_project(capsys, isolated)
    code, _, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert code == 0, err
    server = db.active_server(conn(), "crm")
    assert server["host_key"].startswith("ssh-ed25519 ")
    assert "ssh_keys:\n  ed25519_private: |\n    -----BEGIN OPENSSH PRIVATE KEY-----" in seen["user_data"]
    assert f"  ed25519_public: {server['host_key']}\n" in seen["user_data"]

    ctx = cli.Context(cli.build_parser().parse_args(["status"]))
    remote = ctx.remote(server)
    assert remote.pinned
    assert remote.known_hosts.read_text() == f"{server['ip']} {server['host_key']}\n"
    opts = " ".join(remote.ssh_opts())
    assert "StrictHostKeyChecking=yes" in opts and "HostKeyAlgorithms=ssh-ed25519" in opts
    assert "accept-new" not in opts


def test_servers_from_older_versions_keep_trust_on_first_use(capsys, isolated, cloud):
    add_project(capsys, isolated)
    running_server(cloud)
    run(capsys, "status")
    server = db.active_server(conn(), "crm")
    assert server["host_key"] is None
    remote = cli.Context(cli.build_parser().parse_args(["status"])).remote(server)
    assert not remote.pinned and "StrictHostKeyChecking=accept-new" in remote.ssh_opts()


def test_wait_ssh_never_accepts_another_host_key(monkeypatch, tmp_path):
    r = Remote("192.0.2.1", tmp_path / "key", tmp_path / "kh", pinned=True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) < 3:
            return subprocess.CompletedProcess(cmd, 255, "", "Host key verification failed.\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("devbox.remote.subprocess.run", fake_run)
    messages = []
    progress = Progress(Deadline(60))
    monkeypatch.setattr(progress, "waiting", messages.append)
    r.wait_ssh(Deadline(60), progress, sleep=lambda s: None)
    assert len(calls) == 3 and "doesn't present devbox's host key yet" in messages[0]


def test_generated_host_key_is_valid_cloud_config():
    private, public = cli.generate_host_key()
    text = cli.with_host_key("#cloud-config\npackages:\n  - curl\n", private, public)
    assert text.startswith("#cloud-config\n")
    assert public.count(" ") == 1 and "devbox-host" not in public
    body = text.split("ed25519_private: |\n", 1)[1].split("  ed25519_public:")[0]
    assert [line[4:] for line in body.splitlines()] == private.strip().splitlines()


# Cloudflare: cleanup and doctor


class CleanupCloudflare(FakeCloudflare):
    def __init__(self):
        super().__init__()
        self.deleted: list[str] = []
        self.token_status = "active"

    def _request(self, method, path, body=None, query=None):
        if path == "/user/tokens/verify":
            return {"status": self.token_status}
        if method == "GET" and path == "/accounts/acc1/cfd_tunnel" and "include_prefix" in query:
            return [{"id": tid, "name": name} for name, tid in self.tunnels.items()
                    if name.startswith(query["include_prefix"])]
        if method == "GET" and path == "/zones/zone1/dns_records" and "content" in query:
            return [{"id": f"rec-{name}", "name": name, **r} for name, r in self.dns.items()
                    if r["content"] == query["content"]]
        if method == "DELETE":
            self.deleted.append(path)
            return {}
        return super()._request(method, path, body, query)


@pytest.fixture
def cf2(monkeypatch, cf):
    fake = CleanupCloudflare()
    monkeypatch.setattr(previews.Cloudflare, "_request", lambda self, *a, **kw: fake._request(*a, **kw))
    return fake


def test_cleanup_removes_the_preview_tunnel_of_a_removed_project(capsys, isolated, cloud, box, cf2):
    run(capsys, "expose", "crm", "3000")
    tid = cf2.tunnels[tunnel_name()]
    cf2.tunnels["someone-elses-tunnel"] = "tid-x"
    code, out, _ = run(capsys, "cleanup", "--json")
    assert json.loads(out)["items"] == []  # the project still exists

    run(capsys, "down", "crm")
    code, out, _ = run(capsys, "project", "remove", "crm")
    assert "`devbox cleanup` lists its Cloudflare preview tunnel" in out
    code, out, _ = run(capsys, "cleanup")
    assert "dns record crm.example.com" in out and f"cloudflare tunnel {tunnel_name()}" in out
    assert "someone-elses" not in out
    code, out, _ = run(capsys, "cleanup", "--yes")
    assert code == 0
    assert cf2.deleted == [
        "/zones/zone1/dns_records/rec-crm.example.com",
        f"/accounts/acc1/cfd_tunnel/{tid}/connections",
        f"/accounts/acc1/cfd_tunnel/{tid}",
    ]


def test_cleanup_notes_when_cloudflare_cant_be_checked(capsys, isolated, cloud, cf2):
    cf2.zones.clear()
    code, out, _ = run(capsys, "cleanup")
    assert "Cloudflare preview tunnels not checked: no Cloudflare zone" in out


def doctor_check(capsys):
    code, out, _ = run(capsys, "doctor", "--json")
    return next(c for c in json.loads(out)["checks"] if c["check"] == "cloudflare")


def test_doctor_checks_cloudflare(capsys, isolated, cloud, cf2):
    check = doctor_check(capsys)
    assert check["status"] == "ok" and "zone example.com" in check["detail"]

    cf2.token_status = "disabled"
    check = doctor_check(capsys)
    assert check["status"] == "fail" and "disabled" in check["detail"] and "Cloudflare Tunnel > Edit" in check["fix"]

    cf2.token_status = "active"
    (isolated / "config" / "devbox" / "config.toml").write_text(
        f'preview_domain = "dev.example.com"\ncloudflare_token_file = "{isolated / "cf-token"}"\n'
    )
    check = doctor_check(capsys)
    assert check["status"] == "warn" and "Advanced Certificate" in check["fix"]


def test_doctor_skips_cloudflare_without_preview_domain(capsys, isolated, cloud):
    code, out, _ = run(capsys, "doctor", "--json")
    assert all(c["check"] != "cloudflare" for c in json.loads(out)["checks"])


# Reuse of a paid hour


def backdate(minutes):
    """Move every event and the server's creation `minutes` into the past."""
    c = conn()
    for r in c.execute("SELECT rowid AS rid, at FROM events").fetchall():
        at = datetime.fromisoformat(r["at"]) - timedelta(minutes=minutes)
        c.execute("UPDATE events SET at = ? WHERE rowid = ?", (at.isoformat(timespec="seconds"), r["rid"]))
    s = db.active_server(c, "crm")
    created = datetime.fromisoformat(s["created_at"]) - timedelta(minutes=minutes)
    c.execute("UPDATE servers SET created_at = ? WHERE id = ?", (created.isoformat(timespec="seconds"), s["id"]))


def reuse_count():
    return conn().execute("SELECT COUNT(*) FROM events WHERE kind = 'reuse'").fetchone()[0]


def test_hour_end_reuse_after_idle_is_counted(capsys, isolated, cloud, box):
    run(capsys, "project", "set", "crm", "--idle-timeout", "hour-end")
    run(capsys, "touch", "crm")
    assert reuse_count() == 0  # first use of the server
    run(capsys, "touch", "crm")
    assert reuse_count() == 0  # not idle for min_idle (5m) in between
    backdate(10)
    run(capsys, "expose", "crm", "3000")
    assert reuse_count() == 1
    # Looking at the server isn't a reuse.
    backdate(10)
    run(capsys, "ps", "crm")
    assert reuse_count() == 1


def test_other_idle_timeouts_dont_count(capsys, isolated, cloud, box):
    run(capsys, "touch", "crm")
    backdate(10)
    run(capsys, "touch", "crm")
    assert reuse_count() == 0


def test_cancelled_hour_end_stop_is_a_reuse_and_costs_shows_the_saving(capsys, isolated, cloud, box):
    run(capsys, "pause", "crm", "--at-hour-end")
    run(capsys, "touch", "crm")
    assert reuse_count() == 1
    c = conn()
    c.execute("UPDATE servers SET price_hourly = 0.0123")
    code, out, _ = run(capsys, "costs")
    assert "reused a paid hour 1 time instead of starting a new server" in out and "~€0.01 saved" in out
    code, out, _ = run(capsys, "costs", "--json")
    assert json.loads(out)["reuse"] == {"count": 1, "saved": pytest.approx(0.0123)}
    code, out, _ = run(capsys, "costs", "--since", "2000-01-01", "--until", "2001-01-01", "--json")
    assert json.loads(out)["reuse"]["count"] == 0
