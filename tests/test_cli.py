import json
from datetime import datetime, timedelta, timezone

import pytest

from devbox import DevboxError, cli
from devbox.remote import parse_units, to_script


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "crm").mkdir()
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_project_add_list_notes(capsys, isolated):
    code, _, _ = run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    assert code == 0

    code, out, _ = run(capsys, "project", "list", "--json")
    projects = json.loads(out)
    assert projects == [
        {"name": "crm", "path": str((isolated / "crm").resolve()), "has_notes": False, "server": None}
    ]

    run(capsys, "project", "notes", "crm", "--set", "npm run dev, port 3000")
    code, out, _ = run(capsys, "project", "notes", "crm", "--json")
    assert json.loads(out)["notes"] == "npm run dev, port 3000\n"


def test_unknown_project_is_an_error(capsys):
    code, out, _ = run(capsys, "project", "notes", "nope", "--json")
    assert code == 1
    assert "unknown project" in json.loads(out)["error"]


def test_add_missing_path(capsys, isolated):
    code, _, err = run(capsys, "project", "add", "x", "--path", str(isolated / "missing"))
    assert code == 1
    assert "not a directory" in err


def test_exec_without_server_never_runs_locally(capsys, isolated, monkeypatch):
    import subprocess

    def boom(*a, **kw):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, _, err = run(capsys, "exec", "crm", "--", "touch", str(isolated / "pwned"))
    assert code == 1
    assert "has no server" in err
    assert not (isolated / "pwned").exists()


