"""Regression tests for issues #24 (Compose file location), #26 (status scope),
#27 (idle-check schedule warning) and #29 (`--json` placement)."""

import json
import subprocess

import pytest

from devbox import DevboxError, cli, databases

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_hetzner import SERVER, fake_hcloud  # noqa: F401  (fixtures)


def add_project(capsys, isolated):
    code, _, _ = run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    assert code == 0


def running_server(cloud, sid=78):
    cloud.servers[sid] = {
        "id": sid, "name": "devbox-crm", "status": "running", "ip": "192.0.2.8",
        "server_type": "cx23", "location": "fsn1", "project": "crm", "created": "2026-10-31T08:00:00+00:00",
    }


# #29: `--json` is honoured wherever it is accepted


@pytest.mark.parametrize("argv", [
    ["--json", "project", "list"],
    ["project", "--json", "list"],
    ["project", "list", "--json"],
    ["--json", "project", "--json", "list", "--json"],
])
def test_json_flag_works_at_every_level(capsys, isolated, argv):
    add_project(capsys, isolated)
    code, out, _ = run(capsys, *argv)
    assert code == 0
    assert [p["name"] for p in json.loads(out)] == ["crm"]


@pytest.mark.parametrize("argv", [["--json", "status"], ["status", "--json"]])
def test_json_flag_before_and_after_top_level_command(capsys, isolated, cloud, argv):
    code, out, _ = run(capsys, *argv)
    assert code == 0
    assert json.loads(out)["projects"] == []


def test_without_flag_output_stays_human(capsys, isolated):
    add_project(capsys, isolated)
    code, out, _ = run(capsys, "project", "list")
    assert code == 0 and out.startswith("crm")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_json_errors_from_commands_are_json_with_nonzero_exit(capsys, isolated):
    code, out, _ = run(capsys, "--json", "project", "notes", "nope")
    assert code == 1
    assert "unknown project" in json.loads(out)["error"]


