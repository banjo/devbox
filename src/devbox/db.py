import json
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
    """
    CREATE TABLE snapshots (
      id                  INTEGER PRIMARY KEY,
      project             TEXT NOT NULL REFERENCES projects(name),
      hetzner_image_id    INTEGER NOT NULL UNIQUE,
      server_id           INTEGER REFERENCES servers(id),
      server_type         TEXT,
      location            TEXT,
      architecture        TEXT,
      size_gb             REAL,
      disk_gb             INTEGER,
      price_per_gb_month  REAL,
      status              TEXT NOT NULL CHECK (status IN ('available', 'deleted')),
      created_at          TEXT NOT NULL,
      deleted_at          TEXT
    );

    ALTER TABLE servers ADD COLUMN snapshot_id INTEGER REFERENCES snapshots(id);
    ALTER TABLE projects ADD COLUMN idle_timeout TEXT;
    """,
    """
    CREATE TABLE databases (
      project     TEXT NOT NULL REFERENCES projects(name),
      name        TEXT NOT NULL,
      engine      TEXT NOT NULL,
      settings    TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (project, name)
    );
    """,
    """
    ALTER TABLE servers ADD COLUMN power TEXT;
    """,
    """
    ALTER TABLE servers ADD COLUMN setup_stage TEXT;
    ALTER TABLE servers ADD COLUMN setup_error TEXT;
    ALTER TABLE servers ADD COLUMN diagnostics TEXT;
    ALTER TABLE projects ADD COLUMN startup_timeout TEXT;
    """,
    """
    ALTER TABLE servers ADD COLUMN stop_action TEXT;
    ALTER TABLE servers ADD COLUMN stop_requested_at TEXT;
    """,
    """
    CREATE TABLE previews (
      server_id   INTEGER NOT NULL REFERENCES servers(id),
      name        TEXT NOT NULL,
      port        INTEGER NOT NULL,
      kind        TEXT NOT NULL CHECK (kind IN ('quick', 'named')),
      url         TEXT NOT NULL,
      hostname    TEXT,
      unit        TEXT,
      protected   INTEGER NOT NULL DEFAULT 0,
      proxy_port  INTEGER,
      created_at  TEXT NOT NULL,
      PRIMARY KEY (server_id, name)
    );

    INSERT INTO previews (server_id, name, port, kind, url, unit, created_at)
      SELECT id, 'default', preview_port, 'quick', preview_url, 'tunnel', created_at FROM servers
      WHERE preview_url IS NOT NULL AND preview_port IS NOT NULL AND status != 'deleted';
    """,
    """
    ALTER TABLE servers ADD COLUMN host_key TEXT;
    """,
    """
    ALTER TABLE servers ADD COLUMN startup TEXT;
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


def set_idle_timeout(conn: sqlite3.Connection, name: str, value: str | None) -> None:
    require_project(conn, name)
    conn.execute(
        "UPDATE projects SET idle_timeout = ?, updated_at = ? WHERE name = ?", (value, now(), name)
    )


def set_startup_timeout(conn: sqlite3.Connection, name: str, value: str | None) -> None:
    require_project(conn, name)
    conn.execute(
        "UPDATE projects SET startup_timeout = ?, updated_at = ? WHERE name = ?", (value, now(), name)
    )


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
    if snapshots(conn, name):
        raise DevboxError(
            f"project '{name}' has snapshots that still cost money; "
            f"delete them with `devbox snapshot delete` first"
        )
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
    snapshot_id: int | None = None,
    power: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO servers
           (project, hetzner_id, ip, server_type, location, status, created_at,
            price_hourly, price_monthly, snapshot_id, power)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project, hetzner_id, ip, server_type, location, status, created_at or now(),
         price_hourly, price_monthly, snapshot_id, power),
    )
    return cur.lastrowid


def last_server(conn: sqlite3.Connection, project: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM servers WHERE project = ? ORDER BY created_at DESC, id DESC LIMIT 1", (project,)
    ).fetchone()


def last_activity(conn: sqlite3.Connection, server) -> datetime:
    row = conn.execute("SELECT MAX(at) AS at FROM events WHERE server_id = ?", (server["id"],)).fetchone()
    stamps = [server["created_at"], row["at"]]
    return max(_parse(s) for s in stamps if s)


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Snapshots


def insert_snapshot(
    conn: sqlite3.Connection,
    project: str,
    hetzner_image_id: int,
    server_id: int | None = None,
    server_type: str | None = None,
    location: str | None = None,
    architecture: str | None = None,
    size_gb: float | None = None,
    disk_gb: int | None = None,
    price_per_gb_month: float | None = None,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO snapshots
           (project, hetzner_image_id, server_id, server_type, location, architecture,
            size_gb, disk_gb, price_per_gb_month, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)""",
        (project, hetzner_image_id, server_id, server_type, location, architecture,
         size_gb, disk_gb, price_per_gb_month, created_at or now()),
    )
    return cur.lastrowid


