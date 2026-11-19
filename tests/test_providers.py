"""Provider interface: capabilities, registry, JSON compatibility and the database migration."""

import json
import sqlite3

import pytest

from devbox import config, db, providers
from devbox.providers import Capabilities, Provider
from devbox.providers.hetzner import HetznerProvider
from fake_provider import FakeProvider
from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)


def start(capsys, isolated, name="crm"):  # noqa: F811
    run(capsys, "project", "add", name, "--path", str(isolated / "crm"))
    code, _, err = run(capsys, "up", name, "--type", "cx23", "--location", "fsn1")
    assert code == 0, err


def limit(cloud, **caps):  # noqa: F811
    cloud.capabilities = Capabilities(**{**vars(FakeProvider.capabilities), **caps})


# Protocol and registry


def test_hetzner_and_fake_provider_implement_the_protocol():
    assert isinstance(FakeProvider(), Provider)
    assert isinstance(HetznerProvider(None, "abc"), Provider)


def test_hetzner_capabilities():
    caps = providers.capabilities("hetzner")
    assert caps == Capabilities(can_create=True, can_delete=True, has_snapshots=True, has_billing=True, has_offers=True)


def test_unknown_provider_is_an_error():
    with pytest.raises(Exception, match="unknown provider 'nope'"):
        providers.provider_class("nope")


def test_hetzner_provider_reads_hcloud_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    cfg_dir = tmp_path / "config" / "devbox"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f'hcloud_token_file = "{token}"\n')
    provider = providers.get("hetzner", config.load())
    assert provider.env["HCLOUD_TOKEN"] == "secret-token"
    assert provider.name == "hetzner" and provider.display_name == "Hetzner"


def test_provider_for_uses_the_provider_stored_with_the_server(capsys, isolated, cloud, monkeypatch):  # noqa: F811
    other = FakeProvider()
    other.name = "other"
    monkeypatch.setitem(providers._REGISTRY, "other", other)
    start(capsys, isolated)
    conn = db.connect(config.load().db_path)
    server = db.active_server(conn, "crm")
    assert server["provider"] == "hetzner"

    import argparse

    from devbox import cli

    ctx = cli.Context(argparse.Namespace(json=False))
    assert ctx.provider_for(server) is cloud
    assert ctx.provider_for({"provider": "other"}) is other
    assert ctx.hcloud is cloud  # back-compat alias


# Capabilities decide what commands do


