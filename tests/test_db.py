import sqlite3

import pytest

from devbox import DevboxError, db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "devbox.db")


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "devbox.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_add_and_list_projects(conn):
    db.add_project(conn, "crm", "/code/crm")
    db.add_project(conn, "blog", "/code/blog")
    assert [p["name"] for p in db.list_projects(conn)] == ["blog", "crm"]
    with pytest.raises(DevboxError):
        db.add_project(conn, "crm", "/elsewhere")


@pytest.mark.parametrize("name", ["CRM", "-crm", "crm_app", "a" * 42, ""])
def test_rejects_bad_names(conn, name):
    with pytest.raises(DevboxError):
        db.add_project(conn, name, "/code")


def test_notes(conn):
    db.add_project(conn, "crm", "/code/crm")
    assert db.get_project(conn, "crm")["notes"] == ""
    db.set_notes(conn, "crm", "npm run dev on 3000\n")
    assert db.get_project(conn, "crm")["notes"] == "npm run dev on 3000\n"
    with pytest.raises(DevboxError):
        db.set_notes(conn, "nope", "x")


def test_one_active_server_per_project(conn):
    db.add_project(conn, "crm", "/code/crm")
    sid = db.insert_server(conn, "crm", 1, "running")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_server(conn, "crm", 2, "creating")
    db.mark_deleted(conn, sid)
    db.insert_server(conn, "crm", 2, "creating")
    assert db.active_server(conn, "crm")["hetzner_id"] == 2


def test_remove_project_with_server_fails(conn):
    db.add_project(conn, "crm", "/code/crm")
    db.insert_server(conn, "crm", 1, "running")
    with pytest.raises(DevboxError):
        db.remove_project(conn, "crm")


def test_update_server_rejects_unknown_fields(conn):
    db.add_project(conn, "crm", "/code/crm")
    sid = db.insert_server(conn, "crm", 1, "running")
    db.update_server(conn, sid, preview_port=3000, preview_url="https://x.trycloudflare.com")
    assert db.active_server(conn, "crm")["preview_port"] == 3000
    with pytest.raises(ValueError):
        db.update_server(conn, sid, project="other")
