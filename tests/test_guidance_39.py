"""Tests for issue #39: say what to do next after up, pause and down."""

import json

from devbox import cli

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_previews_6 import box, cf, expose  # noqa: F401  (fixtures)

NOTES = "New server: `npm ci`.\nEvery start: `exec --detach --name web -- 'npm run dev'`."


def up(capsys, *args):
    code, out, err = run(capsys, "up", "crm", *args, "--json")
    assert code == 0, out + err
    return json.loads(out)["next"]


def set_notes(capsys):
    run(capsys, "project", "notes", "crm", "--set", NOTES)


def test_up_says_what_state_the_server_is_in(capsys, box, cloud):
    set_notes(capsys)
    nxt = up(capsys)
    assert nxt["state"] == "running" and "nothing to start" in nxt["steps"][0]
    assert nxt["notes"] == NOTES and nxt["previews"] == []

    run(capsys, "pause", "crm")
    nxt = up(capsys)
    assert nxt["state"] == "restored" and "every-start" in nxt["steps"][0]

    run(capsys, "down", "crm")
    nxt = up(capsys, "--fresh")
    assert nxt["state"] == "new" and "new-server steps" in nxt["steps"][0]


def test_up_prints_notes_and_next_steps(capsys, box, cloud):
    set_notes(capsys)
    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "up", "crm")
    assert "next: restored from a snapshot" in out
    assert "notes:\n  New server: `npm ci`." in out


def test_up_without_notes_gives_a_checklist(capsys, box, cloud):
    nxt = up(capsys)
    assert nxt["notes"] is None
    assert any("no notes for crm yet" in s for s in nxt["steps"])
    assert any("project notes crm --set-file -" in s for s in nxt["steps"])


def test_previews_are_listed_with_the_command_to_reopen_them(capsys, box, cloud):
    expose(capsys, "5173", "--name", "web", "--protect")
    expose(capsys, "3000")
    run(capsys, "pause", "crm")
    nxt = up(capsys)
    assert [p["command"] for p in nxt["previews"]] == [
        "devbox expose crm 3000", "devbox expose crm 5173 --name web --protect",
    ]
    code, out, _ = run(capsys, "up", "crm")
    assert up(capsys)["previews"] == []  # running: they are still open (or shown by status)
    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "up", "crm")
    assert "  devbox expose crm 5173 --name web --protect" not in out  # nothing exposed on the last server


def test_quick_preview_on_a_stable_domain_reopens_with_quick(capsys, box, cloud, cf):
    expose(capsys, "5173", "--quick")
    run(capsys, "pause", "crm")
    assert up(capsys)["previews"][0]["command"] == "devbox expose crm 5173 --quick"


def test_pause_and_down_say_how_to_continue(capsys, box, cloud):
    code, out, _ = run(capsys, "pause", "crm", "--json")
    nxt = json.loads(out)["next"]
    assert "project notes crm --set-file -" in nxt["steps"][0]
    assert nxt["tell_user"] == "crm is paused; `devbox up crm` restores this snapshot"

    up(capsys)  # times a restore
    code, out, _ = run(capsys, "pause", "crm")
    assert "tell the user: crm is paused; `devbox up crm` restores this snapshot, about" in out

    up(capsys)
    code, out, _ = run(capsys, "down", "crm", "--json")
    assert "restores the older snapshot" in json.loads(out)["next"]["tell_user"]


def database(capsys, monkeypatch):
    monkeypatch.setattr(cli.Remote, "stream_out", lambda r, script, dest: dest.write_bytes(b"D") or 1)
    run(capsys, "db", "add", "crm", "main", "--engine", "sqlite", "--option", "path=app.db")


def test_down_refuses_to_lose_an_undumped_database(capsys, box, cloud, monkeypatch):
    database(capsys, monkeypatch)
    code, _, err = run(capsys, "down", "crm")
    assert code == 1 and "main has no dump" in err and "--dump" in err and "--no-dump" in err
    assert len(cloud.servers) == 1

    code, _, err = run(capsys, "down", "crm", "--at-hour-end")
    assert code == 1 and "devbox db dump crm" in err

    run(capsys, "pause", "crm")  # keeps the data: no question
    assert cloud.servers == {}


def test_down_after_a_fresh_dump_or_with_no_dump(capsys, box, cloud, monkeypatch):
    database(capsys, monkeypatch)
    run(capsys, "db", "dump", "crm")
    code, _, err = run(capsys, "down", "crm")
    assert code == 0, err


def test_down_with_dump_or_no_dump(capsys, box, cloud, monkeypatch):
    database(capsys, monkeypatch)
    code, _, err = run(capsys, "down", "crm", "--no-dump")
    assert code == 0, err
    up(capsys)
    code, _, err = run(capsys, "down", "crm", "--dump")
    assert code == 0, err


def test_use_after_the_dump_makes_it_stale(capsys, box, cloud, monkeypatch):
    database(capsys, monkeypatch)
    run(capsys, "db", "dump", "crm")
    from devbox import config, db
    conn = db.connect(config.load().db_path)
    server = db.active_server(conn, "crm")
    conn.execute("INSERT INTO events (project, server_id, kind, at) VALUES ('crm', ?, 'exec', '2999-01-01T00:00:00+00:00')",
                 (server["id"],))
    code, _, err = run(capsys, "down", "crm")
    assert code == 1 and "no dump since" in err
