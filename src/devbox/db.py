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
    """
    ALTER TABLE projects ADD COLUMN removed_at TEXT;
    ALTER TABLE servers ADD COLUMN price_hourly REAL;
    ALTER TABLE servers ADD COLUMN price_monthly REAL;

    CREATE TABLE events (
      id          INTEGER PRIMARY KEY,
      project     TEXT NOT NULL REFERENCES projects(name),
      server_id   INTEGER REFERENCES servers(id),
      kind        TEXT NOT NULL,
      at          TEXT NOT NULL
    );

    CREATE INDEX events_server ON events(server_id);
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
    existing = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if existing and existing["removed_at"] is None:
        raise DevboxError(f"project '{name}' already exists")
    ts = now()
    if existing:
        # A removed project keeps its cost history; adding it again revives it.
        conn.execute(
            "UPDATE projects SET path = ?, notes = '', removed_at = NULL, updated_at = ? WHERE name = ?",
            (path, ts, name),
        )
        return
    conn.execute(
        "INSERT INTO projects (name, path, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, path, ts, ts),
    )


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM projects WHERE name = ? AND removed_at IS NULL", (name,)
    ).fetchone()


def require_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    project = get_project(conn, name)
    if project is None:
        raise DevboxError(f"unknown project '{name}' (add it with `devbox project add`)")
    return project


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects WHERE removed_at IS NULL ORDER BY name"
    ).fetchall()


def set_notes(conn: sqlite3.Connection, name: str, notes: str) -> None:
    require_project(conn, name)
    conn.execute(
        "UPDATE projects SET notes = ?, updated_at = ? WHERE name = ?", (notes, now(), name)
    )


def remove_project(conn: sqlite3.Connection, name: str) -> None:
    """Hide a project. Its servers and events stay for cost history."""
    require_project(conn, name)
    if active_server(conn, name):
        raise DevboxError(f"project '{name}' has a server; run `devbox down {name}` first")
    conn.execute(
        "UPDATE projects SET removed_at = ?, updated_at = ? WHERE name = ?", (now(), now(), name)
    )


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
    price_hourly: float | None = None,
    price_monthly: float | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO servers
           (project, hetzner_id, ip, server_type, location, status, created_at,
            price_hourly, price_monthly)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project, hetzner_id, ip, server_type, location, status, created_at or now(),
         price_hourly, price_monthly),
    )
    return cur.lastrowid


def get_server(conn: sqlite3.Connection, server_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def all_servers(conn: sqlite3.Connection, project: str | None = None) -> list[sqlite3.Row]:
    if project:
        return conn.execute(
            "SELECT * FROM servers WHERE project = ? ORDER BY created_at", (project,)
        ).fetchall()
    return conn.execute("SELECT * FROM servers ORDER BY created_at").fetchall()


def servers_missing_price(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM servers WHERE price_hourly IS NULL AND server_type IS NOT NULL"
    ).fetchall()


# Events (activity counts for stats)


def add_event(conn: sqlite3.Connection, project: str, server_id: int | None, kind: str) -> None:
    conn.execute(
        "INSERT INTO events (project, server_id, kind, at) VALUES (?, ?, ?, ?)",
        (project, server_id, kind, now()),
    )


def event_counts(conn: sqlite3.Connection, server_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM events WHERE server_id = ? GROUP BY kind", (server_id,)
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


_SERVER_FIELDS = {
    "ip", "server_type", "location", "status", "preview_port", "preview_url",
    "price_hourly", "price_monthly",
}


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
