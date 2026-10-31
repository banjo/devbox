import json
import stat

import pytest

from devbox import DevboxError
from devbox.hetzner import Hcloud, firewall_rules, server_info

SERVER = {
    "id": 42,
    "name": "devbox-crm",
    "status": "running",
    "created": "2026-10-31T10:00:00+00:00",
    "public_net": {"ipv4": {"ip": "1.2.3.4"}},
    "server_type": {"name": "cx23"},
    "location": {"name": "fsn1"},
    "labels": {"managed-by": "devbox", "project": "crm"},
}


@pytest.fixture
def fake_hcloud(tmp_path):
    """A fake hcloud that logs its arguments and prints a canned response."""
    log = tmp_path / "calls.jsonl"
    responses = tmp_path / "responses"
    responses.mkdir()
    script = tmp_path / "hcloud"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, sys, pathlib
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
key = "_".join(args[:2])
resp = pathlib.Path({str(responses)!r}) / key
if resp.exists():
    print(resp.read_text())
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def respond(key, data):
        (responses / key).write_text(json.dumps(data))

    return Hcloud("token", binary=str(script)), calls, respond


def test_server_info_reads_location_and_old_datacenter_shape():
    assert server_info(SERVER)["location"] == "fsn1"
    old = dict(SERVER, location=None, datacenter={"location": {"name": "nbg1"}})
    assert server_info(old)["location"] == "nbg1"
    assert server_info(SERVER)["project"] == "crm"
    assert server_info(SERVER)["ip"] == "1.2.3.4"


def test_list_servers_filters_by_label(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("server_list", [SERVER])
    assert [s["id"] for s in hc.list_servers()] == [42]
    assert calls()[0]["args"] == ["server", "list", "-l", "managed-by=devbox", "-o", "json"]


def test_create_server_labels_and_parses(fake_hcloud, tmp_path):
    hc, calls, respond = fake_hcloud
    respond("server_create", {"server": SERVER, "root_password": None})
    info = hc.create_server("devbox-crm", "crm", "cx23", "ubuntu-24.04", "fsn1", tmp_path / "ud.yaml")
    assert info["id"] == 42
    args = calls()[0]["args"]
    assert "managed-by=devbox" in args and "project=crm" in args
    assert args[args.index("--firewall") + 1] == "devbox"


def test_firewall_created_then_replaced(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("firewall_list", [])
    hc.ensure_firewall(["1.2.3.4/32"])
    assert calls()[-1]["args"][:2] == ["firewall", "create"]
    assert json.loads(calls()[-1]["stdin"]) == firewall_rules(["1.2.3.4/32"])

    respond("firewall_list", [{"name": "devbox"}])
    hc.ensure_firewall(["5.6.7.8/32"])
    assert calls()[-1]["args"][:3] == ["firewall", "replace-rules", "devbox"]


def test_ssh_key_mismatch_is_an_error(fake_hcloud):
    hc, _, respond = fake_hcloud
    respond("ssh-key_list", [{"name": "devbox", "public_key": "ssh-ed25519 AAAA other"}])
    hc.ensure_ssh_key("ssh-ed25519 AAAA devbox")
    with pytest.raises(DevboxError):
        hc.ensure_ssh_key("ssh-ed25519 BBBB devbox")


def test_missing_hcloud_binary():
    with pytest.raises(DevboxError, match="hcloud CLI not found"):
        Hcloud(None, binary="/nonexistent/hcloud").list_servers()


def server_type(name, cores, memory, arch, locations, deprecation=None):
    return {
        "name": name,
        "cores": cores,
        "memory": memory,
        "disk": 40,
        "architecture": arch,
        "cpu_type": "shared",
        "category": "cost_optimized",
        "deprecation": deprecation,
        "locations": [
            {"name": loc, "available": avail, "recommended": False, "deprecation": None}
            for loc, avail, _ in locations
        ],
        "prices": [
            {
                "location": loc,
                "price_hourly": {"net": "0.0060", "gross": "0.0072"},
                "price_monthly": {"net": str(price), "gross": str(price * 1.2)},
            }
            for loc, _, price in locations
        ],
    }


LOCATIONS = [
    {"name": "fsn1", "city": "Falkenstein", "country": "DE", "network_zone": "eu-central"},
    {"name": "ash", "city": "Ashburn, VA", "country": "US", "network_zone": "us-east"},
]

TYPES = [
    server_type("cx33", 4, 8.0, "x86", [("fsn1", True, 6.0)]),
    server_type("cx23", 2, 4.0, "x86", [("fsn1", False, 4.0), ("ash", True, 5.0)]),
    server_type("cax11", 2, 4.0, "arm", [("fsn1", True, 4.5)]),
    server_type("cx22", 2, 4.0, "x86", [("fsn1", True, 1.0)], deprecation={"announced": "x"}),
]


def test_build_offers_sorted_by_price_without_deprecated():
    from devbox.hetzner import build_offers

    offers = build_offers(TYPES, LOCATIONS)
    assert [(o["type"], o["location"]) for o in offers] == [
        ("cx23", "fsn1"), ("cax11", "fsn1"), ("cx23", "ash"), ("cx33", "fsn1"),
    ]
    first = offers[0]
    assert first["available"] is False
    assert first["network_zone"] == "eu-central"
    assert first["price_monthly"] == pytest.approx(4.8)


def test_filter_offers():
    from devbox.hetzner import build_offers, filter_offers

    offers = build_offers(TYPES, LOCATIONS)
    assert [o["type"] for o in filter_offers(offers)] == ["cax11", "cx23", "cx33"]
    assert [o["type"] for o in filter_offers(offers, architecture="x86", network_zone="eu-central")] == ["cx33"]
    assert [o["type"] for o in filter_offers(offers, min_memory=8)] == ["cx33"]
    assert len(filter_offers(offers, include_unavailable=True)) == 4


def test_no_available_flag_anywhere_means_unknown():
    from devbox.hetzner import build_offers

    types = [server_type("cx23", 2, 4.0, "x86", [("fsn1", False, 4.0)])]
    assert build_offers(types, LOCATIONS)[0]["available"] is None
