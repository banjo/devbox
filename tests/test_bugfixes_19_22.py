"""Regression tests for issues #19-#22."""

import json
import os
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from devbox import DevboxError, cli, costs, databases, envsecrets
from devbox import remote as remote_mod
from devbox.remote import Remote

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)

SQLITE = databases.get_engine("sqlite")


def sh(cmd: str, cwd: Path, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, input=data, capture_output=True)


def make_db(path: Path, rows: int = 42) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def count(path: Path) -> int:
    return sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone()[0]


# #19 SQLite dump/restore


def test_dump_of_missing_database_fails_and_creates_nothing(tmp_path):
    (tmp_path / "data").mkdir()
    settings = SQLITE.validate({"path": "data/app.db"})
    proc = sh(SQLITE.dump_command(settings), tmp_path)
    assert proc.returncode != 0
    assert b"no database at data/app.db" in proc.stderr and b"Traceback" not in proc.stderr
    assert proc.stdout == b""
    assert not (tmp_path / "data/app.db").exists()


def test_restore_creates_missing_folder(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir(); target.mkdir()
    make_db(source / "data/app.db")
    settings = SQLITE.validate({"path": "data/app.db"})
    dump = sh(SQLITE.dump_command(settings), source).stdout
    proc = sh(SQLITE.restore_command(settings), target, dump)
    assert proc.returncode == 0, proc.stderr
    assert count(target / "data/app.db") == 42
    assert [p.name for p in (target / "data").iterdir()] == ["app.db"]


@pytest.mark.parametrize("payload, message", [
    (b"", b"the dump is empty"),
    (b"not a database" * 50, b"not a SQLite database"),
])
def test_bad_restore_input_keeps_data_and_cleans_up(tmp_path, payload, message):
    make_db(tmp_path / "data/app.db")
    settings = SQLITE.validate({"path": "data/app.db"})
    proc = sh(SQLITE.restore_command(settings), tmp_path, payload)
    assert proc.returncode != 0 and message in proc.stderr and b"Traceback" not in proc.stderr
    assert count(tmp_path / "data/app.db") == 42
    assert [p.name for p in (tmp_path / "data").iterdir()] == ["app.db"]  # no leftover temp file


def test_truncated_dump_is_rejected(tmp_path):
    make_db(tmp_path / "src/data/app.db", rows=5000)
    settings = SQLITE.validate({"path": "data/app.db"})
    dump = sh(SQLITE.dump_command(settings), tmp_path / "src").stdout
    assert len(dump) > 8192
    make_db(tmp_path / "dst/data/app.db")
    proc = sh(SQLITE.restore_command(settings), tmp_path / "dst", dump[:4096])
    assert proc.returncode != 0 and b"nothing was restored" in proc.stderr
    assert count(tmp_path / "dst/data/app.db") == 42


def test_legitimately_empty_database_still_restores(tmp_path):
    sqlite3.connect(tmp_path / "empty.db").execute("CREATE TABLE t (x)").connection.commit()
    (tmp_path / "srv").mkdir()
    (tmp_path / "srv/empty.db").write_bytes((tmp_path / "empty.db").read_bytes())
    settings = SQLITE.validate({"path": "empty.db"})
    dump = sh(SQLITE.dump_command(settings), tmp_path / "srv").stdout
    make_db(tmp_path / "dst/empty.db")
    proc = sh(SQLITE.restore_command(settings), tmp_path / "dst", dump)
    assert proc.returncode == 0
    assert count(tmp_path / "dst/empty.db") == 0


def test_local_dump_files_are_private(tmp_path, monkeypatch):
    old = os.umask(0o022)
    try:
        monkeypatch.setattr(
            remote_mod.subprocess, "run",
            lambda cmd, stdout=None, **kw: (stdout.write(b"DUMP"), subprocess.CompletedProcess(cmd, 0, None, b""))[1],
        )
        dest = tmp_path / "x.pgdump"
        (tmp_path / "x.pgdump.partial").write_bytes(b"stale")
        os.chmod(tmp_path / "x.pgdump.partial", 0o644)
        Remote("192.0.2.1", Path("/k"), Path("/kh")).stream_out("pg_dump", dest)
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600 and dest.read_bytes() == b"DUMP"
    finally:
        os.umask(old)


def test_dump_folder_is_private(capsys, isolated, cloud, monkeypatch):
    monkeypatch.setattr(cli.Remote, "stream_out", lambda r, script, dest: dest.write_bytes(b"D") or 1)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "db", "add", "crm", "main", "--engine", "sqlite", "--option", "path=app.db")
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    code, out, _ = run(capsys, "db", "dump", "crm", "--json")
    folder = Path(json.loads(out)["path"]).parent
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(folder.parent.stat().st_mode) == 0o700


