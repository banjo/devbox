"""Tests for issue #27: `devbox autostop` and idle-check run records."""

import fcntl
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from devbox import DevboxError, admin, autostop, cli, config

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_bugfixes_24_29 import FOREIGN, add_project, running_server

UNRELATED = "0 3 * * * /usr/bin/backup --nightly\n"


@pytest.fixture
def cron(monkeypatch):
    """An in-memory crontab; no systemd timer; hcloud "installed"; verification passes."""
    store = {"text": UNRELATED}

    def fake_run(cmd):
        if cmd[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(cmd, 0, store["text"], "")
        return None

    def write(text):
        store["text"] = text

    monkeypatch.setattr(admin, "_run", fake_run)
    monkeypatch.setattr(autostop, "write_crontab", write)
    monkeypatch.setattr(autostop, "verify", lambda timeout=120: {"ok": True})
    monkeypatch.setattr(autostop.shutil, "which", lambda tool: f"/opt/tools/{tool}")
    return store


def token_file(isolated, extra=""):
    token = isolated / "hcloud-token"
    token.write_text("file-token\n")
    token.chmod(0o600)
    cfg_dir = isolated / "config" / "devbox"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(f'hcloud_token_file = "{token}"\n{extra}')
    return token


def state(isolated):
    return isolated / "state" / "devbox"


# enable / disable


def test_enable_requires_a_token_file(capsys, isolated, cron):
    code, out, _ = run(capsys, "autostop", "enable", "--yes", "--json")
    assert code == 1
    err = json.loads(out)
    assert err["code"] == "no_token_file" and "hcloud_token_file" in err["error"]
    assert cron["text"] == UNRELATED


def test_enable_without_yes_only_shows_the_plan(capsys, isolated, cron):
    token_file(isolated)
    code, out, _ = run(capsys, "autostop", "enable", "--json")
    assert code == 0
    plan = json.loads(out)
    assert plan["installed"] is False and plan["verified"] is None
    assert cron["text"] == UNRELATED
    entry = plan["entry"]
    assert entry.startswith("*/5 * * * * XDG_CONFIG_HOME=")
    assert str(isolated / "config") in entry and str(isolated / "state") in entry
    assert "-m devbox idle-check >> " in entry and "PATH=/opt/tools:" in entry
    assert "file-token" not in entry and "%" not in entry

    code, out, _ = run(capsys, "autostop", "enable")
    assert "nothing changed" in out and "--yes" in out


def test_enable_installs_once_and_keeps_other_lines(capsys, isolated, cron):
    token_file(isolated)
    code, out, _ = run(capsys, "autostop", "enable", "--yes")
    assert code == 0 and "enabled" in out
    assert cron["text"].startswith(UNRELATED)
    assert cron["text"].count("-m devbox idle-check") == 1
    assert cron["text"].count(autostop.MARKER) == 1

    code, out, _ = run(capsys, "autostop", "enable", "--yes", "--json")
    assert json.loads(out)["already_enabled"] is True
    assert cron["text"].count("-m devbox idle-check") == 1

    # Freshly installed: healthy until the first check is overdue.
    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert code == 0 and data["healthy"] is True
    assert "first check pending" in data["enforcement"]["detail"]


def test_failed_test_run_installs_nothing(capsys, isolated, cron, monkeypatch):
    token_file(isolated)
    monkeypatch.setattr(autostop, "verify", lambda timeout=120: {"ok": False, "error": "hcloud server list failed"})
    code, _, err = run(capsys, "autostop", "enable", "--yes")
    assert code == 1 and "nothing was installed" in err and "hcloud server list failed" in err
    assert cron["text"] == UNRELATED


def test_enable_warns_about_down_and_manual_schedules(capsys, isolated, cron):
    token_file(isolated, 'idle_action = "down"\n')
    cron["text"] += "*/30 * * * * $HOME/.local/bin/devbox idle-check\n"
    code, out, _ = run(capsys, "autostop", "enable", "--json")
    warnings = " ".join(json.loads(out)["warnings"])
    assert "deleted with their data" in warnings
    assert "not managed by autostop" in warnings


def test_entries_per_config_home_and_disable_removes_only_its_own(capsys, isolated, cron, monkeypatch):
    token_file(isolated)
    run(capsys, "autostop", "enable", "--yes")
    first = cron["text"]

    other = isolated / "other-config"
    (other / "devbox").mkdir(parents=True)
    (other / "devbox" / "config.toml").write_text(
        f'hcloud_token_file = "{isolated / "hcloud-token"}"\ncontroller_id = "draw-mobile"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(other))
    run(capsys, "autostop", "enable", "--yes")
    assert cron["text"].count(autostop.MARKER) == 2

    code, out, _ = run(capsys, "autostop", "disable")
    assert "would remove" in out and cron["text"].count(autostop.MARKER) == 2
    code, out, _ = run(capsys, "autostop", "disable", "--yes", "--json")
    assert json.loads(out)["removed"] is True
    assert cron["text"] == first

    code, out, _ = run(capsys, "autostop", "disable", "--yes")
    assert "not enabled" in out and cron["text"] == first


def test_entry_parsing_ignores_lookalikes():
    crontab = (
        f"{UNRELATED}"
        "# devbox-autostop-ish config=/x state=/y\n"
        "*/15 * * * * devbox idle-check\n"
        f"{autostop.MARKER} config='/a b' state=/s\n"
        "*/15 * * * * XDG_CONFIG_HOME=/a devbox idle-check\n"
    )
    entries = autostop.parse_entries(crontab)
    assert [(e["config_dir"], e["state_dir"]) for e in entries] == [("/a b", "/s")]
    assert autostop.without_entry(crontab, "/a b", "/s") == (
        f"{UNRELATED}# devbox-autostop-ish config=/x state=/y\n*/15 * * * * devbox idle-check\n"
    )


def test_percent_in_a_path_is_refused(isolated, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "50%"))
    with pytest.raises(DevboxError, match="'%'"):
        autostop.build_entry()


# Health


def enabled(capsys, isolated, cron):
    token_file(isolated)
    run(capsys, "autostop", "enable", "--yes")


def test_recent_success_is_healthy_and_shows_when_servers_are_checked(capsys, isolated, cloud, cron):
    enabled(capsys, isolated, cron)
    autostop.record_run(state(isolated), None)
    add_project(capsys, isolated)
    running_server(cloud)

    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert code == 0 and data["healthy"] is True
    assert data["enforcement"]["status"] == "ok" and "last check" in data["enforcement"]["detail"]
    server = data["servers"][0]
    assert server["idle_timeout"] == "8h" and server["eligible_at"] and server["first_check"]
    first = datetime.fromisoformat(server["first_check"])
    assert first.minute % 5 == 0 and first >= datetime.fromisoformat(server["eligible_at"])

    code, out, _ = run(capsys, "status")
    assert "not enforced" not in out


def test_stale_schedule_is_not_healthy(capsys, isolated, cloud, cron, monkeypatch):
    enabled(capsys, isolated, cron)
    autostop.record_run(state(isolated), None, now=datetime.now(timezone.utc) - timedelta(hours=2))
    add_project(capsys, isolated)
    running_server(cloud)

    code, out, _ = run(capsys, "autostop", "status")
    assert code == 1 and "[FAIL] idle shutdown" in out and "cron may not be running" in out
    assert "nothing will act on it" in out
    code, out, _ = run(capsys, "status")
    assert "warning: idle shutdown is not enforced" in out


def test_failed_last_run_is_not_healthy(capsys, isolated, cloud, cron, monkeypatch):
    enabled(capsys, isolated, cron)
    now = datetime.now(timezone.utc)
    autostop.record_run(state(isolated), None, now=now - timedelta(minutes=20))
    autostop.record_run(state(isolated), "hcloud server list failed: unauthorized", now=now - timedelta(minutes=5))
    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert code == 1 and data["healthy"] is False
    assert "unauthorized" in data["enforcement"]["detail"]


def test_missing_token_file_makes_an_installed_schedule_unhealthy(capsys, isolated, cloud, cron, monkeypatch):
    enabled(capsys, isolated, cron)
    autostop.record_run(state(isolated), None)
    (isolated / "config" / "devbox" / "config.toml").write_text("")
    monkeypatch.setenv("HCLOUD_TOKEN", "shell-only")
    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert code == 1 and data["enforcement"]["status"] == "fail"
    assert "HCLOUD_TOKEN" in data["enforcement"]["detail"]


def test_not_enabled_status_and_overrides(capsys, isolated, cloud):
    add_project(capsys, isolated)
    running_server(cloud)
    run(capsys, "project", "set", "crm", "--idle-timeout", "off")
    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert code == 1 and data["entry"] is None
    assert data["enforcement"]["fix"] == "devbox autostop enable"
    assert data["servers"][0]["idle_timeout"] == "off" and data["servers"][0]["eligible_at"] is None


def test_uncovered_controllers_are_reported(capsys, isolated, cloud, cron, monkeypatch):
    enabled(capsys, isolated, cron)
    autostop.record_run(state(isolated), None)
    cloud.foreign = [FOREIGN]

    code, out, _ = run(capsys, "autostop", "status")
    assert "controller draw-mobile has server(s)" in out and "devbox-draw-mobile" in out

    # Once an entry on this machine covers draw-mobile, the warning goes away.
    other = isolated / "other-config"
    (other / "devbox").mkdir(parents=True)
    (other / "devbox" / "config.toml").write_text('controller_id = "draw-mobile"\n')
    cron["text"] += f"{autostop.MARKER} config={other / 'devbox'} state=/tmp/s\n*/15 * * * * true idle-check\n"
    code, out, _ = run(capsys, "autostop", "status", "--json")
    data = json.loads(out)
    assert data["uncovered_controllers"] == []
    controllers = [c["controller"] for c in data["coverage"]]
    assert len(controllers) == 2 and "draw-mobile" in controllers
    assert not cloud.calls  # read-only


def test_manual_cron_with_failed_last_run_warns(cron, isolated):
    cron["text"] += "*/15 * * * * devbox idle-check\n"
    autostop.record_run(state(isolated), "boom")
    result = autostop.enforcement(config.load())
    assert result["status"] == admin.WARN and "boom" in result["detail"]


def test_another_configs_entry_does_not_count_for_this_one(cron, isolated):
    cron["text"] += f"{autostop.MARKER} config=/elsewhere/devbox state=/elsewhere/state\n" \
                    "*/15 * * * * XDG_CONFIG_HOME=/elsewhere python -m devbox idle-check\n"
    assert autostop.enforcement(config.load())["status"] == admin.WARN


# idle-check run records and overlap


def test_idle_check_records_success_and_failure(capsys, isolated, cloud, monkeypatch):
    code, _, _ = run(capsys, "idle-check", "--dry-run")
    assert not (state(isolated) / autostop.RUNS_FILE).exists()

    code, _, _ = run(capsys, "idle-check")
    runs = json.loads((state(isolated) / autostop.RUNS_FILE).read_text())
    assert code == 0 and runs["last_success"] and "last_failure" not in runs

    def broken():
        raise DevboxError("hcloud server list failed: unauthorized")

    monkeypatch.setattr(cloud, "list_all_servers", broken)
    code, _, _ = run(capsys, "idle-check")
    runs = json.loads((state(isolated) / autostop.RUNS_FILE).read_text())
    assert code == 1 and runs["last_error"] == "hcloud server list failed: unauthorized"
    assert autostop.last_failed(runs)


def test_overlapping_idle_check_is_skipped(capsys, isolated, cloud):
    state(isolated).mkdir(parents=True, exist_ok=True)
    with open(state(isolated) / "idle-check.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out, _ = run(capsys, "idle-check")
    assert code == 0 and "still running; skipped" in out
    assert not (state(isolated) / autostop.RUNS_FILE).exists()


# The real test run: a cron-like environment and a fake hcloud


def test_verify_runs_the_job_without_the_shell_environment(isolated, monkeypatch):
    bin_dir = isolated / "bin"
    bin_dir.mkdir()
    hcloud = bin_dir / "hcloud"
    hcloud.write_text(
        "#!/bin/sh\n"
        'if [ "$HCLOUD_TOKEN" != "file-token" ]; then echo "wrong token: $HCLOUD_TOKEN" >&2; exit 1; fi\n'
        "echo '[]'\n"
    )
    hcloud.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HCLOUD_TOKEN", "shell-token")
    token_file(isolated)
    assert autostop.verify() == {"ok": True}

    (isolated / "hcloud-token").write_text("revoked\n")
    result = autostop.verify()
    assert result["ok"] is False and "wrong token: revoked" in result["error"]
