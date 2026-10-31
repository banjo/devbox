import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from devbox import DevboxError, cli, databases, envsecrets, watch
from devbox import remote as remote_mod
from devbox.remote import Remote, git_ignored

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)


# Secrets


def test_parse_env_handles_comments_export_and_quotes():
    text = '# comment\nexport A=1\nB="two words"\nC=\'x=y\'\n\nD=\n'
    assert envsecrets.parse_env(text) == {"A": "1", "B": "two words", "C": "x=y", "D": ""}


def test_merge_env_updates_keys_and_keeps_other_lines():
    existing = "# db\nDATABASE_URL=old\nexport TOKEN=a\nPORT=3000\n"
    merged = envsecrets.merge_env(existing, {"DATABASE_URL": "postgres://u:p@db/app", "TOKEN": "b c", "NEW": "1"})
    assert merged == (
        "# db\nDATABASE_URL=postgres://u:p@db/app\nexport TOKEN=\"b c\"\nPORT=3000\nNEW=1\n"
    )
    assert envsecrets.parse_env(merged)["TOKEN"] == "b c"


@pytest.mark.parametrize("path", ["/etc/passwd", "../x/.env", "a/../../.env", ""])
def test_env_file_must_be_inside_project(path):
    with pytest.raises(DevboxError):
        envsecrets.validate_file(path)


@pytest.mark.parametrize("bad", ["NOEQUALS", "1A=x", "A-B=x", "=x"])
def test_invalid_assignments(bad):
    with pytest.raises(DevboxError):
        envsecrets.parse_assignment(bad)


def test_store_is_private_and_lists_only_keys(tmp_path):
    store = envsecrets.SecretStore(tmp_path, "app")
    store.set(".env", {"A": "secret-a"})
    store.set("apps/api/.env", {"B": "secret-b"})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.summary() == {".env": ["A"], "apps/api/.env": ["B"]}
    assert store.unset(".env", ["A", "MISSING"]) == ["A"]
    assert store.summary() == {"apps/api/.env": ["B"]}
    store.unset("apps/api/.env", ["B"])
    assert not store.path.exists()


class FakeFiles:
    """Stands in for files on the server."""

    def __init__(self):
        self.files: dict[str, str] = {}

    def install(self, monkeypatch):
        monkeypatch.setattr(cli.Remote, "read_file", lambda r, path: self.files.get(path))
        monkeypatch.setattr(cli.Remote, "write_file", lambda r, path, content, mode="600": self.files.__setitem__(path, content))


def test_secrets_cli_stores_pushes_and_never_prints_values(capsys, isolated, cloud, monkeypatch):
    server_files = FakeFiles()
    server_files.install(monkeypatch)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))

    code, out, _ = run(capsys, "secrets", "set", "crm", "API_KEY=sk-123", "--no-push")
    assert code == 0 and "API_KEY" in out and "sk-123" not in out

    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    assert server_files.files[".env"] == "API_KEY=sk-123\n"

    server_files.files["apps/api/.env"] = "PORT=3000\nDB=old\n"
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("DB=postgres://x\n# c\nOTHER='a b'\n"))
    code, out, _ = run(capsys, "secrets", "set", "crm", "--file", "apps/api/.env", "--from-file", "-")
    assert "written to the server" in out and "postgres://x" not in out
    assert server_files.files["apps/api/.env"] == 'PORT=3000\nDB=postgres://x\nOTHER="a b"\n'

    code, out, _ = run(capsys, "secrets", "list", "crm", "--json")
    assert json.loads(out)["files"] == {".env": ["API_KEY"], "apps/api/.env": ["DB", "OTHER"]}
    assert "sk-123" not in out

    code, out, _ = run(capsys, "secrets", "unset", "crm", "API_KEY")
    assert "removed API_KEY" in out


# Databases


def test_engines_validate_options():
    pg = databases.get_engine("postgres")
    assert pg.validate({"database": "app", "service": "db"}) == {"database": "app", "service": "db", "user": "postgres"}
    with pytest.raises(DevboxError, match="needs --option database"):
        pg.validate({})
    with pytest.raises(DevboxError, match="unknown option"):
        pg.validate({"database": "app", "nope": "x"})
    with pytest.raises(DevboxError, match="invalid postgres database"):
        pg.validate({"database": "app; rm -rf /"})
    with pytest.raises(DevboxError, match="invalid sqlite path"):
        databases.get_engine("sqlite").validate({"path": "../outside.db"})
    with pytest.raises(DevboxError, match="unknown database engine"):
        databases.get_engine("mysql")