# #20 rounding


NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def item(created, deleted, hourly=0.01, monthly=5.0):
    return dict(id=1, hetzner_id=101, project="app", created_at=created, deleted_at=deleted,
                price_hourly=hourly, price_monthly=monthly, server_type="cx23", location="fsn1")


@pytest.mark.parametrize("server, by", [
    (item("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"), "day"),
    (item("2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z"), "day"),
    (item("2026-10-01T23:00:00Z", "2026-10-03T00:02:00Z", hourly=0.01, monthly=None), "day"),
    (item("2026-10-31T20:00:00Z", "2026-11-01T05:30:00Z", hourly=0.0137, monthly=4.79), "month"),
])
def test_rows_sum_exactly_to_totals(server, by):
    s = costs.summarize([server], by=by, now=NOW)
    as_json = json.loads(json.dumps(s))
    assert sum(Decimal(str(g["cost"])) for g in as_json["groups"]) == Decimal(str(as_json["total"]))
    assert sum(Decimal(str(g["billed_hours"])) for g in as_json["groups"]) == Decimal(str(as_json["billed_hours"]))


def test_billed_hours_rows_match_whole_hours():
    s = costs.summarize([item("2026-10-01T23:00:00Z", "2026-10-03T00:02:00Z", monthly=None)], by="day", now=NOW)
    assert s["billed_hours"] == 26
    assert sum(g["billed_hours"] for g in s["groups"]) == pytest.approx(26)


def test_human_cents_add_up(capsys, isolated):
    from devbox import config, db

    run(capsys, "project", "add", "app", "--path", str(isolated / "crm"))
    conn = db.connect(config.load().db_path)
    sid = db.insert_server(conn, "app", 1, "running", server_type="cx23", location="fsn1",
                           created_at="2026-10-01T00:00:00+00:00", price_hourly=0.01, price_monthly=5.0)
    conn.execute("UPDATE servers SET status = 'deleted', deleted_at = '2026-11-01T00:00:00+00:00' WHERE id = ?", (sid,))
    code, out, _ = run(capsys, "costs", "--by", "day", "--month", "2026-10")
    rows = [l for l in out.splitlines() if l.startswith("2026-10-")]
    assert len(rows) == 31
    cents = sum(Decimal(l.split("€")[-1].rstrip("*")) for l in rows)
    assert cents == Decimal("5.00") and "total 2026-10-01 to 2026-10-31: €5.00" in out


def test_apportion():
    total, rows = costs.apportion([5 / 31] * 31, 5.0, 2)
    assert total == 5.0 and sum(Decimal(str(r)) for r in rows) == Decimal("5.00")
    assert costs.apportion([], 0.0, 2) == (0.0, [])


# #21 empty filters


@pytest.mark.parametrize("flag", ["--since", "--until", "--month", "--days"])
def test_empty_period_values_are_errors(capsys, flag):
    code, out, err = run(capsys, "costs", flag, "", "--json")
    assert code == 1 and "error" in json.loads(out) and err == ""


# #22 secrets


def test_rotation_replaces_every_duplicate():
    merged = envsecrets.merge_env("TOKEN=old\nA=1\nexport TOKEN=shadow\n", {"TOKEN": "new"})
    assert merged == "TOKEN=new\nA=1\n"
    assert envsecrets.parse_env(merged)["TOKEN"] == "new"


def test_malformed_input_is_not_echoed():
    secret = "my-super-secret-value-without-equals"
    with pytest.raises(DevboxError) as e:
        envsecrets.parse_env(f"# ok\nA=1\n{secret}\n")
    assert secret not in str(e.value) and "line 3: expected KEY=VALUE" in str(e.value)
    with pytest.raises(DevboxError) as e:
        envsecrets.parse_assignment("9bad=sk-live-zzz", "argument 2")
    assert "sk-live-zzz" not in str(e.value) and "9bad" not in str(e.value) and "argument 2" in str(e.value)


def test_cli_does_not_echo_malformed_secret(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, out, err = run(capsys, "secrets", "set", "crm", "sk-live-should-not-appear", "--no-push")
    assert code == 1 and "sk-live-should-not-appear" not in out + err and "argument 1" in err


def test_stale_temp_file_cannot_loosen_permissions(tmp_path):
    store = envsecrets.SecretStore(tmp_path, "probe")
    store.path.parent.mkdir(parents=True)
    tmp = store.path.with_suffix(".tmp")
    tmp.write_text("stale")
    os.chmod(tmp, 0o644)
    store.set(".env", {"A": "1"})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not tmp.exists()