def test_exec_splits_at_double_dash(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_exec", lambda ctx: seen.update(vars(ctx.args)) or 0)
    parser_main = cli.main(["exec", "crm", "--detach", "--name", "web", "--", "npm", "run", "dev", "--", "--host"])
    assert parser_main == 0
    assert seen["detach"] and seen["proc"] == "web"
    assert seen["cmd"] == ["npm", "run", "dev", "--", "--host"]


def test_to_script():
    assert to_script(["npm ci && npm run build"]) == "npm ci && npm run build"
    assert to_script(["echo", "a b", "$HOME"]) == "echo 'a b' '$HOME'"
    with pytest.raises(DevboxError):
        to_script([])


def test_parse_units():
    out = (
        "devbox-web.service loaded active running /bin/bash -lc npm run dev\n"
        "devbox-tunnel.service loaded failed failed cloudflared\n"
    )
    assert parse_units(out) == [
        {"name": "web", "active": "active", "sub": "running"},
        {"name": "tunnel", "active": "failed", "sub": "failed"},
    ]


class FakeHcloud:
    def __init__(self, offers):
        self._offers = offers

    def offers(self):
        return self._offers


def offer(type, location, available=True, price=5.0, cores=2, memory=4.0):
    return {
        "type": type, "location": location, "city": "X", "country": "DE",
        "network_zone": "eu-central", "cores": cores, "memory_gb": memory, "disk_gb": 40,
        "architecture": "x86", "cpu_type": "shared", "category": "cost_optimized",
        "price_hourly": 0.01, "price_monthly": price, "available": available, "recommended": False,
    }


def make_ctx(offers, type=None, location=None):
    import argparse

    ctx = cli.Context(argparse.Namespace(type=type, location=location, json=False))
    ctx._hcloud = FakeHcloud(offers)
    return ctx


def test_choose_offer_requires_a_choice():
    with pytest.raises(DevboxError, match="no server chosen"):
        cli.choose_offer(make_ctx([offer("cx23", "fsn1")]), "crm")


def test_choose_offer_returns_available_offer():
    o = cli.choose_offer(make_ctx([offer("cx23", "fsn1")], "cx23", "fsn1"), "crm")
    assert o["type"] == "cx23"


def test_choose_offer_suggests_alternatives_when_sold_out():
    offers = [
        offer("cx23", "fsn1", available=False, price=4),
        offer("cx23", "nbg1", price=4),
        offer("cpx12", "fsn1", price=3, cores=1, memory=2),
    ]
    with pytest.raises(DevboxError) as e:
        cli.choose_offer(make_ctx(offers, "cx23", "fsn1"), "crm")
    msg = str(e.value)
    assert "not available in fsn1" in msg
    assert "cx23 in nbg1" in msg
    assert "cpx12" not in msg


def test_choose_offer_unknown_type():
    with pytest.raises(DevboxError, match="unknown server type"):
        cli.choose_offer(make_ctx([offer("cx23", "fsn1")], "nope", "fsn1"), "crm")


class FakeCloud:
    """In-memory stand-in for Hcloud in end-to-end CLI tests."""

    def __init__(self, token=None, controller_id="test"):
        self.servers: dict[int, dict] = {}
        self.images: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.next_id = 100
        self.fail_snapshot = False
        self.offer_list = [
            offer("cx23", "fsn1", price=4.0),
            offer("cx33", "fsn1", price=7.0, cores=4, memory=8.0),
        ]

    def __call__(self, token, controller_id):
        return self

    def _id(self):
        self.next_id += 1
        return self.next_id

    def list_servers(self):
        return [dict(s) for s in self.servers.values()]

    def list_snapshots(self):
        return [dict(i) for i in self.images.values()]

    def offers(self):
        return [dict(o) for o in self.offer_list]

    def ensure_ssh_key(self, public_key):
        return 1

    def ensure_firewall(self, cidrs):
        return 2

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
        del self.servers[server_id]

    def shutdown(self, server_id):
        self.calls.append(("shutdown", server_id))

    def poweron(self, server_id):
        self.calls.append(("poweron", server_id))

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


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(cli, "Hcloud", fake)
    monkeypatch.setattr(cli, "ensure_local_ssh_key", lambda key: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(cli, "resolve_ssh_allow", lambda entries: ["192.0.2.1/32"])
    monkeypatch.setattr(cli.Remote, "wait_ready", lambda self: [])
    monkeypatch.setattr(
        cli.Remote, "sync", lambda self, path, manifest: {"mode": "git", "files": 1, "deleted": 0, "warning": None}
    )
    monkeypatch.setattr(cli.Remote, "list_units", lambda self: [])
    return fake


def test_down_prints_cost_and_activity_and_costs_reports_it(capsys, isolated, cloud):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    cloud.servers[77] = {
        "id": 77, "name": "devbox-crm", "status": "running", "ip": "192.0.2.7",
        "server_type": "cx23", "location": "fsn1", "project": "crm", "created": None,
    }
    conn = db.connect(config.load().db_path)
    sid = db.insert_server(
        conn, "crm", 77, "running", ip="192.0.2.7", server_type="cx23", location="fsn1",
        created_at="2026-10-31T08:00:00+00:00", price_hourly=0.01, price_monthly=5.0,
    )
    for kind in ["sync", "sync", "exec", "exec_detach", "expose"]:
        db.add_event(conn, "crm", sid, kind)

    code, out, _ = run(capsys, "down", "crm")
    assert code == 0
    assert ("delete", 77) in cloud.calls
    assert "deleted server 77 for crm" in out
    assert "cx23 in fsn1" in out
    assert "2 syncs, 1 exec, 1 background start, 1 expose" in out
    assert "project total:" in out

    code, out, _ = run(capsys, "costs", "--json")
    data = json.loads(out)
    assert data["servers"] == 1
    assert data["groups"][0]["key"] == "crm"
    assert data["total"] > 0

    code, out, _ = run(capsys, "costs", "--by", "month", "--project", "crm")
    assert code == 0 and "2026-10" in out


def test_status_shows_cost_so_far(capsys, isolated, cloud):
    cloud.servers[78] = {
        "id": 78, "name": "devbox-crm", "status": "running", "ip": "192.0.2.8",
        "server_type": "cx23", "location": "fsn1", "project": "crm", "created": "2026-10-31T08:00:00+00:00",
    }
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    # Adopted by reconcile, price filled in from offers.
    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    entry = data["projects"][0]
    assert entry["server"]["price_hourly"] == 0.01
    assert entry["usage"]["cost"] > 0
    assert entry["idle"]["idle_timeout"] == "8h"
    assert data["month"]["servers"] == 1


def test_pause_then_up_restores_snapshot_with_last_server_choice(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, out, err = run(capsys, "up", "crm", "--type", "cx33", "--location", "fsn1")
    assert code == 0, err
    assert "created server" in out

    code, out, err = run(capsys, "pause", "crm")
    assert code == 0, err
    assert "paused server" in out
    assert "snapshot" in out and "GB" in out
    assert len(cloud.images) == 1
    image_id = next(iter(cloud.images))
    kinds = [c[0] for c in cloud.calls]
    assert kinds[-3:] == ["shutdown", "snapshot", "delete"]

    code, out, _ = run(capsys, "status", "crm")
    assert f"paused, snapshot {image_id}" in out

    code, out, err = run(capsys, "up", "crm", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["restored_from_snapshot"]["hetzner_image_id"] == image_id
    assert data["offer"]["choice"] == "last used"
    create = [c for c in cloud.calls if c[0] == "create"][-1]
    assert create == ("create", "cx33", "fsn1", str(image_id))


def test_new_pause_replaces_old_snapshot(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    run(capsys, "pause", "crm")
    first = next(iter(cloud.images))
    run(capsys, "up", "crm")
    code, out, _ = run(capsys, "pause", "crm")
    assert f"deleted older snapshot {first}" in out
    assert list(cloud.images) != [first] and len(cloud.images) == 1


def test_failed_snapshot_keeps_server(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.fail_snapshot = True
    code, _, err = run(capsys, "pause", "crm")
    assert code == 1
    assert "snapshot failed" in err and "powered on again" in err
    assert len(cloud.servers) == 1
    assert not any(c[0] == "delete" for c in cloud.calls)


def test_up_fresh_ignores_snapshot_and_resume_requires_one(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, _, err = run(capsys, "resume", "crm")
    assert code == 1 and "no snapshot" in err

    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "up", "crm", "--fresh", "--json")
    assert json.loads(out)["restored_from_snapshot"] is None
    assert [c for c in cloud.calls if c[0] == "create"][-1][3] == "ubuntu-24.04"


def test_snapshot_must_fit_the_server(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    run(capsys, "pause", "crm")
    cloud.offer_list.append({**offer("cax11", "fsn1", price=3.0), "architecture": "arm"})
    code, _, err = run(capsys, "up", "crm", "--type", "cax11", "--location", "fsn1")
    assert code == 1
    assert "cannot hold the snapshot" in err
    assert "cx23 in fsn1" in err


def test_idle_check_pauses_idle_servers(capsys, isolated, cloud):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    conn = db.connect(config.load().db_path)
    conn.execute("UPDATE servers SET created_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE events SET at = '2026-01-01T00:00:00+00:00'")

    code, out, _ = run(capsys, "idle-check", "--dry-run")
    assert "would pause" in out
    assert len(cloud.servers) == 1

    run(capsys, "project", "set", "crm", "--idle-timeout", "off")
    code, out, _ = run(capsys, "idle-check")
    assert "idle shutdown off" in out and len(cloud.servers) == 1

    run(capsys, "project", "set", "crm", "--idle-timeout", "default")
    code, out, _ = run(capsys, "idle-check")
    assert code == 0
    assert "paused" in out
    assert cloud.servers == {} and len(cloud.images) == 1


def test_touch_resets_idle_timer(capsys, isolated, cloud):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    conn = db.connect(config.load().db_path)
    conn.execute("UPDATE servers SET created_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE events SET at = '2026-01-01T00:00:00+00:00'")
    run(capsys, "touch", "crm")
    code, out, _ = run(capsys, "idle-check")
    assert len(cloud.servers) == 1 and "left of 8h" in out


def test_budget_warns_or_blocks(capsys, isolated, cloud):
    from devbox import config, db

    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('monthly_budget = 1.0\nbudget_action = "block"\n')
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    conn = db.connect(config.load().db_path)
    now = datetime.now(timezone.utc)
    start = max(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now - timedelta(hours=3))
    sid = db.insert_server(
        conn, "crm", 5, "running", server_type="cx23", location="fsn1",
        created_at=start.isoformat(), price_hourly=10.0, price_monthly=100.0,
    )
    db.mark_deleted(conn, sid)

    code, _, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert code == 1 and "budget" in err and cloud.servers == {}

    code, out, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--over-budget")
    assert code == 0 and "budget" in out

    code, out, _ = run(capsys, "status")
    assert "of €1.00 budget" in out


def test_costs_include_snapshots(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "costs", "--json")
    data = json.loads(out)
    assert data["snapshots"] == 1
    code, out, _ = run(capsys, "snapshot", "list")
    assert "crm" in out
    image_id = next(iter(cloud.images))
    code, out, _ = run(capsys, "project", "remove", "crm")
    assert code == 1
    code, out, _ = run(capsys, "snapshot", "delete", str(image_id))
    assert code == 0 and cloud.images == {}
    code, _, _ = run(capsys, "project", "remove", "crm")
    assert code == 0