def test_postgres_commands():
    pg = databases.get_engine("postgres")
    in_compose = pg.validate({"database": "app", "service": "db"})
    assert pg.dump_command(in_compose) == "docker compose exec -T db pg_dump -U postgres -Fc --no-owner app"
    assert pg.restore_command(in_compose).startswith("docker compose exec -T db pg_restore -U postgres --clean")
    direct = pg.validate({"database": "app", "host": "localhost", "port": "5433"})
    assert pg.dump_command(direct) == "pg_dump -U postgres -h localhost -p 5433 -Fc --no-owner app"


def test_sqlite_dump_and_restore_round_trip(tmp_path):
    """Runs the exact remote commands locally against a real SQLite file."""
    import sqlite3

    engine = databases.get_engine("sqlite")
    settings = engine.validate({"path": "data/app.db"})
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data/app.db")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES ('kept')")
    conn.commit()

    dump = subprocess.run(["bash", "-c", engine.dump_command(settings)], cwd=tmp_path, capture_output=True, check=True).stdout
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t VALUES ('changed')")
    conn.commit()
    conn.close()

    subprocess.run(["bash", "-c", engine.restore_command(settings)], cwd=tmp_path, input=dump, check=True)
    assert sqlite3.connect(tmp_path / "data/app.db").execute("SELECT x FROM t").fetchall() == [("kept",)]

    bad = subprocess.run(["bash", "-c", engine.restore_command(settings)], cwd=tmp_path, input=b"not a db" * 100, capture_output=True)
    assert bad.returncode != 0
    assert sqlite3.connect(tmp_path / "data/app.db").execute("SELECT x FROM t").fetchall() == [("kept",)]


def test_db_cli_add_dump_restore(capsys, isolated, cloud, monkeypatch):
    streamed = {}

    def stream_out(r, script, dest):
        streamed["dump"] = script
        dest.write_bytes(b"DUMP")
        return 4

    def stream_in(r, script, src):
        streamed["restore"] = (script, Path(src).read_bytes())

    monkeypatch.setattr(cli.Remote, "stream_out", stream_out)
    monkeypatch.setattr(cli.Remote, "stream_in", stream_in)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))

    code, _, err = run(capsys, "db", "add", "crm", "main", "--engine", "postgres", "--option", "service=db")
    assert code == 1 and "needs --option database" in err
    code, out, _ = run(capsys, "db", "add", "crm", "main", "--engine", "postgres", "--option", "service=db", "--option", "database=app")
    assert code == 0

    code, _, err = run(capsys, "db", "dump", "crm")
    assert code == 1 and "has no server" in err

    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    code, out, _ = run(capsys, "db", "dump", "crm", "--json")
    dump = json.loads(out)
    assert dump["database"] == "main" and Path(dump["path"]).read_bytes() == b"DUMP"
    assert dump["path"].endswith(".pgdump")
    assert "pg_dump" in streamed["dump"]

    code, _, err = run(capsys, "db", "restore", "crm")
    assert code == 1 and "--yes" in err and "restore" not in streamed
    code, out, _ = run(capsys, "db", "restore", "crm", "--yes")
    assert code == 0 and streamed["restore"][1] == b"DUMP" and "pg_restore" in streamed["restore"][0]

    code, out, _ = run(capsys, "db", "list", "crm")
    assert "main: postgres" in out and "latest dump main-" in out

    code, out, _ = run(capsys, "down", "crm", "--dump")
    assert code == 0 and "dumped main" in out
    assert cloud.servers == {}


def test_db_engines_lists_options(capsys):
    code, out, _ = run(capsys, "db", "engines", "--json")
    engines = {e["engine"]: e for e in json.loads(out)}
    assert set(engines) == {"postgres", "sqlite"}
    assert any(o["name"] == "database" and o["required"] for o in engines["postgres"]["options"])


# Watch


