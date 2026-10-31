from datetime import datetime, timezone

import pytest

from devbox import costs, db


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def server(id=1, project="app", created="2026-10-01T10:00:00", deleted=None, hourly=0.01, monthly=5.0,
           type="cx23", location="fsn1"):
    return {
        "id": id, "project": project, "hetzner_id": 100 + id, "server_type": type, "location": location,
        "created_at": created + "+00:00", "deleted_at": deleted + "+00:00" if deleted else None,
        "price_hourly": hourly, "price_monthly": monthly,
    }


def test_billed_per_started_hour():
    assert costs.billed_hours(ts("2026-10-01T10:00:00"), ts("2026-10-01T10:00:01")) == 1
    assert costs.billed_hours(ts("2026-10-01T10:00:00"), ts("2026-10-01T12:00:00")) == 2
    assert costs.cost_between(ts("2026-10-01T10:00:00"), ts("2026-10-01T12:30:00"), 0.01, 5.0) == pytest.approx(0.03)


def test_monthly_cap_per_calendar_month():
    cost = costs.cost_between(ts("2026-10-01T00:00:00"), ts("2026-11-15T00:00:00"), 0.01, 5.0)
    # October capped at 5.0, then 14 days = 336 h of November.
    assert cost == pytest.approx(5.0 + 3.36)


def test_unknown_price():
    assert costs.cost_between(ts("2026-10-01T00:00:00"), ts("2026-10-02T00:00:00"), None, None) is None


def test_running_server_uses_now():
    s = server(created="2026-10-31T10:00:00")
    usage = costs.server_usage(s, now=ts("2026-10-31T13:10:00"))
    assert usage["billed_hours"] == 4
    assert usage["cost"] == pytest.approx(0.04)


def test_summarize_by_project_and_period():
    servers = [
        server(1, "app", "2026-09-30T22:00:00", "2026-10-01T02:00:00"),
        server(2, "app", "2026-10-10T10:00:00", "2026-10-10T12:00:00"),
        server(3, "blog", "2026-10-20T10:00:00", "2026-10-20T11:00:00", hourly=None, monthly=None),
        server(4, "app", "2026-11-02T10:00:00", "2026-11-02T11:00:00"),
    ]
    since, until = costs.parse_month("2026-10")
    s = costs.summarize(servers, since, until, "project", now=ts("2026-12-01T00:00:00"))
    groups = {g["key"]: g for g in s["groups"]}
    assert groups["app"]["billed_hours"] == 4  # 2h of server 1 in October + 2h of server 2
    assert groups["app"]["cost"] == pytest.approx(0.04)
    assert groups["blog"]["unknown_price"] == 1
    assert s["total"] == pytest.approx(0.04)
    assert s["servers"] == 3
    assert s["unknown_price"] == 1


def test_summarize_by_month_splits_servers():
    servers = [server(1, "app", "2026-09-30T22:00:00", "2026-10-01T02:00:00")]
    s = costs.summarize(servers, by="month", now=ts("2026-12-01T00:00:00"))
    assert [(g["key"], g["billed_hours"]) for g in s["groups"]] == [("2026-09", 2), ("2026-10", 2)]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "devbox.db")


def test_removed_project_keeps_history_and_can_be_revived(conn):
    db.add_project(conn, "app", "/code/app")
    sid = db.insert_server(conn, "app", 1, "running", price_hourly=0.01, price_monthly=5.0)
    db.add_event(conn, "app", sid, "sync")
    db.add_event(conn, "app", sid, "sync")
    db.add_event(conn, "app", sid, "exec")
    db.mark_deleted(conn, sid)
    db.remove_project(conn, "app")

    assert db.get_project(conn, "app") is None
    assert db.list_projects(conn) == []
    assert len(db.all_servers(conn, "app")) == 1
    assert db.event_counts(conn, sid) == {"sync": 2, "exec": 1}

    db.add_project(conn, "app", "/code/app2")
    assert db.get_project(conn, "app")["path"] == "/code/app2"
    assert len(db.all_servers(conn, "app")) == 1


def test_migration_from_v1_keeps_data(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for stmt in db._split(db.MIGRATIONS[0]):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO projects (name, path, created_at, updated_at) VALUES ('a', '/a', 'x', 'x')")
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert db.get_project(conn, "a")["path"] == "/a"
