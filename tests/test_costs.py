from datetime import datetime, timedelta, timezone

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


# Issue #11: breakdowns and adjacent periods reconcile with totals.

NOW = ts("2026-12-01T00:00:00")


def capped_october():
    return server(1, "app", "2026-10-01T00:00:00", "2026-11-01T00:00:00", hourly=0.01, monthly=5.0)


@pytest.mark.parametrize("by", costs.GROUPINGS)
def test_every_grouping_adds_up_to_the_total(by):
    items = [
        capped_october(),
        server(2, "app", "2026-10-31T23:30:00", "2026-11-01T00:30:00"),
        server(3, "blog", "2026-10-01T23:30:00", "2026-10-02T00:30:00", type="cx33"),
    ]
    s = costs.summarize(items, by=by, now=NOW)
    assert sum(g["cost"] for g in s["groups"]) == pytest.approx(s["total"], abs=1e-3)
    assert sum(g["billed_hours"] for g in s["groups"]) == pytest.approx(s["billed_hours"], abs=0.05)
    # Grouping never changes the total.
    assert s["total"] == pytest.approx(costs.summarize(items, by="project", now=NOW)["total"])


def test_capped_month_by_day_keeps_the_cap():
    s = costs.summarize([capped_october()], by="day", now=NOW)
    assert s["total"] == pytest.approx(5.0)
    assert len(s["groups"]) == 31
    assert sum(g["cost"] for g in s["groups"]) == pytest.approx(5.0, abs=1e-3)


def test_hour_across_midnight_is_not_counted_twice():
    item = server(1, "app", "2026-10-01T23:30:00", "2026-10-02T00:30:00")
    s = costs.summarize([item], by="day", now=NOW)
    assert s["total"] == pytest.approx(0.01)
    assert [g["cost"] for g in s["groups"]] == [pytest.approx(0.005), pytest.approx(0.005)]
    assert sum(g["billed_hours"] for g in s["groups"]) == pytest.approx(1)


def test_hour_across_month_boundary_has_same_billed_hours_in_every_grouping():
    item = server(1, "app", "2026-10-31T23:30:00", "2026-11-01T00:30:00")
    by_project = costs.summarize([item], by="project", now=NOW)
    by_month = costs.summarize([item], by="month", now=NOW)
    assert by_project["billed_hours"] == by_month["billed_hours"] == 2
    assert by_project["groups"][0]["billed_hours"] == 2
    assert [g["billed_hours"] for g in by_month["groups"]] == [1, 1]
    assert by_project["total"] == by_month["total"] == pytest.approx(0.02)
    assert costs.server_usage(item, now=NOW)["billed_hours"] == 2


@pytest.mark.parametrize("cut", ["2026-10-31T00:00:00", "2026-10-15T12:00:00", "2026-10-02T00:00:00"])
def test_adjacent_periods_add_up_even_after_the_cap(cut):
    item = capped_october()
    start, mid, end = ts("2026-10-01T00:00:00"), ts(cut), ts("2026-11-01T00:00:00")
    first = costs.summarize([item], start, mid, now=NOW)["total"]
    second = costs.summarize([item], mid, end, now=NOW)["total"]
    whole = costs.summarize([item], start, end, now=NOW)["total"]
    assert whole == pytest.approx(5.0)
    assert first + second == pytest.approx(whole, abs=1e-3)


def test_period_share_is_proportional_to_running_time():
    item = capped_october()  # 744 h in October, capped at 5.0
    s = costs.summarize([item], ts("2026-10-31T00:00:00"), ts("2026-11-01T00:00:00"), now=NOW)
    assert s["total"] == pytest.approx(5.0 * 24 / 744, abs=1e-4)
    assert s["billed_hours"] == pytest.approx(24)


def test_running_server_cost_so_far_is_whole_hours():
    item = server(1, "app", "2026-11-30T22:10:00")
    usage = costs.server_usage(item, now=NOW)
    assert usage["billed_hours"] == 2
    assert usage["cost"] == pytest.approx(0.02)


# Issue #12: period parsing and validation.

from devbox import DevboxError  # noqa: E402


@pytest.mark.parametrize("value", ["2024-02-29", "2026-12-31", "2000-01-01"])
def test_valid_days(value):
    assert costs.parse_day(value).date().isoformat() == value


@pytest.mark.parametrize("value", ["2026-02-30", "2025-02-29", "garbage", "2026-13-01", "20261001", "2026-W01-1", "1999-12-31", "9999-12-31", ""])
def test_invalid_days(value):
    with pytest.raises(DevboxError):
        costs.parse_day(value)


@pytest.mark.parametrize("value", ["2026-13", "2026-00", "9999-12", "1999-12", "2026-1", "garbage"])
def test_invalid_months(value):
    with pytest.raises(DevboxError):
        costs.parse_month(value)


def test_month_bounds():
    assert costs.parse_month("2026-12") == (ts("2026-12-01T00:00:00"), ts("2027-01-01T00:00:00"))
    assert costs.parse_month("9998-12")[1] == ts("9999-01-01T00:00:00")


@pytest.mark.parametrize("value", ["0", "-1", "1000000000", "abc", "1.5", str(costs.MAX_DAYS + 1)])
def test_invalid_days_count(value):
    with pytest.raises(DevboxError):
        costs.parse_days(value)


def test_days_period():
    since, until = costs.resolve_period(None, "7", None, None, NOW)
    assert since == NOW - timedelta(days=7) and until is None


@pytest.mark.parametrize("args", [
    (None, None, "2026-11-01", "2026-10-01"),
    ("2026-10", None, None, "2026-09-01"),
    (None, None, "2026-10-01", "2026-10-01"),
])
def test_empty_or_reversed_periods_are_rejected(args):
    with pytest.raises(DevboxError, match="empty or reversed"):
        costs.resolve_period(*args, NOW)
