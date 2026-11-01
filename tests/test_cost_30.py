"""Tests for issue #30: keep idle servers until the paid billing hour ends."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from devbox import DevboxError, admin, autostop, billing, config, costs, db

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_bugfixes_24_29 import add_project

UTC = timezone.utc
T0 = datetime(2026, 11, 1, 12, 0, 7, tzinfo=UTC)
M = timedelta(minutes=1)


# Billing hours


@pytest.mark.parametrize("minutes, expected", [
    (0, 60), (1, 60), (59.9, 60), (60, 120), (61, 120), (-5, 60),
])
def test_next_boundary_counts_from_creation(minutes, expected):
    assert billing.next_boundary(T0, T0 + minutes * M) == T0 + expected * M


def test_boundary_ignores_clock_hours_and_month_changes():
    created = datetime(2026, 10, 31, 23, 59, 30, tzinfo=UTC)
    assert billing.next_boundary(created, created + 2 * M) == datetime(2026, 11, 1, 0, 59, 30, tzinfo=UTC)


def plan(ready_min, now_min, interval=5, action=5, margin=5):
    t = billing.planned_stop(T0, T0 + ready_min * M, T0 + now_min * M, interval * M, action * M, margin * M)
    return (t - T0) / M


def test_stop_is_planned_in_the_window_before_the_boundary():
    # Window [45, 50] for interval 5, action 5, margin 5.
    assert plan(ready_min=5, now_min=10) == 45
    assert plan(ready_min=5, now_min=47) == 47
    assert plan(ready_min=48, now_min=40) == 48
    # Too late for this hour (the next one is already started by the time it finishes): next window.
    assert plan(ready_min=5, now_min=51) == 105
    assert plan(ready_min=52, now_min=10) == 105


def test_a_longer_interval_or_action_starts_the_window_earlier():
    assert plan(ready_min=0, now_min=1, interval=15) == 35
    assert plan(ready_min=0, now_min=1, action=15) == 35


def test_missed_boundary():
    assert billing.missed_boundary(T0, T0, T0 + 30 * M, 5 * M, 5 * M) is None
    assert billing.missed_boundary(T0, T0, T0 + 70 * M, 5 * M, 5 * M) == T0 + 60 * M
    # Became ready too late to stop before that boundary: nothing was missed.
    assert billing.missed_boundary(T0, T0 + 55 * M, T0 + 70 * M, 5 * M, 5 * M) is None


def test_action_time_uses_recent_runs(tmp_path):
    assert billing.action_time(tmp_path, "pause") == timedelta(minutes=5)
    assert billing.action_time(tmp_path, "down") == timedelta(minutes=1)
    for seconds in (400, 120, 90, 80, 70, 60):
        billing.record_action_time(tmp_path, "pause", seconds)
    # The 400 s run is no longer among the five most recent.
    assert billing.action_time(tmp_path, "pause") == timedelta(seconds=120)
    assert billing.action_time(tmp_path, "down") == timedelta(minutes=1)


# Config


def test_hour_end_is_a_valid_idle_timeout(isolated):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('idle_timeout = "hour-end"\nmin_idle = "0m"\nbilling_margin = "3m"\n')
    cfg = config.load()
    assert config.parse_idle_timeout(cfg.idle_timeout) == config.HOUR_END
    assert config.parse_duration(cfg.billing_margin) == 3 * M

    (cfg_dir / "config.toml").write_text('idle_timeout = "hourend"\n')
    with pytest.raises(DevboxError, match="hour-end"):
        config.load()
    (cfg_dir / "config.toml").write_text('min_idle = "off"\n')
    with pytest.raises(DevboxError, match="min_idle"):
        config.load()


# CLI


def server(cloud, created: datetime, sid=78):
    cloud.servers[sid] = {
        "id": sid, "name": "devbox-crm", "status": "running", "ip": "192.0.2.8",
        "server_type": "cx23", "location": "fsn1", "project": "crm",
        "created": created.isoformat(timespec="seconds"),
    }


def hour_end_server(capsys, isolated, cloud, minutes_ago, idle_minutes=None):
    """A hour-end project whose server was created `minutes_ago` (no crontab: 15 min interval)."""
    add_project(capsys, isolated)
    run(capsys, "project", "set", "crm", "--idle-timeout", "hour-end")
    now = datetime.now(UTC)
    server(cloud, now - minutes_ago * M)
    run(capsys, "status")
    if idle_minutes is not None:
        conn = db.connect(config.load().db_path)
        row = db.active_server(conn, "crm")
        conn.execute("INSERT INTO events (project, server_id, kind, at) VALUES ('crm', ?, 'exec', ?)",
                     (row["id"], (now - idle_minutes * M).isoformat(timespec="seconds")))


def idle_check(capsys):
    code, out, _ = run(capsys, "idle-check", "--json")
    return code, json.loads(out)[0]


def test_hour_end_keeps_an_idle_server_until_the_window(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=10)
    code, entry = idle_check(capsys)
    assert code == 0 and entry["action"] is None and entry["policy"] == "hour-end"
    # Manual schedule (15 min) + 5 min pause + 5 min margin: window starts 35 min after creation.
    created = costs.parse_ts(db.active_server(db.connect(config.load().db_path), "crm")["created_at"])
    assert costs.parse_ts(entry["planned_at"]) == created + 35 * M
    assert costs.parse_ts(entry["paid_until"]) == created + 60 * M
    assert len(cloud.servers) == 1

    code, out, _ = run(capsys, "status")
    assert "idle" in out and "hour-end: paid until" in out and "pause at about" in out


def test_hour_end_pauses_inside_the_window(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=40)
    code, out, _ = run(capsys, "idle-check")
    assert code == 0 and "paused" in out and "hour-end" in out
    assert cloud.servers == {} and len(cloud.images) == 1
    # The pause was timed for next time.
    times = json.loads((isolated / "state" / "devbox" / billing.TIMES_FILE).read_text())
    assert len(times["pause"]) == 1


def test_hour_end_waits_for_min_idle(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=40, idle_minutes=2)
    code, entry = idle_check(capsys)
    assert entry["action"] is None and 2 * 60 < entry["remaining_seconds"] <= 3 * 60 + 5
    assert len(cloud.servers) == 1


def test_too_late_in_the_hour_waits_for_the_next_one(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=55)
    code, entry = idle_check(capsys)
    assert entry["action"] is None and len(cloud.servers) == 1
    assert 30 * 60 < entry["remaining_seconds"] <= 40 * 60
    assert entry["missed_boundary"] is None


def test_busy_server_is_tried_again_next_hour_and_the_missed_hour_is_reported(capsys, isolated, cloud):
    hour_end_server(capsys, isolated, cloud, minutes_ago=40)
    cloud.running_work = [{"unit": "devbox-build.service", "kind": "keep-alive job"}]
    code, out, _ = run(capsys, "idle-check")
    assert "tried again before the next billing hour" in out and len(cloud.servers) == 1

    # Later the same server is past its first boundary.
    cloud.servers[78]["created"] = (datetime.now(UTC) - 75 * M).isoformat(timespec="seconds")
    cloud.running_work = []
    code, entry = idle_check(capsys)
    assert entry["action"] is None and entry["missed_boundary"]
    code, out, _ = run(capsys, "autostop", "status")
    assert "was not used to stop it" in out


def test_pause_at_hour_end_marks_and_idle_check_carries_it_out(capsys, isolated, cloud):
    add_project(capsys, isolated)
    server(cloud, datetime.now(UTC) - 10 * M)
    code, out, _ = run(capsys, "pause", "crm", "--at-hour-end", "--json")
    data = json.loads(out)
    assert code == 0 and data["stop_action"] == "pause" and data["planned_at"]
    # No healthy schedule in the test environment: the user is told nothing will act on it.
    assert "nothing will carry this out" in data["warning"]
    assert len(cloud.servers) == 1 and not cloud.images

    code, out, _ = run(capsys, "status")
    assert "pause requested: paid until" in out and "cancels the pause" in out
    # Looking at the server doesn't cancel it.
    run(capsys, "ps", "crm")
    code, entry = idle_check(capsys)
    assert entry["policy"] == "requested" and entry["action"] is None

    cloud.servers[78]["created"] = (datetime.now(UTC) - 40 * M).isoformat(timespec="seconds")
    code, out, _ = run(capsys, "idle-check")
    assert "paused" in out and cloud.servers == {} and len(cloud.images) == 1
    kinds = [r["kind"] for r in db.connect(config.load().db_path).execute("SELECT kind FROM events")]
    assert "hour_end_pause" in kinds


def test_down_at_hour_end_deletes_without_snapshot(capsys, isolated, cloud):
    add_project(capsys, isolated)
    server(cloud, datetime.now(UTC) - 40 * M)
    run(capsys, "down", "crm", "--at-hour-end")
    code, out, _ = run(capsys, "idle-check")
    assert "deleted" in out and cloud.servers == {} and not cloud.images


@pytest.mark.parametrize("argv", [["touch", "crm"], ["up", "crm"]])
def test_using_the_server_cancels_the_stop(capsys, isolated, cloud, argv):
    add_project(capsys, isolated)
    server(cloud, datetime.now(UTC) - 40 * M)
    run(capsys, "pause", "crm", "--at-hour-end")
    code, out, _ = run(capsys, *argv)
    assert code == 0
    if argv[0] == "up":
        assert "cancelled the pause planned for the end of the billing hour" in out
    code, entry = idle_check(capsys)
    assert entry["policy"] == "timeout" and entry["action"] is None
    assert len(cloud.servers) == 1


def test_at_hour_end_edge_cases(capsys, isolated, cloud):
    add_project(capsys, isolated)
    code, out, _ = run(capsys, "pause", "crm", "--at-hour-end")
    assert code == 0 and "has no server" in out
    code, _, err = run(capsys, "pause", "crm", "--at-hour-end", "--dump")
    assert code == 1 and "--dump can't be combined" in err


def test_reconcile_takes_the_creation_time_from_hetzner(capsys, isolated, cloud):
    add_project(capsys, isolated)
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    cloud.servers[101]["created"] = "2026-10-31T08:00:00Z"
    run(capsys, "status")
    row = db.active_server(db.connect(config.load().db_path), "crm")
    assert row["created_at"] == "2026-10-31T08:00:00+00:00"


def test_autostop_status_suggests_the_shorter_interval(capsys, isolated, cloud, monkeypatch):
    hour_end_server(capsys, isolated, cloud, minutes_ago=10)
    config_dir, state_dir = autostop.homes()
    crontab = (f"{autostop.MARKER} config={config_dir} state={state_dir}\n"
               "*/15 * * * * XDG_CONFIG_HOME=/x python -m devbox idle-check\n")
    monkeypatch.setattr(admin, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, crontab, "")
                        if cmd[:2] == ["crontab", "-l"] else None)
    code, out, _ = run(capsys, "autostop", "status", "--json")
    assert json.loads(out)["interval_minutes"] == 15
    code, out, _ = run(capsys, "autostop", "status")
    assert "updates it to every 5 min" in out


# Monthly cap


def priced(created, deleted=None, monthly=10.0):
    return {"id": 1, "created_at": created.isoformat(), "deleted_at": deleted.isoformat() if deleted else None,
            "price_hourly": monthly / 730, "price_monthly": monthly}


def test_monthly_cap_hint():
    now = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
    # 20 servers of 12 h each in 20 days: on track for about half of an always-on server.
    rows = [priced(datetime(2026, 11, d, 8, tzinfo=UTC), datetime(2026, 11, d, 20, tzinfo=UTC)) for d in range(1, 21)]
    assert costs.monthly_cap_hint(rows, now) is None
    # 30 ten-minute servers a day: each pays a full hour, 30 billed hours a day.
    rows = [priced(datetime(2026, 11, d, tzinfo=UTC) + k * 40 * M, datetime(2026, 11, d, tzinfo=UTC) + (k * 40 + 10) * M)
            for d in range(1, 21) for k in range(30)]
    hint = costs.monthly_cap_hint(rows, now)
    assert hint["servers"] == 600 and hint["billed_hours"] == pytest.approx(600)
    assert hint["projected"] > hint["always_on"] == pytest.approx(720 * 10 / 730)
    # One server, which reaches its own cap anyway: no hint.
    assert costs.monthly_cap_hint([priced(datetime(2026, 11, 1, tzinfo=UTC))], now) is None
