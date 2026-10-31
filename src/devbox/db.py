import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from devbox import DevboxError

MIGRATIONS = [
    """
    CREATE TABLE projects (
      name        TEXT PRIMARY KEY,
      path        TEXT NOT NULL,
      notes       TEXT NOT NULL DEFAULT '',
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    );

    CREATE TABLE servers (
      id           INTEGER PRIMARY KEY,
      project      TEXT NOT NULL REFERENCES projects(name),
      hetzner_id   INTEGER NOT NULL,
      ip           TEXT,
      server_type  TEXT,
      location     TEXT,
      status       TEXT NOT NULL CHECK (status IN ('creating', 'running', 'deleted')),
      preview_port INTEGER,
      preview_url  TEXT,
      created_at   TEXT NOT NULL,
      deleted_at   TEXT
    );

    CREATE UNIQUE INDEX servers_one_active_per_project
      ON servers(project) WHERE status != 'deleted';
    """,
]

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        try:
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


# Projects


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise DevboxError(
            f"invalid project name '{name}': use lowercase letters, digits and dashes"
        )


def add_project(conn: sqlite3.Connection, name: str, path: str) -> None:
    validate_name(name)
    if get_project(conn, name):
        raise DevboxError(f"project '{name}' already exists")
    ts = now()
    conn.execute(
        "INSERT INTO projects (name, path, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, path, ts, ts),
    )


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def require_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    project = get_project(conn, name)
    if project is None:
        raise DevboxError(f"unknown project '{name}' (add it with `devbox project add`)")
    return project


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY name").fetchall()


def set_notes(conn: sqlite3.Connection, name: str, notes: str) -> None:
    require_project(conn, name)
    conn.execute(
        "UPDATE projects SET notes = ?, updated_at = ? WHERE name = ?", (notes, now(), name)
    )


def remove_project(conn: sqlite3.Connection, name: str) -> None:
    require_project(conn, name)
    if active_server(conn, name):
        raise DevboxError(f"project '{name}' has a server; run `devbox down {name}` first")
    conn.execute("BEGIN")
    conn.execute("DELETE FROM servers WHERE project = ?", (name,))
    conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    conn.execute("COMMIT")


# Servers


def active_server(conn: sqlite3.Connection, project: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM servers WHERE project = ? AND status != 'deleted'", (project,)
    ).fetchone()


def active_servers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM servers WHERE status != 'deleted' ORDER BY project"
    ).fetchall()


def insert_server(
    conn: sqlite3.Connection,
    project: str,
    hetzner_id: int,
    status: str,
    ip: str | None = None,
    server_type: str | None = None,
    location: str | None = None,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO servers
           (project, hetzner_id, ip, server_type, location, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project, hetzner_id, ip, server_type, location, status, created_at or now()),
    )
    return cur.lastrowid


_SERVER_FIELDS = {"ip", "server_type", "location", "status", "preview_port", "preview_url"}


def update_server(conn: sqlite3.Connection, server_id: int, **fields) -> None:
    unknown = set(fields) - _SERVER_FIELDS
    if unknown:
        raise ValueError(f"unknown server fields: {unknown}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE servers SET {sets} WHERE id = ?", (*fields.values(), server_id))


def mark_deleted(conn: sqlite3.Connection, server_id: int) -> None:
    conn.execute(
        "UPDATE servers SET status = 'deleted', deleted_at = ? WHERE id = ?", (now(), server_id)
    )


def reconcile(conn: sqlite3.Connection, hetzner_servers: list[dict]) -> list[dict]:
    """Bring the registry in line with the labeled servers that exist in Hetzner.

    `hetzner_servers` are dicts from `hetzner.server_info`. Returns labeled servers
    whose project is not registered, so the caller can report them.
    """
    by_id = {s["id"]: s for s in hetzner_servers}

    for row in active_servers(conn):
        live = by_id.get(row["hetzner_id"])
        if live is None:
            mark_deleted(conn, row["id"])
        elif live["ip"] != row["ip"]:
            update_server(conn, row["id"], ip=live["ip"])

    known_ids = {row["hetzner_id"] for row in active_servers(conn)}
    orphans = []
    for s in hetzner_servers:
        if s["id"] in known_ids:
            continue
        project = s.get("project")
        if project and get_project(conn, project) and not active_server(conn, project):
            insert_server(
                conn,
                project,
                s["id"],
                "running" if s["status"] == "running" else "creating",
                ip=s["ip"],
                server_type=s["server_type"],
                location=s["location"],
                created_at=s.get("created"),
            )
        else:
            orphans.append(s)
    return orphans
