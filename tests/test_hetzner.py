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

    return Hcloud("token", "abc123", binary=str(script)), calls, respond


def test_server_info_reads_location_and_old_datacenter_shape():
    assert server_info(SERVER)["location"] == "fsn1"
    old = dict(SERVER, location=None, datacenter={"location": {"name": "nbg1"}})
    assert server_info(old)["location"] == "nbg1"
    assert server_info(SERVER)["project"] == "crm"
    assert server_info(SERVER)["ip"] == "1.2.3.4"


OWNED = {"managed-by": "devbox", "devbox-controller": "abc123"}


def test_list_servers_filters_by_label_and_controller(fake_hcloud):
    hc, calls, respond = fake_hcloud
    other = dict(SERVER, id=43, labels={"managed-by": "devbox", "devbox-controller": "other", "project": "x"})
    legacy = dict(SERVER, id=44, labels={"managed-by": "devbox", "project": "old"})
    mine = dict(SERVER, labels={**OWNED, "project": "crm"})
    respond("server_list", [mine, other, legacy])
    assert [s["id"] for s in hc.list_servers()] == [42, 44]
    assert calls()[0]["args"] == ["server", "list", "-l", "managed-by=devbox", "-o", "json"]


def test_create_server_labels_and_parses(fake_hcloud, tmp_path):
    hc, calls, respond = fake_hcloud
    respond("server_create", {"server": SERVER, "root_password": None})
    info = hc.create_server(
        "devbox-crm", "crm", "cx23", "ubuntu-24.04", "fsn1", tmp_path / "ud.yaml",
        ssh_key_id=7, firewall_id=9,
    )
    assert info["id"] == 42
    args = calls()[0]["args"]
    assert "managed-by=devbox" in args and "project=crm" in args and "devbox-controller=abc123" in args
    assert args[args.index("--firewall") + 1] == "9"
    assert args[args.index("--ssh-key") + 1] == "7"


