import json

import pytest

from devbox import DevboxError, cli
from devbox.remote import parse_units, to_script


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "crm").mkdir()
    return tmp_path


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_project_add_list_notes(capsys, isolated):
    code, _, _ = run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    assert code == 0

    code, out, _ = run(capsys, "project", "list", "--json")
    projects = json.loads(out)
    assert projects == [
        {"name": "crm", "path": str((isolated / "crm").resolve()), "has_notes": False, "server": None}
    ]

    run(capsys, "project", "notes", "crm", "--set", "npm run dev, port 3000")
    code, out, _ = run(capsys, "project", "notes", "crm", "--json")
    assert json.loads(out)["notes"] == "npm run dev, port 3000\n"


def test_unknown_project_is_an_error(capsys):
    code, out, _ = run(capsys, "project", "notes", "nope", "--json")
    assert code == 1
    assert "unknown project" in json.loads(out)["error"]


def test_add_missing_path(capsys, isolated):
    code, _, err = run(capsys, "project", "add", "x", "--path", str(isolated / "missing"))
    assert code == 1
    assert "not a directory" in err


def test_exec_without_server_never_runs_locally(capsys, isolated, monkeypatch):
    import subprocess

    def boom(*a, **kw):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, _, err = run(capsys, "exec", "crm", "--", "touch", str(isolated / "pwned"))
    assert code == 1
    assert "has no server" in err
    assert not (isolated / "pwned").exists()


def test_exec_splits_at_double_dash(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_exec", lambda ctx: seen.update(vars(ctx.args)) or 0)
    parser_main = cli.main(["exec", "crm", "--detach", "--name", "web", "--", "npm", "run", "dev", "--", "--host"])
    assert parser_main == 0
    assert seen["detach"] and seen["proc"] == "web"
    assert seen["cmd"] == ["npm", "run", "dev", "--", "--host"]


def test_to_script():
    assert to_script(["npm ci && npm run build"]) == "npm ci && npm run build"
    assert to_script(["echo", "a b", "$HOME"]) == "echo 'a b' '$HOME'"
    with pytest.raises(DevboxError):
        to_script([])


def test_parse_units():
    out = (
        "devbox-web.service loaded active running /bin/bash -lc npm run dev\n"
        "devbox-tunnel.service loaded failed failed cloudflared\n"
    )
    assert parse_units(out) == [
        {"name": "web", "active": "active", "sub": "running"},
        {"name": "tunnel", "active": "failed", "sub": "failed"},
    ]
