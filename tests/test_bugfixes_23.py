"""Regression tests for issue #23 (doctor and cleanup)."""

import json
import subprocess

import pytest

from devbox import DevboxError, admin, cli
from devbox.hetzner import Hcloud

from test_cli import OWNED_LABELS, cloud, isolated, run  # noqa: F401  (fixtures)


# 1. Scheduling detection


@pytest.mark.parametrize("crontab, scheduled", [
    ("*/15 * * * * $HOME/.local/bin/devbox idle-check >> $HOME/idle.log 2>&1\n", True),
    ("@hourly devbox idle-check\n", True),
    ("0 * * * * /usr/bin/env uv run devbox idle-check --json\n", True),
    ("# */15 * * * * $HOME/.local/bin/devbox idle-check\n", False),
    ("   #*/15 * * * * devbox idle-check\n", False),
    ("@reboot echo idle-check\n", False),
    ("*/15 * * * * devbox status\n", False),
    ("MAILTO=idle-check@example.com\n", False),
    ("", False),
])
def test_cron_detection(crontab, scheduled):
    assert admin.cron_has_idle_check(crontab) is scheduled


class FakeCtx:
    class cfg:
        idle_timeout = "8h"


def fake_commands(monkeypatch, crontab="", enabled="disabled", active="inactive"):
    def run_cmd(cmd):
        if cmd[0] == "crontab":
            return subprocess.CompletedProcess(cmd, 0, crontab, "")
        if "is-enabled" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if enabled == "enabled" else 1, enabled + "\n", "")
        return subprocess.CompletedProcess(cmd, 0 if active == "active" else 3, active + "\n", "")

    monkeypatch.setattr(admin, "_run", run_cmd)


def test_commented_cron_line_is_not_scheduled(monkeypatch):
    fake_commands(monkeypatch, crontab="# */15 * * * * devbox idle-check\n")
    assert admin.schedule_check(FakeCtx)["status"] == admin.WARN


def test_enabled_but_inactive_timer_warns(monkeypatch):
    fake_commands(monkeypatch, enabled="enabled", active="inactive")
    result = admin.schedule_check(FakeCtx)
    assert result["status"] == admin.WARN and "not active" in result["detail"]
    assert result["fix"] == "systemctl --user start devbox-idle-check.timer"


def test_enabled_and_active_timer_is_ok(monkeypatch):
    fake_commands(monkeypatch, enabled="enabled", active="active")
    assert admin.schedule_check(FakeCtx)["status"] == admin.OK


def test_real_cron_entry_is_ok(monkeypatch):
    fake_commands(monkeypatch, crontab="*/15 * * * * $HOME/.local/bin/devbox idle-check\n")
    assert admin.schedule_check(FakeCtx)["status"] == admin.OK


# 2. The key in use is never listed


def setup_keys(isolated, cloud, keys):
    ssh_dir = isolated / "home-ssh"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "devbox").write_text("private")
    (ssh_dir / "devbox.pub").write_text("ssh-ed25519 AAAA local")
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(f'ssh_key = "{ssh_dir / "devbox"}"\n')
    cloud.keys = keys
    cloud.firewalls = []


def test_ambiguous_key_lists_no_keys_and_explains(capsys, isolated, cloud, monkeypatch):
    setup_keys(isolated, cloud, [
        {"id": 1, "name": "devbox", "public_key": "ssh-ed25519 AAAA local", "labels": {"managed-by": "devbox"}},
        {"id": 2, "name": "someone-else", "public_key": "ssh-ed25519 AAAA local", "labels": {}},
        {"id": 3, "name": "devbox-old", "public_key": "ssh-ed25519 BBBB", "labels": OWNED_LABELS},
    ])

    def collision(public_key):
        raise DevboxError("the local devbox SSH key is already registered in Hetzner as 'someone-else' (id 2)")

    monkeypatch.setattr(cloud, "find_ssh_key", collision)
    code, out, _ = run(capsys, "cleanup", "--json")
    data = json.loads(out)
    assert [i for i in data["items"] if i["kind"] == "ssh-key"] == []
    assert any("SSH keys not checked" in n for n in data["notes"])

    code, out, _ = run(capsys, "cleanup", "--yes")
    assert not any(c[0] == "delete_ssh_key" for c in cloud.calls)
    assert "note: SSH keys not checked" in out


def test_key_matching_local_key_is_never_listed(capsys, isolated, cloud):
    setup_keys(isolated, cloud, [
        {"id": 1, "name": "devbox", "public_key": "ssh-ed25519 AAAA other-comment", "labels": {"managed-by": "devbox"}},
        {"id": 4, "name": "devbox-test", "public_key": "ssh-ed25519 AAAA local", "labels": OWNED_LABELS},
        {"id": 5, "name": "devbox-old", "public_key": "ssh-ed25519 CCCC", "labels": OWNED_LABELS},
    ])
    code, out, _ = run(capsys, "cleanup", "--json")
    keys = [i["id"] for i in json.loads(out)["items"] if i["kind"] == "ssh-key"]
    assert keys == [5]


# 3. Legacy snapshots can be deleted


def test_delete_image_accepts_legacy_snapshots(tmp_path):
    hc = Hcloud(None, "abc")
    calls = []

    def fake_run(*args, **kw):
        calls.append(args)
        if args[:2] == ("image", "describe"):
            return {"id": 9, "labels": {"managed-by": "devbox", "project": "old"}}

    hc.run = fake_run
    hc.delete_image(9)
    assert calls[-1] == ("image", "delete", "9")

    hc.run = lambda *a, **kw: {"id": 9, "labels": {"managed-by": "devbox", "devbox-controller": "other"}} if a[1] == "describe" else None
    with pytest.raises(DevboxError, match="not owned"):
        hc.delete_image(9)