def test_pause_needs_snapshots(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    limit(cloud, has_snapshots=False)
    code, _, err = run(capsys, "pause", "crm")
    assert code == 1 and "does not support snapshots" in err
    assert len(cloud.servers) == 1 and ("shutdown", next(iter(cloud.servers))) not in cloud.calls


def test_down_and_pause_need_delete(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    limit(cloud, can_delete=False)
    for cmd in ("down", "pause"):
        code, _, err = run(capsys, cmd, "crm", "--no-dump") if cmd == "down" else run(capsys, cmd, "crm")
        assert code == 1 and "does not support deleting servers" in err
    assert len(cloud.servers) == 1


def test_up_needs_can_create(capsys, isolated, cloud):  # noqa: F811
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    limit(cloud, can_create=False)
    code, _, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert code == 1 and "does not support creating servers" in err
    assert cloud.servers == {}


def test_server_types_needs_offers(capsys, isolated, cloud):  # noqa: F811
    limit(cloud, has_offers=False)
    code, _, err = run(capsys, "server-types")
    assert code == 1 and "does not support listing server types" in err


def test_at_hour_end_needs_billing(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    limit(cloud, has_billing=False)
    code, _, err = run(capsys, "pause", "crm", "--at-hour-end")
    assert code == 1 and "does not support --at-hour-end" in err
    assert db.active_server(db.connect(config.load().db_path), "crm")["stop_action"] is None


def test_costs_skip_providers_without_billing(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "costs", "--json")
    assert json.loads(out)["servers"] == 1 and json.loads(out)["snapshots"] == 1
    limit(cloud, has_billing=False)
    code, out, _ = run(capsys, "costs", "--json")
    data = json.loads(out)
    assert data["servers"] == 0 and data["snapshots"] == 0 and data["total"] == 0


def test_idle_check_skips_what_the_provider_cannot_do(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    conn = db.connect(config.load().db_path)
    conn.execute("UPDATE servers SET created_at = '2026-01-01T00:00:00+00:00'")
    conn.execute("UPDATE events SET at = '2026-01-01T00:00:00+00:00'")
    limit(cloud, has_snapshots=False)
    code, out, _ = run(capsys, "idle-check", "--json")
    entry = json.loads(out)[0]
    assert entry["action"] is None and "cannot pause" in entry["skipped"]
    assert len(cloud.servers) == 1


def test_cleanup_asks_the_provider(capsys, isolated, cloud):  # noqa: F811
    cloud.firewalls = [{"id": 9, "name": "old", "labels": {"managed-by": "devbox", "devbox-controller": "test"}}]
    cloud.keys = []
    code, out, _ = run(capsys, "cleanup", "--json")
    items = json.loads(out)["items"]
    assert [(i["kind"], i["id"]) for i in items] == [("firewall", 9)]
    run(capsys, "cleanup", "--yes")
    assert ("delete_firewall", 9) in cloud.calls


# Output keeps the Hetzner key names next to the neutral ones


def test_json_keeps_hetzner_keys(capsys, isolated, cloud):  # noqa: F811
    start(capsys, isolated)
    code, out, _ = run(capsys, "status", "--json")
    server = json.loads(out)["projects"][0]["server"]
    assert server["hetzner_id"] == server["provider_id"] and server["provider"] == "hetzner"
    assert server["host"] is None

    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "snapshot", "list", "--json")
    snap = json.loads(out)["snapshots"][0]
    assert snap["hetzner_image_id"] == snap["image_id"] == next(iter(cloud.images))
    code, out, _ = run(capsys, "history", "--json")
    data = json.loads(out)
    assert data["servers"][0]["hetzner_id"] == data["servers"][0]["provider_id"]
    assert data["snapshots"][0]["hetzner_image_id"] == data["snapshots"][0]["image_id"]


# Migration from the schema with Hetzner names


def old_database(path):
    """A database as the release before the provider interface wrote it."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    old = [m for m in db.MIGRATIONS[:-1]]
    for i, sql in enumerate(old, start=1):
        for statement in db._split(sql):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {i}")
    return conn


def test_migration_renames_columns_and_keeps_data(tmp_path):
    path = tmp_path / "devbox.db"
    old = old_database(path)
    assert "hetzner_id" in [r["name"] for r in old.execute("PRAGMA table_info(servers)")]
    old.execute("INSERT INTO projects (name, path, created_at, updated_at) VALUES ('crm', '/c', 't', 't')")
    old.execute(
        "INSERT INTO snapshots (project, hetzner_image_id, server_type, status, created_at) "
        "VALUES ('crm', 777, 'cx23', 'available', '2026-01-01T00:00:00+00:00')"
    )
    old.execute(
        "INSERT INTO servers (project, hetzner_id, ip, server_type, location, status, created_at, snapshot_id) "
        "VALUES ('crm', 4242, '192.0.2.5', 'cx23', 'fsn1', 'running', '2026-01-01T00:00:00+00:00', 1)"
    )
    old.close()

    conn = db.connect(path)  # migrates in place, no manual step
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    server_cols = {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}
    snap_cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    assert {"provider_id", "provider", "host"} <= server_cols and "hetzner_id" not in server_cols
    assert {"image_id", "provider"} <= snap_cols and "hetzner_image_id" not in snap_cols

    server = db.active_server(conn, "crm")
    assert server["provider_id"] == 4242 and server["provider"] == "hetzner" and server["host"] is None
    assert server["snapshot_id"] == 1 and server["ip"] == "192.0.2.5"
    snap = db.get_snapshot_by_image(conn, 777)
    assert snap["provider"] == "hetzner" and snap["image_id"] == 777
    assert db.row_dict(server)["hetzner_id"] == 4242
    assert db.row_dict(snap)["hetzner_image_id"] == 777


def test_migrated_database_still_enforces_unique_image_and_one_active_server(tmp_path):
    path = tmp_path / "devbox.db"
    old = old_database(path)
    old.execute("INSERT INTO projects (name, path, created_at, updated_at) VALUES ('crm', '/c', 't', 't')")
    old.close()
    conn = db.connect(path)
    db.insert_snapshot(conn, "crm", 5)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_snapshot(conn, "crm", 5)
    db.insert_server(conn, "crm", 1, "running")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_server(conn, "crm", 2, "running")


def test_reconcile_only_touches_servers_of_its_provider(tmp_path):
    conn = db.connect(tmp_path / "devbox.db")
    db.add_project(conn, "crm", "/c")
    db.insert_server(conn, "crm", 10, "running", provider="other", host="box")
    assert db.reconcile(conn, [], "hetzner") == []
    assert db.active_server(conn, "crm")["status"] == "running"
    db.reconcile(conn, [], "other")
    assert db.active_server(conn, "crm") is None
