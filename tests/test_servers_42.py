"""Tests for issue #42: several servers at once is a supported setup (`max_servers`, `--additional`)."""

import json

import pytest

from devbox import DevboxError, config
from devbox.remote import CAPACITY_MARKER, parse_capacity

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)


def write_config(isolated, text):
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(text)


def add_projects(capsys, isolated, *names):
    for name in names:
        (isolated / name).mkdir(exist_ok=True)
        run(capsys, "project", "add", name, "--path", str(isolated / name))


def up(capsys, name, *args):
    return run(capsys, "up", name, "--type", "cx23", "--location", "fsn1", *args)


def creates(cloud):
    return len([c for c in cloud.calls if c[0] == "create"])


def test_default_limit_refuses_without_suggesting_down_first(capsys, isolated, cloud):
    add_projects(capsys, isolated, "crm", "shop")
    up(capsys, "crm")

    code, _, err = up(capsys, "shop")
    assert code == 1
    assert creates(cloud) == 1
    assert "1 devbox server is running (crm) and max_servers is 1" in err
    options = [line.strip() for line in err.splitlines() if line.strip()[:2] in ("1.", "2.", "3.")]
    assert options[0].startswith("1. run the work on a running server") and "devbox exec crm" in options[0]
    assert "max_servers = 2" in options[1] and "config.toml" in options[1]
    assert "devbox up shop --additional" in options[2]
    assert "devbox down" not in err
    assert "devbox pause crm" in err.splitlines()[-1]


def test_refusal_in_json(capsys, isolated, cloud):
    add_projects(capsys, isolated, "crm", "shop")
    up(capsys, "crm")

    code, out, _ = up(capsys, "shop", "--json")
    data = json.loads(out)
    assert code == 1
    assert data["code"] == "server_limit"
    assert data["running"] == ["crm"] and data["max_servers"] == 1
    assert [o["action"] for o in data["options"]] == ["reuse", "raise_limit", "additional"]


@pytest.mark.parametrize("flag", ["--additional", "--force"])
def test_additional_starts_one_more_once(capsys, isolated, cloud, flag):
    add_projects(capsys, isolated, "crm", "shop", "blog")
    up(capsys, "crm")

    code, out, err = up(capsys, "shop", flag)
    assert code == 0, err
    assert "2 servers running now (also crm); each is billed separately" in out
    # The flag is for that one run; the limit still applies to the next project.
    code, _, err = up(capsys, "blog")
    assert code == 1 and "2 devbox servers are running (crm, shop) and max_servers is 1" in err


def test_max_servers_allows_more_without_a_flag(capsys, isolated, cloud):
    write_config(isolated, "max_servers = 2\n")
    add_projects(capsys, isolated, "crm", "shop", "blog")
    up(capsys, "crm")

    code, out, err = up(capsys, "shop", "--json")
    assert code == 0, err
    assert json.loads(out)["other_servers"] == ["crm"]
    assert creates(cloud) == 2

    code, _, err = up(capsys, "blog")
    assert code == 1
    assert "max_servers is 2" in err and "max_servers = 3" in err


@pytest.mark.parametrize("value", ["0", '"unlimited"'])
def test_no_limit(capsys, isolated, cloud, value):
    write_config(isolated, f"max_servers = {value}\n")
    add_projects(capsys, isolated, "a", "b", "c")
    for name in ("a", "b", "c"):
        code, _, err = up(capsys, name)
        assert code == 0, err
    assert creates(cloud) == 3


def test_reusing_a_running_server_is_not_limited(capsys, isolated, cloud):
    add_projects(capsys, isolated, "crm", "shop")
    up(capsys, "crm")
    up(capsys, "shop", "--additional")

    code, out, err = run(capsys, "up", "crm")
    assert code == 0, err
    assert "reusing server" in out and creates(cloud) == 2


def test_pause_leaves_the_other_server_alone(capsys, isolated, cloud):
    add_projects(capsys, isolated, "crm", "shop")
    up(capsys, "crm")
    up(capsys, "shop", "--additional")

    code, _, err = run(capsys, "pause", "shop")
    assert code == 0, err
    assert [s["project"] for s in cloud.servers.values()] == ["crm"]
    code, _, err = up(capsys, "shop")
    assert code == 1 and "max_servers is 1" in err


@pytest.mark.parametrize("value", ["-1", "true", '"lots"', "1.5"])
def test_invalid_max_servers(isolated, value):
    write_config(isolated, f"max_servers = {value}\n")
    with pytest.raises(DevboxError, match="max_servers must be a whole number"):
        config.load()


def test_max_servers_default_is_one(isolated):
    assert config.load().max_servers == 1
    write_config(isolated, "max_servers = 3\n")
    assert config.load().max_servers == 3


def test_parse_capacity():
    out = "2\n0.42 0.30 0.10\n3880000 2100000\n40000000000 30000000000\n"
    assert parse_capacity(out) == {
        "cores": 2, "load": {"1m": 0.42, "5m": 0.3, "15m": 0.1},
        "memory_gb": 3.7, "memory_free_gb": 2.0, "disk_gb": 37.3, "disk_free_gb": 27.9,
    }
    assert parse_capacity("") is None
    assert parse_capacity("2\nnot numbers\n") is None


def test_overview_splits_units_and_capacity(monkeypatch):
    import subprocess
    from pathlib import Path

    from devbox.remote import Remote

    stdout = (
        "devbox-web.service loaded active running npm run dev\n"
        f"{CAPACITY_MARKER}\n"
        "4\n1.5 1.0 0.5\n8000000 4000000\n80000000000 50000000000\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout, ""))
    units, capacity = Remote("192.0.2.1", Path("/k"), Path("/kh")).overview()
    assert [u["name"] for u in units] == ["web"]
    assert capacity["cores"] == 4 and capacity["load"]["5m"] == 1.0


def test_status_shows_capacity_per_server(capsys, isolated, cloud):
    add_projects(capsys, isolated, "crm")
    up(capsys, "crm")
    cloud.capacity = parse_capacity("2\n0.42 0.30 0.10\n3880000 2100000\n40000000000 30000000000\n")

    code, out, _ = run(capsys, "status")
    assert "capacity: load 0.30 on 2 cores (5 min avg), 2.0 of 3.7 GB memory free, 27.9 of 37.3 GB disk free" in out

    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["projects"][0]["capacity"]["memory_free_gb"] == 2.0
