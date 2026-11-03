"""Tests for issue #37: hold a server while testing the preview (`touch --for`)."""

import json
from datetime import datetime, timedelta, timezone

from devbox import config, costs, db

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_cost_30 import hour_end_server

M = timedelta(minutes=1)


def conn():
    return db.connect(config.load().db_path)


def idle_server(capsys, isolated, idle_minutes):
    """crm with a 10m idle timeout, last used `idle_minutes` ago."""
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "project", "set", "crm", "--idle-timeout", "10m")
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    set_idle(idle_minutes)


def set_idle(minutes):
    at = (datetime.now(timezone.utc) - minutes * M).isoformat(timespec="seconds")
    c = conn()
    c.execute("UPDATE events SET at = ?", (at,))
    c.execute("UPDATE servers SET created_at = ?", (at,))


def set_hold(minutes_from_now):
    at = (datetime.now(timezone.utc) + minutes_from_now * M).isoformat(timespec="seconds")
    conn().execute("UPDATE servers SET hold_until = ? WHERE status != 'deleted'", (at,))


def idle_check(capsys):
    code, out, _ = run(capsys, "idle-check", "--json")
    return json.loads(out)[0]


def test_hold_keeps_an_idle_server_until_it_ends(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    code, out, _ = run(capsys, "touch", "crm", "--for", "20m")
    assert code == 0 and "held until" in out and "--clear" in out

    set_idle(15)  # past the 10m timeout, inside the hold
    entry = idle_check(capsys)
    assert entry["action"] is None and entry["hold_until"] and len(cloud.servers) == 1
    code, out, _ = run(capsys, "idle-check")
    assert "held until" in out
    code, out, _ = run(capsys, "status", "crm")
    assert "held until" in out

    # When the hold is over, the normal rule applies at once: no second full timeout.
    set_hold(-1)
    entry = idle_check(capsys)
    assert entry["action"] == "pause" and cloud.servers == {}


def test_planned_stop_is_the_later_of_timeout_and_hold(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    run(capsys, "touch", "crm", "--for", "30m")
    set_idle(5)
    entry = idle_check(capsys)
    hold = costs.parse_ts(entry["hold_until"])
    assert costs.parse_ts(entry["planned_at"]) == hold
    assert 29 * 60 < entry["remaining_seconds"] <= 30 * 60


def test_clear_ends_the_hold(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    run(capsys, "touch", "crm", "--for", "2h")
    code, out, _ = run(capsys, "touch", "crm", "--clear")
    assert code == 0 and "hold cleared" in out
    assert db.active_server(conn(), "crm")["hold_until"] is None
    set_idle(15)
    assert idle_check(capsys)["action"] == "pause"


def test_hold_is_bounded(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    for value in ("25h", "0m", "soon", "off"):
        code, _, err = run(capsys, "touch", "crm", "--for", value)
        assert code == 1 and ("up to 24h" in err or "invalid duration" in err), value
    code, _, err = run(capsys, "touch", "crm", "--for", "1h", "--clear")
    assert code == 1 and "either --for or --clear" in err
    assert db.active_server(conn(), "crm")["hold_until"] is None


def test_hold_moves_an_hour_end_stop_past_it(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=40)  # inside the window: would pause now
    set_hold(30)
    entry = idle_check(capsys)
    assert entry["action"] is None and len(cloud.servers) == 1
    created = costs.parse_ts(db.active_server(conn(), "crm")["created_at"])
    # Ready at the hold's end (70 min after creation): the next window, 35 min into the second hour.
    assert costs.parse_ts(entry["planned_at"]) == created + 95 * M


def test_hold_moves_a_requested_hour_end_pause(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=40)
    run(capsys, "pause", "crm", "--at-hour-end")
    set_hold(30)
    entry = idle_check(capsys)
    assert entry["policy"] == "requested" and entry["action"] is None and len(cloud.servers) == 1


def test_autostop_status_shows_the_hold(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    run(capsys, "touch", "crm", "--for", "1h")
    code, out, _ = run(capsys, "autostop", "status")
    assert "held until" in out
    code, out, _ = run(capsys, "autostop", "status", "--json")
    assert json.loads(out)["servers"][0]["hold_until"]


def test_new_server_starts_without_a_hold(capsys, isolated, cloud):
    idle_server(capsys, isolated, idle_minutes=0)
    run(capsys, "touch", "crm", "--for", "1h")
    run(capsys, "pause", "crm")
    run(capsys, "up", "crm")
    assert db.active_server(conn(), "crm")["hold_until"] is None