def test_firewall_created_then_replaced_by_id(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("firewall_list", [])
    respond("firewall_create", {"firewall": {"id": 5, "name": "devbox-abc123"}})
    assert hc.ensure_firewall(["192.0.2.1/32"]) == 5
    create = calls()[-1]
    assert create["args"][:2] == ["firewall", "create"]
    assert "devbox-abc123" in create["args"] and "devbox-controller=abc123" in create["args"]
    assert json.loads(create["stdin"]) == firewall_rules(["192.0.2.1/32"])

    respond("firewall_list", [{"id": 5, "name": "devbox-abc123", "labels": OWNED}])
    assert hc.ensure_firewall(["198.51.100.1/32"]) == 5
    assert calls()[-1]["args"][:3] == ["firewall", "replace-rules", "5"]


@pytest.mark.parametrize("labels", [
    {},
    {"owner": "somebody-else"},
    {"managed-by": "devbox"},
    {"managed-by": "devbox", "devbox-controller": "other"},
])
def test_firewall_not_owned_is_never_modified(fake_hcloud, labels):
    hc, calls, respond = fake_hcloud
    respond("firewall_list", [{"id": 999, "name": "devbox-abc123", "labels": labels}])
    with pytest.raises(DevboxError, match="refusing to modify"):
        hc.ensure_firewall(["192.0.2.1/32"])
    assert [c["args"][:2] for c in calls()] == [["firewall", "list"]]


def test_unrelated_firewall_named_devbox_is_ignored(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("firewall_list", [{"id": 999, "name": "devbox", "labels": {"owner": "somebody-else"}}])
    respond("firewall_create", {"firewall": {"id": 6}})
    assert hc.ensure_firewall(["192.0.2.1/32"]) == 6
    assert not any("999" in c["args"] or c["args"][:2] == ["firewall", "replace-rules"] for c in calls())


def test_ssh_key_ownership_and_mismatch(fake_hcloud):
    hc, _, respond = fake_hcloud
    respond("ssh-key_list", [{"id": 3, "name": "devbox-abc123", "public_key": "ssh-ed25519 AAAA x", "labels": OWNED}])
    assert hc.ensure_ssh_key("ssh-ed25519 AAAA devbox") == 3
    with pytest.raises(DevboxError, match="does not match"):
        hc.ensure_ssh_key("ssh-ed25519 BBBB devbox")
    respond("ssh-key_list", [{"id": 3, "name": "devbox-abc123", "public_key": "ssh-ed25519 AAAA x", "labels": {}}])
    with pytest.raises(DevboxError, match="refusing to modify"):
        hc.ensure_ssh_key("ssh-ed25519 AAAA devbox")


def test_missing_hcloud_binary():
    with pytest.raises(DevboxError, match="hcloud CLI not found"):
        Hcloud(None, "abc123", binary="/nonexistent/hcloud").list_servers()


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


IMAGE = {
    "id": 900, "description": "devbox crm", "status": "available", "image_size": 2.4,
    "disk_size": 40, "architecture": "x86", "created": "2026-10-31T10:00:00+00:00",
    "labels": {**OWNED, "project": "crm"},
}


def test_create_snapshot_labels_and_finds_image(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("image_list", [IMAGE])
    info = hc.create_snapshot(42, "crm", "devbox crm")
    assert info["id"] == 900 and info["size_gb"] == 2.4 and info["project"] == "crm"
    create = calls()[0]["args"]
    assert create[:4] == ["server", "create-image", "--type", "snapshot"]
    assert "devbox-controller=abc123" in create and "project=crm" in create
    op = next(a for a in create if a.startswith("devbox-op="))
    assert calls()[1]["args"][:6] == ["image", "list", "-t", "snapshot", "-l", op]


def test_list_snapshots_skips_other_controllers(fake_hcloud):
    hc, _, respond = fake_hcloud
    other = dict(IMAGE, id=901, labels={"managed-by": "devbox", "devbox-controller": "other"})
    respond("image_list", [IMAGE, other])
    assert [i["id"] for i in hc.list_snapshots()] == [900]


def test_delete_image_requires_ownership(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("image_describe", dict(IMAGE, labels={"owner": "someone"}))
    with pytest.raises(DevboxError, match="refusing"):
        hc.delete_image(900)
    assert not any(c["args"][:2] == ["image", "delete"] for c in calls())

    respond("image_describe", IMAGE)
    hc.delete_image(900)
    assert calls()[-1]["args"] == ["image", "delete", "900"]


def test_shutdown_falls_back_to_poweroff(tmp_path):
    hc = Hcloud(None, "abc123")
    seen = []

    def fake_run(*args, **kw):
        seen.append(args[:2])
        if args[1] == "shutdown":
            raise DevboxError("timeout")

    hc.run = fake_run
    hc.shutdown(42)
    assert seen == [("server", "shutdown"), ("server", "poweroff")]


def test_legacy_ssh_key_with_same_material_is_reused(fake_hcloud):
    # Issue #4: earlier versions registered the key as "devbox" without a controller label.
    hc, calls, respond = fake_hcloud
    respond("ssh-key_list", [
        {"id": 11, "name": "devbox", "public_key": "ssh-ed25519 AAAA old-comment", "labels": {"managed-by": "devbox"}},
    ])
    assert hc.ensure_ssh_key("ssh-ed25519 AAAA devbox") == 11
    assert not any(c["args"][:2] in (["ssh-key", "create"], ["ssh-key", "add-label"]) for c in calls())


def test_same_key_owned_by_this_controller_under_other_name_is_reused(fake_hcloud):
    hc, _, respond = fake_hcloud
    respond("ssh-key_list", [{"id": 12, "name": "mine", "public_key": "ssh-ed25519 AAAA", "labels": OWNED}])
    assert hc.ensure_ssh_key("ssh-ed25519 AAAA devbox") == 12


@pytest.mark.parametrize("labels, owner", [
    ({}, "something other than devbox"),
    ({"managed-by": "devbox", "devbox-controller": "other"}, "controller 'other'"),
])
def test_same_key_owned_by_someone_else_gives_actionable_error(fake_hcloud, labels, owner):
    hc, calls, respond = fake_hcloud
    respond("ssh-key_list", [{"id": 13, "name": "laptop", "public_key": "ssh-ed25519 AAAA", "labels": labels}])
    with pytest.raises(DevboxError) as e:
        hc.ensure_ssh_key("ssh-ed25519 AAAA devbox")
    msg = str(e.value)
    assert "'laptop' (id 13)" in msg and owner in msg
    assert "hcloud ssh-key add-label --overwrite 13 managed-by=devbox devbox-controller=abc123" in msg
    assert "ssh_key" in msg
    assert not any(c["args"][:2] == ["ssh-key", "create"] for c in calls())


def test_new_key_is_created_when_not_registered(fake_hcloud):
    hc, calls, respond = fake_hcloud
    respond("ssh-key_list", [{"id": 14, "name": "other", "public_key": "ssh-ed25519 ZZZZ", "labels": {}}])
    respond("ssh-key_create", {"ssh_key": {"id": 15}})
    assert hc.ensure_ssh_key("ssh-ed25519 AAAA devbox") == 15
    create = calls()[-1]["args"]
    assert create[:4] == ["ssh-key", "create", "--name", "devbox-abc123"]
