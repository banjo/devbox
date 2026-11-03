"""Tests for issue #36: the primary IPv4 price in every cost."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from devbox import config, costs, db
from devbox.hetzner import Hcloud

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)

IPV4 = {"fsn1": (0.001, 0.6), "hel1": (0.001, 0.6)}


def conn():
    return db.connect(config.load().db_path)


def server_row(**kw):
    return {"id": 1, "project": "crm", "hetzner_id": 7, "server_type": "cx23", "location": "fsn1",
            "created_at": "2026-10-01T00:00:00+00:00", "deleted_at": "2026-10-01T02:30:00+00:00",
            "price_hourly": 0.01, "price_monthly": 5.0, **kw}


def test_ipv4_is_billed_per_started_hour_on_top_of_the_server():
    usage = costs.server_usage(costs.server_item(server_row(ipv4_price_hourly=0.001, ipv4_price_monthly=0.6)))
    assert usage["billed_hours"] == 3
    assert usage["ipv4_cost"] == pytest.approx(0.003)
    assert usage["cost"] == pytest.approx(0.033)


def test_ipv4_is_capped_at_its_monthly_price():
    month = server_row(deleted_at="2026-11-01T00:00:00+00:00", ipv4_price_hourly=0.001, ipv4_price_monthly=0.6)
    usage = costs.server_usage(costs.server_item(month))
    assert usage["ipv4_cost"] == pytest.approx(0.6) and usage["cost"] == pytest.approx(5.6)


def test_without_an_ipv4_price_costs_are_as_before():
    usage = costs.server_usage(costs.server_item(server_row()))
    assert usage["ipv4_cost"] is None and usage["cost"] == pytest.approx(0.03)


def test_summary_reports_the_ipv4_share_and_still_adds_up():
    items = [costs.server_item(server_row(ipv4_price_hourly=0.001, ipv4_price_monthly=0.6)),
             costs.server_item(server_row(id=2, hetzner_id=8, project="api"))]
    s = costs.summarize(items, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert s["ipv4_total"] == pytest.approx(0.003)
    assert {g["key"]: g["ipv4_cost"] for g in s["groups"]} == pytest.approx({"api": 0.0, "crm": 0.003})
    assert s["total"] == pytest.approx(sum(g["cost"] for g in s["groups"]))


def test_up_stores_the_ipv4_price_of_the_location(capsys, isolated, cloud):
    cloud.ipv4 = IPV4
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, out, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert code == 0, out + err
    server = db.active_server(conn(), "crm")
    assert (server["ipv4_price_hourly"], server["ipv4_price_monthly"]) == (0.001, 0.6)

    code, out, _ = run(capsys, "status", "crm", "--json")
    assert json.loads(out)["projects"][0]["usage"]["ipv4_cost"] == pytest.approx(0.001)
    code, out, _ = run(capsys, "costs")
    assert "for IPv4" in out


def test_older_servers_get_the_current_price_once(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    c = conn()
    old = db.insert_server(c, "crm", 70, "running", ip="192.0.2.70", server_type="cx23", location="hel1",
                           created_at="2026-10-01T08:00:00+00:00", price_hourly=0.01, price_monthly=5.0)
    db.mark_deleted(c, old)
    cloud.ipv4 = IPV4
    run(capsys, "status")
    assert db.get_server(conn(), old)["ipv4_price_hourly"] == 0.001


def test_config_price_is_the_fallback(capsys, isolated, cloud):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text("ipv4_price_monthly = 0.73\n")
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    server = db.active_server(conn(), "crm")
    assert server["ipv4_price_monthly"] == 0.73 and server["ipv4_price_hourly"] == pytest.approx(0.001)


def test_hcloud_reads_ipv4_prices_from_the_pricing_api():
    hc = Hcloud("token", "abc")
    hc._pricing = {"image": {"price_per_gb_month": {"gross": "0.0143"}}, "primary_ips": [
        {"type": "ipv6", "prices": [{"location": "fsn1", "price_hourly": {"gross": "0"}}]},
        {"type": "ipv4", "prices": [{"location": "fsn1", "price_hourly": {"net": "0.0008", "gross": "0.000952"},
                                     "price_monthly": {"net": "0.5", "gross": "0.595"}}]},
    ]}
    assert hc.ipv4_prices() == {"fsn1": (0.000952, 0.595)}
    assert hc.snapshot_price_per_gb_month() == 0.0143
    hc._pricing = None
    assert hc.ipv4_prices() == {} and hc.snapshot_price_per_gb_month() is None


# #40: the IPv4 share in the human output


def test_money_shows_tenths_of_a_cent_only_when_cents_hide_them():
    assert costs.money(0.002) == "€0.002" and costs.money(0.013) == "€0.013"
    assert costs.money(0.01) == "€0.01" and costs.money(0.0) == "€0.00"
    assert costs.money(1.2345) == "€1.23" and costs.money(None) == "€?"


def test_pause_and_costs_show_the_ipv4_share(capsys, isolated, cloud):
    cloud.ipv4 = IPV4
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    c = conn()
    c.execute("UPDATE servers SET created_at = ?",
              ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),))
    c.commit()
    code, out, _ = run(capsys, "costs")
    assert "€0.001 for IPv4" in out
    code, out, _ = run(capsys, "pause", "crm")
    assert "(estimate, server + €0.001 IPv4, incl. VAT)" in out and "server only" not in out
