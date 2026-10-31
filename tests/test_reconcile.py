import pytest

from devbox import db


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "devbox.db")
    db.add_project(conn, "crm", "/code/crm")
    db.add_project(conn, "blog", "/code/blog")
    return conn


def live(id, project, ip="1.2.3.4", status="running"):
    return {
        "id": id,
        "name": f"devbox-{project}",
        "status": status,
        "ip": ip,
        "server_type": "cx23",
        "location": "fsn1",
        "project": project,
        "created": "2026-10-31T10:00:00+00:00",
    }


def test_server_gone_in_hetzner_is_marked_deleted(conn):
    db.insert_server(conn, "crm", 10, "running", ip="1.2.3.4")
    assert db.reconcile(conn, []) == []
    assert db.active_server(conn, "crm") is None


def test_unrecorded_server_is_adopted(conn):
    orphans = db.reconcile(conn, [live(11, "crm")])
    assert orphans == []
    server = db.active_server(conn, "crm")
    assert server["hetzner_id"] == 11
    assert server["status"] == "running"
    assert server["ip"] == "1.2.3.4"


def test_server_still_starting_is_adopted_as_creating(conn):
    db.reconcile(conn, [live(11, "crm", status="initializing")])
    assert db.active_server(conn, "crm")["status"] == "creating"


def test_ip_change_is_recorded(conn):
    db.insert_server(conn, "crm", 10, "running", ip="1.2.3.4")
    db.reconcile(conn, [live(10, "crm", ip="5.6.7.8")])
    assert db.active_server(conn, "crm")["ip"] == "5.6.7.8"


def test_unknown_project_is_reported(conn):
    orphans = db.reconcile(conn, [live(12, "unknown")])
    assert [o["id"] for o in orphans] == [12]
    assert db.active_servers(conn) == []


def test_second_server_for_same_project_is_reported(conn):
    db.insert_server(conn, "crm", 10, "running")
    orphans = db.reconcile(conn, [live(10, "crm"), live(13, "crm")])
    assert [o["id"] for o in orphans] == [13]
    assert db.active_server(conn, "crm")["hetzner_id"] == 10


def test_matching_state_is_unchanged(conn):
    db.insert_server(conn, "crm", 10, "running", ip="1.2.3.4")
    assert db.reconcile(conn, [live(10, "crm")]) == []
    assert [s["hetzner_id"] for s in db.active_servers(conn)] == [10]


def image(id, project="crm", status="available"):
    return {"id": id, "description": "x", "status": status, "size_gb": 2.0, "disk_gb": 40,
            "architecture": "x86", "project": project, "created": "2026-10-31T10:00:00+00:00"}


def test_snapshot_gone_in_hetzner_is_marked_deleted(conn):
    db.insert_snapshot(conn, "crm", 500, size_gb=2.0)
    assert db.reconcile_snapshots(conn, []) == []
    assert db.latest_snapshot(conn, "crm") is None


def test_unrecorded_snapshot_is_adopted(conn):
    # For example, a crash during `pause` after the snapshot was created.
    assert db.reconcile_snapshots(conn, [image(501)]) == []
    assert db.latest_snapshot(conn, "crm")["hetzner_image_id"] == 501


def test_unknown_project_snapshot_is_reported(conn):
    assert [i["id"] for i in db.reconcile_snapshots(conn, [image(502, project="nope")])] == [502]