def snapshots(conn: sqlite3.Connection, project: str | None = None, include_deleted: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM snapshots WHERE 1 = 1"
    args: list = []
    if project:
        sql += " AND project = ?"
        args.append(project)
    if not include_deleted:
        sql += " AND status = 'available'"
    sql += " ORDER BY created_at DESC, id DESC"
    return conn.execute(sql, args).fetchall()


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def latest_snapshot(conn: sqlite3.Connection, project: str) -> sqlite3.Row | None:
    found = snapshots(conn, project)
    return found[0] if found else None


def get_snapshot_by_image(conn: sqlite3.Connection, image_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snapshots WHERE hetzner_image_id = ?", (image_id,)).fetchone()


def mark_snapshot_deleted(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute(
        "UPDATE snapshots SET status = 'deleted', deleted_at = ? WHERE id = ?", (now(), snapshot_id)
    )


def update_snapshot(conn: sqlite3.Connection, snapshot_id: int, **fields) -> None:
    allowed = {"size_gb", "disk_gb", "architecture", "price_per_gb_month"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown snapshot fields: {unknown}")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE snapshots SET {sets} WHERE id = ?", (*fields.values(), snapshot_id))


def reconcile_snapshots(conn: sqlite3.Connection, images: list[dict]) -> list[dict]:
    """Match registry snapshots with labeled Hetzner snapshots. Returns unknown ones."""
    by_id = {i["id"]: i for i in images}
    for snap in snapshots(conn):
        live = by_id.get(snap["hetzner_image_id"])
        if live is None:
            mark_snapshot_deleted(conn, snap["id"])
        elif live["size_gb"] is not None and live["size_gb"] != snap["size_gb"]:
            update_snapshot(conn, snap["id"], size_gb=live["size_gb"])

    unknown = []
    for image in images:
        if get_snapshot_by_image(conn, image["id"]):
            continue
        project = image.get("project")
        if project and get_project(conn, project) and image["status"] == "available":
            # For example a snapshot created just before a crash during `pause`.
            insert_snapshot(
                conn, project, image["id"], architecture=image["architecture"],
                size_gb=image["size_gb"], disk_gb=image["disk_gb"], created_at=image.get("created"),
            )
        else:
            unknown.append(image)
    return unknown


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


def has_events(conn: sqlite3.Connection, server_id: int, kinds) -> bool:
    kinds = sorted(kinds)
    return conn.execute(
        f"SELECT 1 FROM events WHERE server_id = ? AND kind IN ({', '.join('?' * len(kinds))}) LIMIT 1",
        (server_id, *kinds),
    ).fetchone() is not None


def reuses(conn: sqlite3.Connection, project: str | None, since: datetime | None,
           until: datetime | None) -> list[sqlite3.Row]:
    """`reuse` events (a paid hour used instead of a new server), with the server's hourly price."""
    sql = ("SELECT e.project, e.at, s.price_hourly FROM events e JOIN servers s ON s.id = e.server_id "
           "WHERE e.kind = 'reuse'")
    args: list = []
    if project:
        sql += " AND e.project = ?"
        args.append(project)
    rows = conn.execute(sql, args).fetchall()
    return [r for r in rows if (since is None or _parse(r["at"]) >= since) and (until is None or _parse(r["at"]) < until)]


def event_counts(conn: sqlite3.Connection, server_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM events WHERE server_id = ? GROUP BY kind", (server_id,)
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


_SERVER_FIELDS = {
    "ip", "server_type", "location", "status", "preview_port", "preview_url",
    "price_hourly", "price_monthly", "power", "setup_stage", "setup_error", "diagnostics",
    "stop_action", "stop_requested_at", "created_at", "host_key", "startup",
}


def update_server(conn: sqlite3.Connection, server_id: int, **fields) -> None:
    unknown = set(fields) - _SERVER_FIELDS
    if unknown:
        raise ValueError(f"unknown server fields: {unknown}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE servers SET {sets} WHERE id = ?", (*fields.values(), server_id))


def request_stop(conn: sqlite3.Connection, server_id: int, action: str) -> None:
    """Stop the server at the end of its billing hour (`pause/down --at-hour-end`)."""
    conn.execute(
        "UPDATE servers SET stop_action = ?, stop_requested_at = ? WHERE id = ?", (action, now(), server_id)
    )


def cancel_stop(conn: sqlite3.Connection, server_id: int) -> bool:
    return conn.execute(
        "UPDATE servers SET stop_action = NULL, stop_requested_at = NULL WHERE id = ? AND stop_action IS NOT NULL",
        (server_id,),
    ).rowcount > 0


# Previews


def previews(conn: sqlite3.Connection, server_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM previews WHERE server_id = ? ORDER BY name = 'default' DESC, name", (server_id,)
    ).fetchall()


def set_preview(conn: sqlite3.Connection, server_id: int, name: str, **fields) -> None:
    conn.execute("DELETE FROM previews WHERE server_id = ? AND name = ?", (server_id, name))
    columns = ["server_id", "name", "created_at", *fields]
    conn.execute(
        f"INSERT INTO previews ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        (server_id, name, now(), *fields.values()),
    )
    _sync_preview_columns(conn, server_id)


def remove_previews(conn: sqlite3.Connection, server_id: int, names: list[str]) -> None:
    for name in names:
        conn.execute("DELETE FROM previews WHERE server_id = ? AND name = ?", (server_id, name))
    _sync_preview_columns(conn, server_id)


def _sync_preview_columns(conn: sqlite3.Connection, server_id: int) -> None:
    """servers.preview_url/preview_port mirror the first preview (for older JSON readers)."""
    first = next(iter(previews(conn, server_id)), None)
    conn.execute(
        "UPDATE servers SET preview_url = ?, preview_port = ? WHERE id = ?",
        (first["url"] if first else None, first["port"] if first else None, server_id),
    )


def mark_deleted(conn: sqlite3.Connection, server_id: int) -> None:
    conn.execute(
        "UPDATE servers SET status = 'deleted', deleted_at = ? WHERE id = ?", (now(), server_id)
    )
    conn.execute("DELETE FROM previews WHERE server_id = ?", (server_id,))


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
            continue
        changes = {}
        if live["ip"] != row["ip"]:
            changes["ip"] = live["ip"]
        if live.get("status") and live["status"] != row["power"]:
            changes["power"] = live["status"]
        # Hetzner's creation time is what billing hours count from.
        if live.get("created") and _parse(live["created"]) != _parse(row["created_at"]):
            changes["created_at"] = _parse(live["created"]).isoformat(timespec="seconds")
        update_server(conn, row["id"], **changes)

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
                power=s.get("status"),
            )
        else:
            orphans.append(s)
    return orphans


# Databases (for `devbox db dump/restore`)


def add_database(conn: sqlite3.Connection, project: str, name: str, engine: str, settings: dict) -> None:
    require_project(conn, project)
    if not NAME_RE.match(name):
        raise DevboxError(f"invalid database name '{name}': use lowercase letters, digits and dashes")
    if get_database(conn, project, name):
        raise DevboxError(f"{project} already has a database '{name}'; remove it first")
    conn.execute(
        "INSERT INTO databases (project, name, engine, settings, created_at) VALUES (?, ?, ?, ?, ?)",
        (project, name, engine, json.dumps(settings, sort_keys=True), now()),
    )


def get_database(conn: sqlite3.Connection, project: str, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM databases WHERE project = ? AND name = ?", (project, name)
    ).fetchone()
    return _database(row) if row else None


def list_databases(conn: sqlite3.Connection, project: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM databases WHERE project = ? ORDER BY name", (project,)).fetchall()
    return [_database(r) for r in rows]


def remove_database(conn: sqlite3.Connection, project: str, name: str) -> bool:
    return conn.execute(
        "DELETE FROM databases WHERE project = ? AND name = ?", (project, name)
    ).rowcount > 0


def _database(row) -> dict:
    return {**dict(row), "settings": json.loads(row["settings"])}