@pytest.mark.parametrize("argv", [
    ["--json", "project", "frobnicate"],
    ["--json", "status", "--nope"],
    ["project", "list", "--json", "--nope"],
])
def test_usage_errors_in_json_mode_are_json(capsys, isolated, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert json.loads(out)["code"] == "usage"


def test_usage_errors_without_json_stay_argparse_text(capsys, isolated):
    with pytest.raises(SystemExit) as exc:
        cli.main(["project", "frobnicate"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "invalid choice" in captured.err


# #26: status is explicit about its controller scope


FOREIGN = {
    "id": 173845115, "name": "devbox-draw-mobile", "status": "running", "ip": "192.0.2.99",
    "server_type": "cpx22", "location": "hel1", "project": "draw-mobile",
    "controller": "draw-mobile", "created": None,
}


def test_list_all_servers_includes_other_controllers(fake_hcloud):
    hc, calls, respond = fake_hcloud
    mine = dict(SERVER, labels={"managed-by": "devbox", "devbox-controller": "abc123", "project": "crm"})
    other = dict(SERVER, id=43, labels={"managed-by": "devbox", "devbox-controller": "other", "project": "x"})
    legacy = dict(SERVER, id=44, labels={"managed-by": "devbox", "project": "old"})
    respond("server_list", [mine, other, legacy])
    assert [(s["id"], s["controller"]) for s in hc.list_all_servers()] == [(42, "abc123"), (43, "other"), (44, None)]
    assert [s["id"] for s in hc.list_servers()] == [42, 44]


def test_empty_status_names_the_controller_and_checks_others(capsys, isolated, cloud):
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "no devbox servers for controller test" in out
    assert "no devbox servers from other controllers in this Hetzner project" in out

    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["controller"] == "test" and data["scope"] == "controller"
    assert data["other_controllers"] == []


def test_status_reports_other_controllers_servers_without_touching_them(capsys, isolated, cloud):
    add_project(capsys, isolated)
    cloud.foreign = [FOREIGN]

    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "no devbox servers for controller test" in out
    assert "devbox-draw-mobile (id 173845115, running, controller draw-mobile" in out
    assert "unregistered" not in out

    code, out, _ = run(capsys, "status", "--json")
    data = json.loads(out)
    assert [s["id"] for s in data["other_controllers"]] == [173845115]
    assert data["unregistered"] == []
    assert data["projects"][0]["server"] is None

    # Not adopted, not offered for cleanup, never mutated.
    code, out, _ = run(capsys, "cleanup", "--json")
    assert all(item["id"] != 173845115 for item in json.loads(out)["items"])
    assert not [c for c in cloud.calls if 173845115 in c]


def test_status_lists_own_and_other_servers_separately(capsys, isolated, cloud):
    add_project(capsys, isolated)
    running_server(cloud)
    cloud.foreign = [FOREIGN]
    code, out, _ = run(capsys, "status")
    assert "crm: running cx23" in out
    assert "no devbox servers for controller" not in out
    assert "devbox-draw-mobile" in out


# #27: warn when idle timeouts are set but nothing runs idle-check


def scheduled_cron(monkeypatch):
    def fake(cmd):
        if cmd[0] == "crontab":
            return subprocess.CompletedProcess(cmd, 0, "*/15 * * * * devbox idle-check\n", "")
        return None

    monkeypatch.setattr(cli.admin, "_run", fake)


def test_status_warns_when_idle_check_is_not_scheduled(capsys, isolated, cloud):
    add_project(capsys, isolated)
    running_server(cloud)
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "warning: idle shutdown is not enforced" in out

    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["idle_enforcement"]["status"] == "warn"


def test_status_does_not_warn_when_idle_check_is_scheduled(capsys, isolated, cloud, monkeypatch):
    add_project(capsys, isolated)
    running_server(cloud)
    scheduled_cron(monkeypatch)
    code, out, _ = run(capsys, "status")
    assert "not enforced" not in out
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["idle_enforcement"]["status"] == "ok"


def test_status_does_not_warn_without_servers_or_with_timeout_off(capsys, isolated, cloud):
    add_project(capsys, isolated)
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["idle_enforcement"] is None

    running_server(cloud)
    run(capsys, "project", "set", "crm", "--idle-timeout", "off")
    code, out, _ = run(capsys, "status")
    assert "not enforced" not in out


def test_up_warns_when_idle_check_is_not_scheduled(capsys, isolated, cloud, monkeypatch):
    add_project(capsys, isolated)
    code, out, _ = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    assert code == 0
    assert any("idle shutdown is not enforced" in w for w in json.loads(out)["warnings"])

    scheduled_cron(monkeypatch)
    code, out, _ = run(capsys, "up", "crm", "--json")
    assert not any("not enforced" in w for w in json.loads(out)["warnings"])


# #24: Compose files outside the project root


def test_compose_file_is_passed_to_docker_compose():
    pg = databases.get_engine("postgres")
    settings = pg.validate({"database": "app", "service": "db", "compose_file": "docker/docker-compose.yml"})
    assert pg.dump_command(settings).startswith(
        "docker compose -f docker/docker-compose.yml exec -T db pg_dump "
    )
    assert pg.restore_command(settings).startswith(
        "docker compose -f docker/docker-compose.yml exec -T db pg_restore "
    )
    # Unchanged without the option.
    plain = pg.validate({"database": "app", "service": "db"})
    assert pg.dump_command(plain).startswith("docker compose exec -T db ")


@pytest.mark.parametrize("settings, message", [
    ({"database": "app", "compose_file": "docker/compose.yml"}, "only applies together with"),
    ({"database": "app", "service": "db", "compose_file": "/etc/compose.yml"}, "invalid compose_file"),
    ({"database": "app", "service": "db", "compose_file": "../compose.yml"}, "invalid compose_file"),
    ({"database": "app", "service": "db", "compose_file": "a b.yml"}, "invalid compose_file"),
])
def test_invalid_compose_file(settings, message):
    with pytest.raises(DevboxError, match=message):
        databases.get_engine("postgres").validate(settings)


DOCKER_ERROR = "remote command failed (exit 1): no configuration file provided: not found"


@pytest.mark.parametrize("compose_file, hint", [
    (None, "--option compose_file=PATH"),
    ("docker/compose.yml", "'docker/compose.yml' was not found"),
])
def test_missing_compose_file_error_explains_the_fix(capsys, isolated, cloud, monkeypatch, compose_file, hint):
    def fail(*args, **kwargs):
        raise DevboxError(DOCKER_ERROR)

    monkeypatch.setattr(cli.Remote, "stream_out", fail)
    monkeypatch.setattr(cli.Remote, "stream_in", fail)
    add_project(capsys, isolated)
    options = ["--option", "service=db", "--option", "database=app"]
    if compose_file:
        options += ["--option", f"compose_file={compose_file}"]
    run(capsys, "db", "add", "crm", "main", "--engine", "postgres", *options)
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")

    code, _, err = run(capsys, "db", "dump", "crm")
    assert code == 1 and DOCKER_ERROR in err and hint in err

    dump = isolated / "main.pgdump"
    dump.write_bytes(b"DUMP")
    code, out, _ = run(capsys, "db", "restore", "crm", "--file", str(dump), "--yes", "--json")
    assert code == 1 and hint in json.loads(out)["error"]


def test_other_db_failures_are_unchanged():
    assert databases.failure_hint({"service": "db"}, "remote command failed (exit 1): role does not exist") is None
    assert databases.failure_hint({"path": "app.db"}, DOCKER_ERROR) is None