def test_watch_syncs_once_per_burst():
    states = iter([{"a": (1, 1)}, {"a": (1, 1)}, {"a": (2, 1)}, {"a": (3, 1)}, {"a": (3, 1)}, {"a": (3, 1)}, {"a": (3, 1), "b": (1, 1)}, {"a": (3, 1), "b": (1, 1)}])
    calls = []
    watch.watch(lambda: next(states), calls.append, sleep=lambda s: None, rounds=4)
    assert calls == [["a"], ["b"]]


def test_snapshot_follows_git_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "ignored.txt").write_text("x")
    snap = watch.snapshot(tmp_path)
    assert set(snap) == {".gitignore", "a.txt"}


# Pull


def test_git_ignored(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("dist/\n*.log\n")
    assert git_ignored(tmp_path, ["dist/x.js", "src/a.ts", "x.log", "new/migration.sql"]) == {"dist/x.js", "x.log"}
    assert git_ignored(tmp_path / "missing", ["a"]) is None


def test_remote_pull_copies_only_differing_files(tmp_path, monkeypatch):
    """Runs the real rsync locally, with the server folder replaced by a local one."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    server, local, staging = tmp_path / "server", tmp_path / "local", tmp_path / "staging"
    for root in (server, local):
        (root / "src").mkdir(parents=True)
        (root / "src/same.ts").write_text("same")
        (root / "package-lock.json").write_text("old" if root == local else "new")
    (server / "src/migrations").mkdir()
    (server / "src/migrations/001.sql").write_text("create table")

    real_run = subprocess.run

    def local_rsync(cmd, **kw):
        cmd = [c for c in cmd]
        i = cmd.index("-e")
        del cmd[i:i + 2]
        cmd = [f"{server}/" if c.startswith("root@") else c for c in cmd]
        return real_run(cmd, **kw)

    monkeypatch.setattr(remote_mod.subprocess, "run", local_rsync)
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    got = r.pull(["src/same.ts", "package-lock.json", "src/migrations/001.sql"], staging, local)
    assert got == ["package-lock.json", "src/migrations/001.sql"]
    assert not (staging / "src/same.ts").exists()


def test_pull_cli_reports_changes_and_missing_files(capsys, isolated, cloud, monkeypatch):
    root = isolated / "crm"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("dist/\n")
    (root / "kept.ts").write_text("x")
    (root / "gone.ts").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    run(capsys, "project", "add", "crm", "--path", str(root))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")

    seen = {}
    monkeypatch.setattr(cli.Remote, "list_files", lambda r: [".gitignore", "kept.ts", "dist/bundle.js", "new.sql"])

    def fake_pull(r, files, staging, compare):
        seen["files"] = files
        staging.mkdir(parents=True)
        (staging / "new.sql").write_text("y")
        (staging / "kept.ts").write_text("changed")
        return ["kept.ts", "new.sql"]

    monkeypatch.setattr(cli.Remote, "pull", fake_pull)
    code, out, _ = run(capsys, "pull", "crm", "--json")
    data = json.loads(out)
    assert seen["files"] == [".gitignore", "kept.ts", "new.sql"]  # dist/ is ignored
    assert data["files"] == [{"path": "kept.ts", "change": "changed"}, {"path": "new.sql", "change": "new"}]
    assert data["missing_on_server"] == ["gone.ts"]
    assert Path(data["staging"]).is_dir()

    run(capsys, "pull", "crm", "dist")
    assert seen["files"] == ["dist/bundle.js"]


# Forward


@pytest.mark.parametrize("spec, expected", [("5432", (5432, 5432)), ("15432:5432", (15432, 5432))])
def test_port_specs(spec, expected):
    assert cli.parse_port_spec(spec) == expected


@pytest.mark.parametrize("spec", ["x", "0", "70000", "1:2:3"])
def test_bad_port_specs(spec):
    with pytest.raises(DevboxError):
        cli.parse_port_spec(spec)


def test_forward_command_binds_localhost_only():
    cmd = Remote("192.0.2.1", Path("/key"), Path("/kh")).forward_cmd([(15432, 5432), (6379, 6379)])
    assert cmd[-1] == "root@192.0.2.1"
    assert "-N" in cmd and "ExitOnForwardFailure=yes" in cmd
    assert "127.0.0.1:15432:localhost:5432" in cmd and "127.0.0.1:6379:localhost:6379" in cmd
