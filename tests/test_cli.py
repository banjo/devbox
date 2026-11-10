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
        {"name": "web", "active": "active", "sub": "running", "keep_alive": False},
        {"name": "tunnel", "active": "failed", "sub": "failed", "keep_alive": False},
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
        self.controller_id = controller_id
        self.servers: dict[int, dict] = {}
        self.foreign: list[dict] = []
        self.images: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.next_id = 100
        self.fail_snapshot = False
        self.fail_delete = False
        self.running_work = []
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

    def list_all_servers(self):
        mine = [{"controller": self.controller_id, **s} for s in self.servers.values()]
        return mine + [dict(s) for s in self.foreign]

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
        if self.fail_delete:
            from devbox import DevboxError
            raise DevboxError("locked")
        del self.servers[server_id]

    def shutdown(self, server_id):
        self.calls.append(("shutdown", server_id))
        self.servers[server_id]["status"] = "off"

    def poweron(self, server_id):
        self.calls.append(("poweron", server_id))
        self.servers[server_id]["status"] = "running"

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

    # Most tests leave IPv4 out of costs; test_costs_ipv4_36 sets prices.
    ipv4: dict = {}

    def ipv4_prices(self):
        return dict(self.ipv4)

    # Firewalls, SSH keys and ownership, as in Hcloud.
    resource_name = "devbox-test"
    firewalls: list
    keys: list

    def owns(self, r):
        return (r.get("labels") or {}) == {"managed-by": "devbox", "devbox-controller": "test"}

    @staticmethod
    def is_legacy(r):
        labels = r.get("labels") or {}
        return labels.get("managed-by") == "devbox" and "devbox-controller" not in labels

    def version(self):
        return "1.70.2"

    def list_firewalls(self):
        return list(getattr(self, "firewalls", []))

    def list_ssh_keys(self):
        return list(getattr(self, "keys", []))

    def find_firewall(self):
        return next((f for f in self.list_firewalls() if f["name"] == self.resource_name), None)

    def find_ssh_key(self, public_key):
        for k in self.list_ssh_keys():
            if k["public_key"] == public_key:
                return {"status": "owned" if self.owns(k) else "legacy", "key": k}
        return {"status": "missing", "key": None}

    def delete_firewall(self, firewall_id):
        self.calls.append(("delete_firewall", firewall_id))
        self.firewalls = [f for f in self.firewalls if f["id"] != firewall_id]

    def delete_ssh_key(self, key_id):
        self.calls.append(("delete_ssh_key", key_id))
        self.keys = [k for k in self.keys if k["id"] != key_id]


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(cli, "Hcloud", fake)
    monkeypatch.setattr(cli, "ensure_local_ssh_key", lambda key: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(cli, "resolve_ssh_allow", lambda entries: ["192.0.2.1/32"])
    monkeypatch.setattr(cli.Remote, "wait_ssh", lambda self, deadline, progress: None)
    monkeypatch.setattr(cli.Remote, "wait_cloud_init", lambda self, deadline, progress, secrets=(): [])
    monkeypatch.setattr(cli.Remote, "verify_tools", lambda self, deadline, progress: None)
    monkeypatch.setattr(cli.Remote, "collect_diagnostics", lambda self, timeout=20: "cloud-init log\n")
    monkeypatch.setattr(cli.Remote, "prepare_snapshot", lambda self, timeout=60: fake.calls.append(("prepare_snapshot", self.ip)))
    monkeypatch.setattr(
        cli.Remote, "sync",
        lambda self, path, manifest, timeout=None: {"mode": "git", "files": 1, "deleted": 0, "warning": None},
    )
    monkeypatch.setattr(cli.Remote, "list_units", lambda self: [])
    # status reads processes and headroom in one call; the processes come from list_units.
    fake.capacity = None
    monkeypatch.setattr(cli.Remote, "overview", lambda self: (self.list_units(), fake.capacity))
    fake.files = {}
    monkeypatch.setattr(cli.Remote, "write_file",
                        lambda self, path, content, mode="600": fake.files.__setitem__(path, content))
    monkeypatch.setattr(cli.Remote, "running_work", lambda self: list(fake.running_work))
    # Never read the real crontab/systemd; tests that care set this themselves.
    monkeypatch.setattr(cli.admin, "_run", lambda cmd: None)
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


@pytest.mark.parametrize("args", [
    ["--since", "2026-02-30"],
    ["--until", "garbage"],
    ["--month", "2026-13"],
    ["--month", "9999-12"],
    ["--days", "0"],
    ["--days", "-1"],
    ["--days", "1000000000"],
    ["--since", "2026-11-01", "--until", "2026-10-01"],
    ["--month", "2026-10", "--until", "2026-09-01"],
])
def test_costs_invalid_periods_give_json_errors(capsys, args):
    code, out, err = run(capsys, "costs", *args, "--json")
    assert code == 1
    assert "Traceback" not in err
    assert "error" in json.loads(out)


OWNED_LABELS = {"managed-by": "devbox", "devbox-controller": "test"}


def test_cleanup_lists_then_deletes_only_unused_devbox_resources(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.servers[500] = {"id": 500, "name": "devbox-ghost", "status": "running", "ip": "192.0.2.50",
                          "server_type": "cx23", "location": "fsn1", "project": "ghost", "created": None}
    cloud.images[600] = {"id": 600, "description": "old", "status": "available", "size_gb": 1.0,
                         "disk_gb": 40, "architecture": "x86", "project": "ghost", "created": None}
    cloud.firewalls = [
        {"id": 1, "name": "devbox-test", "labels": OWNED_LABELS, "applied_to": [{"type": "server"}]},
        {"id": 2, "name": "devbox", "labels": {"managed-by": "devbox"}, "applied_to": []},
        {"id": 3, "name": "devbox", "labels": {"managed-by": "devbox"}, "applied_to": [{"type": "server"}]},
        {"id": 4, "name": "prod", "labels": {}, "applied_to": []},
        {"id": 5, "name": "other", "labels": {"managed-by": "devbox", "devbox-controller": "other"}, "applied_to": []},
    ]
    ssh_dir = isolated / "home-ssh"
    ssh_dir.mkdir()
    (ssh_dir / "devbox.pub").write_text("ssh-ed25519 AAAA local")
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f'ssh_key = "{ssh_dir / "devbox"}"\n')
    cloud.keys = [
        {"id": 10, "name": "devbox", "public_key": "ssh-ed25519 AAAA local", "labels": {"managed-by": "devbox"}},
        {"id": 11, "name": "devbox-old", "public_key": "ssh-ed25519 BBBB", "labels": OWNED_LABELS},
        {"id": 12, "name": "laptop", "public_key": "ssh-ed25519 CCCC", "labels": {}},
    ]
    stale = isolated / "state" / "devbox" / "known_hosts" / "crm-1"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("x")

    code, out, _ = run(capsys, "cleanup", "--json")
    plan = json.loads(out)["items"]
    found = {(i["kind"], str(i["id"])) for i in plan}
    assert found == {
        ("server", "500"), ("snapshot", "600"), ("firewall", "2"), ("ssh-key", "11"), ("local file", str(stale)),
    }
    assert 500 in cloud.servers and stale.exists()  # nothing deleted without --yes

    code, out, _ = run(capsys, "cleanup", "--yes")
    assert code == 0
    assert 500 not in cloud.servers and 600 not in cloud.images
    assert [f["id"] for f in cloud.firewalls] == [1, 3, 4, 5]
    assert [k["id"] for k in cloud.keys] == [10, 12]
    assert not stale.exists()
    assert len(cloud.servers) == 1  # the project's own server stays

    code, out, _ = run(capsys, "cleanup")
    assert "nothing to clean up" in out


def test_history_shows_servers_snapshots_and_totals(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    run(capsys, "pause", "crm")
    run(capsys, "up", "crm")
    code, out, _ = run(capsys, "history", "crm", "--json")
    data = json.loads(out)
    assert len(data["servers"]) == 2
    latest, first = data["servers"]
    assert latest["status"] == "running" and latest["restored_from_snapshot"] == data["snapshots"][0]["hetzner_image_id"]
    assert first["activity"]["pause"] == 1
    assert data["total"]["servers"] == 2 and data["total"]["snapshots"] == 1

    code, out, _ = run(capsys, "history", "crm")
    assert "from snapshot" in out and "paused" in out and "total:" in out
    code, out, _ = run(capsys, "history", "crm", "--limit", "1", "--json")
    assert len(json.loads(out)["servers"]) == 1
    code, _, err = run(capsys, "history", "nope")
    assert code == 1 and "unknown project" in err


def test_doctor_reports_checks_and_fails_on_conflicts(capsys, isolated, cloud, monkeypatch):
    from devbox import admin

    monkeypatch.setattr(admin.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(admin, "schedule_check", lambda ctx: admin.check("idle shutdown", admin.WARN, "not scheduled"))
    cloud.firewalls = [{"id": 1, "name": "devbox-test", "labels": OWNED_LABELS, "applied_to": [],
                        "rules": [{"source_ips": ["198.51.100.1/32"]}]}]
    code, out, _ = run(capsys, "doctor", "--json")
    data = json.loads(out)
    by_name = {c["check"]: c for c in data["checks"]}
    assert by_name["hcloud"]["detail"] == "version 1.70.2"
    assert by_name["hetzner api"]["status"] == "ok"
    assert by_name["firewall"]["status"] == "warn"  # allows another IP than this machine's
    assert by_name["idle shutdown"]["status"] == "warn"
    assert code == 0 and data["ok"]

    def conflict(public_key):
        raise DevboxError("the local devbox SSH key is already registered")

    ssh_dir = isolated / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "devbox").write_text("private")
    (ssh_dir / "devbox.pub").write_text("ssh-ed25519 AAAA")
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f'ssh_key = "{ssh_dir / "devbox"}"\n')
    monkeypatch.setattr(cloud, "find_ssh_key", conflict)
    code, out, _ = run(capsys, "doctor")
    assert code == 1
    assert "[FAIL] hetzner ssh key" in out


def test_doctor_without_hcloud(capsys, isolated, monkeypatch):
    from devbox import admin

    monkeypatch.setattr(admin.shutil, "which", lambda tool: None if tool == "hcloud" else f"/usr/bin/{tool}")
    code, out, _ = run(capsys, "doctor")
    assert code == 1
    assert "[FAIL] hcloud: hcloud CLI not found" in out
    assert "registry" in out


# Issue #14: idle shutdown must not interrupt running work.


def make_idle(isolated):
    from devbox import config, db

    conn = db.connect(config.load().db_path)
    conn.execute("UPDATE servers SET created_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE events SET at = '2026-01-01T00:00:00+00:00'")


@pytest.mark.parametrize("work", [
    [{"unit": "devbox-exec-1a2b.scope", "kind": "command"}],
    [{"unit": "devbox-build.service", "kind": "keep-alive job"}],
])
def test_idle_check_leaves_servers_with_running_work_alone(capsys, isolated, cloud, work):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    make_idle(isolated)
    cloud.running_work = work
    code, out, _ = run(capsys, "idle-check")
    assert code == 0 and "still running; left alone" in out
    assert len(cloud.servers) == 1 and not cloud.images

    cloud.running_work = []
    code, out, _ = run(capsys, "idle-check")
    assert "paused" in out and cloud.servers == {}


def test_idle_check_does_nothing_when_work_cannot_be_checked(capsys, isolated, cloud, monkeypatch):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    make_idle(isolated)

    def unreachable(self):
        raise DevboxError("ssh to 192.0.2.1 failed")

    monkeypatch.setattr(cli.Remote, "running_work", unreachable)
    code, out, _ = run(capsys, "idle-check", "--json")
    entry = json.loads(out)[0]
    assert entry["action"] is None and "could not check" in entry["skipped"]
    assert len(cloud.servers) == 1


def test_keep_alive_only_with_detach(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    code, _, err = run(capsys, "exec", "crm", "--keep-alive", "--", "make")
    assert code == 1 and "--keep-alive only applies to --detach" in err


def test_busy_units_parsing():
    from devbox.remote import busy_units

    out = (
        "devbox-exec-ab12.scope loaded active running bash -lc make\n"
        "devbox-web.service loaded active running devbox web\n"
        "devbox-build.service loaded active running devbox-keepalive build\n"
        "devbox-tunnel.service loaded active running devbox tunnel\n"
    )
    assert busy_units(out) == [
        {"unit": "devbox-exec-ab12.scope", "kind": "command"},
        {"unit": "devbox-build.service", "kind": "keep-alive job"},
    ]


# Issue #15: snapshots are priced even when no server needs a price.


def test_adopted_snapshot_is_priced_and_counts_for_budget(capsys, isolated, cloud):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('monthly_budget = 0.01\nbudget_action = "block"\n')
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cloud.images[700] = {
        # Large enough to pass the budget within seconds of the month starting.
        "id": 700, "description": "old", "status": "available", "size_gb": 100_000.0, "disk_gb": 40,
        "architecture": "x86", "project": "crm", "created": start.isoformat(),
    }
    code, _, err = run(capsys, "up", "crm", "--fresh", "--type", "cx23", "--location", "fsn1")
    assert code == 1 and "budget" in err
    code, out, _ = run(capsys, "snapshot", "list", "--json")
    assert json.loads(out)["snapshots"][0]["price_per_gb_month"] == 0.012


def test_unknown_prices_make_budget_check_incomplete(capsys, isolated, cloud, monkeypatch):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("monthly_budget = 100\n")
    monkeypatch.setattr(cloud, "snapshot_price_per_gb_month", lambda: None)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    cloud.images[701] = {
        "id": 701, "description": "old", "status": "available", "size_gb": 20.0, "disk_gb": 40,
        "architecture": "x86", "project": "crm", "created": datetime.now(timezone.utc).isoformat(),
    }
    code, out, _ = run(capsys, "up", "crm", "--fresh", "--type", "cx23", "--location", "fsn1", "--json")
    assert code == 0
    assert any("budget check is incomplete" in w for w in json.loads(out)["warnings"])
    code, out, _ = run(capsys, "status")
    assert "with unknown price" in out


# Issue #16: a pause whose delete fails leaves an accurate, recoverable state.


def test_pause_delete_failure_is_recorded_and_recoverable(capsys, isolated, cloud):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.fail_delete = True
    code, _, err = run(capsys, "pause", "crm")
    assert code == 1
    assert "is saved, but deleting server" in err and "still billed" in err
    image = next(iter(cloud.images))
    conn = db.connect(config.load().db_path)
    assert db.active_server(conn, "crm")["power"] == "off"

    code, out, _ = run(capsys, "status", "crm")
    assert "powered off" in out and "still billed" in out
    code, _, err = run(capsys, "exec", "crm", "--", "true")
    assert code == 1 and "powered off" in err

    # Retrying finishes the pause with the same snapshot.
    cloud.fail_delete = False
    snapshots_before = len([c for c in cloud.calls if c[0] == "snapshot"])
    code, out, err = run(capsys, "pause", "crm")
    assert code == 0, err
    assert len([c for c in cloud.calls if c[0] == "snapshot"]) == snapshots_before
    assert list(cloud.images) == [image] and cloud.servers == {}


def test_up_powers_on_a_shut_down_server(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.fail_delete = True
    run(capsys, "pause", "crm")
    code, out, err = run(capsys, "up", "crm")
    assert code == 0, err
    assert "powered on server" in out
    assert cloud.calls[-1][0] == "poweron"
    code, out, _ = run(capsys, "status", "crm")
    assert "crm: running" in out


def test_snapshot_failure_records_power_state(capsys, isolated, cloud, monkeypatch):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.fail_snapshot = True

    def broken_poweron(server_id):
        raise DevboxError("api down")

    monkeypatch.setattr(cloud, "poweron", broken_poweron)
    code, _, err = run(capsys, "pause", "crm")
    assert code == 1 and "powered off (still billed)" in err
    assert db.active_server(db.connect(config.load().db_path), "crm")["power"] == "off"


# Issue #17: commands a person runs count as activity; status does not.


@pytest.mark.parametrize("args", [["ps", "crm"], ["stop", "crm", "web"]])
def test_inspection_commands_reset_idle_timer(capsys, isolated, cloud, monkeypatch, args):
    monkeypatch.setattr(cli.Remote, "stop_unit", lambda self, name: True)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    make_idle(isolated)
    code, _, _ = run(capsys, *args)
    assert code == 0
    code, out, _ = run(capsys, "idle-check", "--dry-run")
    assert "would pause" not in out


def test_status_does_not_reset_idle_timer(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    make_idle(isolated)
    run(capsys, "status")
    code, out, _ = run(capsys, "idle-check", "--dry-run")
    assert "would pause" in out
