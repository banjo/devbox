import io
import json
import subprocess
from pathlib import Path

import pytest

from devbox import DevboxError, cli, startup
from devbox import remote as remote_mod
from devbox.remote import Remote, parse_cloud_init_poll

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def deadline_and_progress(limit=60):
    clock = Clock()
    deadline = startup.Deadline(limit, clock=clock)
    out = io.StringIO()
    return clock, deadline, startup.Progress(deadline, stream=out), out


def test_deadline_bounds_and_expires():
    clock, deadline, _, _ = deadline_and_progress(100)
    assert deadline.bound("ssh", 15) == 15
    clock.now = 95
    assert deadline.bound("ssh", 15) == 5
    clock.now = 100
    with pytest.raises(startup.StageTimeout) as e:
        deadline.check("cloud-init")
    assert e.value.stage == "cloud-init" and e.value.exit_code == 124


def test_progress_lines_are_timestamped_and_deduplicated():
    clock, deadline, progress, out = deadline_and_progress()
    progress.step("ssh", "Waiting for SSH")
    clock.now = 65
    progress.waiting("cloud-init running; no further progress reported")
    progress.waiting("cloud-init running; no further progress reported")
    clock.now = 100
    progress.waiting("cloud-init running; no further progress reported")
    assert out.getvalue().splitlines() == [
        "[00:00] Waiting for SSH",
        "[01:05] cloud-init running; no further progress reported",
        "[01:40] cloud-init running; no further progress reported",
    ]


def test_redact():
    text = "DATABASE_URL=postgres://app:hunter22@db/app\nAPI_TOKEN=abc123\nAuthorization: Bearer xyz\nvalue sk-live-999"
    out = startup.redact(text, ["sk-live-999"])
    assert "hunter22" not in out and "abc123" not in out and "xyz" not in out and "sk-live-999" not in out
    assert "postgres://app:***@db/app" in out and "API_TOKEN=***" in out


@pytest.mark.parametrize("output, expected", [
    ("status: running\nrc=0\nGet:12 http://mirror ... 30%\n", ("running", 0, "Get:12 http://mirror ... 30%")),
    ("status: done\nrc=0\n", ("done", 0, None)),
    ("status: degraded done\nrc=2\nlast\n", ("done", 2, "last")),
    ("status: error\nrc=1\n", ("error", 1, None)),
    ("status: not started\nrc=0\n", ("not started", 0, None)),
])
def test_parse_cloud_init_poll(output, expected):
    assert parse_cloud_init_poll(output) == expected


class ScriptedSsh:
    """Fake `_ssh`: answers cloud-init polls from a list, other commands with fixed output."""

    def __init__(self, clock, polls, per_call=1.0, other=""):
        self.clock = clock
        self.polls = list(polls)
        self.per_call = per_call
        self.other = other
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append((cmd, kw.get("timeout")))
        self.clock.now += self.per_call
        if cmd.startswith("cloud-init status 2>"):
            out = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return subprocess.CompletedProcess([], 0, out, "")
        return subprocess.CompletedProcess([], 0, self.other, "")


def remote_with_ssh(monkeypatch, fake):
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    monkeypatch.setattr(r, "_ssh", fake)
    return r


def test_cloud_init_reports_progress_then_finishes(monkeypatch):
    clock, deadline, progress, out = deadline_and_progress(300)
    fake = ScriptedSsh(clock, [
        "status: running\nrc=0\nSetting up docker-ce (5:27)\n",
        "status: running\nrc=0\nSetting up docker-ce (5:27)\n",
        "status: done\nrc=0\n",
    ])
    r = remote_with_ssh(monkeypatch, fake)
    assert r.wait_cloud_init(deadline, progress, sleep=clock.sleep) == []
    lines = out.getvalue()
    assert "cloud-init running; last output: Setting up docker-ce" in lines
    assert "cloud-init finished" in lines
    assert all(timeout <= 20 for _, timeout in fake.calls)


def test_cloud_init_error_fails_at_once(monkeypatch):
    clock, deadline, progress, out = deadline_and_progress(300)
    fake = ScriptedSsh(clock, ["status: error\nrc=1\n"], other="extended_status: error\nerrors: apt failed TOKEN=s3cret")
    r = remote_with_ssh(monkeypatch, fake)
    with pytest.raises(startup.StageFailed) as e:
        r.wait_cloud_init(deadline, progress, sleep=clock.sleep)
    assert e.value.stage == "cloud-init" and "apt failed" in str(e.value) and "s3cret" not in str(e.value)
    assert clock.now < 10


def test_stalled_cloud_init_times_out_truthfully(monkeypatch):
    clock, deadline, progress, out = deadline_and_progress(120)
    fake = ScriptedSsh(clock, ["status: running\nrc=0\n"])
    r = remote_with_ssh(monkeypatch, fake)
    with pytest.raises(startup.StageTimeout) as e:
        r.wait_cloud_init(deadline, progress, sleep=clock.sleep)
    assert e.value.stage == "cloud-init"
    assert clock.now <= 125  # never runs past the deadline by more than one poll
    assert "no further progress reported" in out.getvalue()
    assert "%" not in out.getvalue()


def test_hung_ssh_probe_respects_deadline(monkeypatch):
    clock, deadline, progress, out = deadline_and_progress(30)

    def hung(cmd, **kw):
        clock.now += kw["timeout"]
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(remote_mod.subprocess, "run", hung)
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    with pytest.raises(startup.StageTimeout) as e:
        r.wait_ssh(deadline, progress, sleep=clock.sleep)
    assert e.value.stage == "ssh" and clock.now <= 31


def test_missing_tools_fail(monkeypatch):
    clock, deadline, progress, out = deadline_and_progress()
    r = remote_with_ssh(monkeypatch, ScriptedSsh(clock, ["x"], other="mise\ndocker compose\n"))
    with pytest.raises(startup.StageFailed, match="missing: mise, docker compose"):
        r.verify_tools(deadline, progress)


def test_sync_timeout_is_a_stage_timeout(monkeypatch, tmp_path):
    def slow(cmd, **kw):
        if cmd[0] == "rsync":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(remote_mod, "git_files", lambda root: ["a.txt"])
    monkeypatch.setattr(remote_mod.subprocess, "run", slow)
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    with pytest.raises(startup.StageTimeout) as e:
        r.sync(str(tmp_path), tmp_path / "m.json", timeout=1)
    assert e.value.stage == "sync"


def test_hcloud_call_stops_at_deadline(tmp_path):
    from devbox.hetzner import Hcloud

    script = tmp_path / "hcloud"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    hc = Hcloud(None, "abc", binary=str(script))
    hc.deadline = startup.Deadline(0.5)
    hc.stage = "create"
    with pytest.raises(startup.StageTimeout) as e:
        hc.list_servers()
    assert e.value.stage == "create" and "may still complete" in str(e.value)


# End to end through the CLI with the fake provider.


def fail_cloud_init(monkeypatch, exc):
    def failing(self, deadline, progress, secrets=()):
        progress.step("cloud-init", "Waiting for cloud-init (server setup)...")
        raise exc

    monkeypatch.setattr(cli.Remote, "wait_cloud_init", failing)


def test_cloud_init_timeout_keeps_server_and_explains(capsys, isolated, cloud, monkeypatch):
    from devbox import config, db, envsecrets

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    envsecrets.SecretStore(config.config_dir(), "crm").set(".env", {"API_KEY": "sk-live-123456"})
    monkeypatch.setattr(cli.Remote, "collect_diagnostics", lambda self, timeout=20: "apt API_KEY sk-live-123456\nGet:1 mirror\n")
    files = {}
    monkeypatch.setattr(cli.Remote, "read_file", lambda self, path: files.get(path))
    monkeypatch.setattr(cli.Remote, "write_file", lambda self, path, content, mode="600": files.__setitem__(path, content))
    fail_cloud_init(monkeypatch, startup.StageTimeout("cloud-init"))

    code, out, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    assert code == 124
    data = json.loads(out)  # stdout stays machine-readable; progress went to stderr
    assert data["code"] == "startup_timeout" and data["stage"] == "cloud-init"
    assert data["server_retained"] and data["billable"] and data["remote_may_still_run"]
    assert data["limit_seconds"] == 300
    diag = Path(data["diagnostics"])
    assert "sk-live-123456" not in diag.read_text() and "Get:1 mirror" in diag.read_text()
    assert "Creating server" in err

    server = db.active_server(db.connect(config.load().db_path), "crm")
    assert server["setup_stage"] == "cloud-init" and server["status"] == "creating"

    code, out, _ = run(capsys, "status", "crm")
    assert "setup failed at cloud-init" in out

    code, _, err = run(capsys, "up", "crm")
    assert code == 124 and "Retry startup: devbox up crm" in err and "still billable" in err
    assert len([c for c in cloud.calls if c[0] == "create"]) == 1

    # Retry succeeds on the same server, without a second create.
    monkeypatch.setattr(cli.Remote, "wait_cloud_init", lambda self, deadline, progress, secrets=(): [])
    code, out, err = run(capsys, "up", "crm")
    assert code == 0, err
    assert "Retrying setup of server" in err
    assert len([c for c in cloud.calls if c[0] == "create"]) == 1
    server = db.active_server(db.connect(config.load().db_path), "crm")
    assert server["status"] == "running" and server["setup_error"] is None


def test_cleanup_on_failure_deletes_only_fresh_servers(capsys, isolated, cloud, monkeypatch):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    fail_cloud_init(monkeypatch, startup.StageFailed("cloud-init", "cloud-init reported an error"))
    code, out, _ = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--cleanup-on-failure", "--json")
    data = json.loads(out)
    assert code == 1 and data["code"] == "startup_failed"
    assert data["cleanup"]["deleted"] and not data["server_retained"]
    assert cloud.servers == {}

    # A server restored from a snapshot is never deleted by cleanup.
    monkeypatch.setattr(cli.Remote, "wait_cloud_init", lambda self, deadline, progress, secrets=(): [])
    run(capsys, "up", "crm", "--fresh")
    run(capsys, "pause", "crm")
    fail_cloud_init(monkeypatch, startup.StageFailed("cloud-init", "boom"))
    code, out, _ = run(capsys, "up", "crm", "--cleanup-on-failure", "--json")
    data = json.loads(out)
    assert "skipped" in data["cleanup"] and data["server_retained"]
    assert len(cloud.servers) == 1 and len(cloud.images) == 1


def test_cleanup_failure_is_reported(capsys, isolated, cloud, monkeypatch):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    fail_cloud_init(monkeypatch, startup.StageTimeout("cloud-init"))
    cloud.fail_delete = True
    code, _, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--cleanup-on-failure")
    assert code == 124 and "Cleanup failed: locked" in err and "still billable" in err


def test_interrupt_is_reported_with_retained_server(capsys, isolated, cloud, monkeypatch):
    from devbox import config, db

    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))

    def interrupted(self, deadline, progress):
        progress.step("ssh", "Waiting for SSH")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.Remote, "wait_ssh", interrupted)
    code, out, _ = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    data = json.loads(out)
    assert code == 130 and data["code"] == "interrupted" and data["stage"] == "ssh" and data["server_retained"]
    assert db.active_server(db.connect(config.load().db_path), "crm")["setup_error"] == "interrupted (Ctrl-C)"


def test_create_timeout_mentions_possible_server(capsys, isolated, cloud, monkeypatch):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))

    def hung(*a, **kw):
        raise startup.StageTimeout("create", "hcloud server create did not finish; the action may still complete")

    monkeypatch.setattr(cloud, "create_server", hung)
    code, out, _ = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    data = json.loads(out)
    assert code == 124 and data["stage"] == "create" and data["server_id"] is None and data["billable"] is None


def test_invalid_startup_timeout_is_rejected_before_creating(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    for value in ("off", "soon", "0m"):
        code, _, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--startup-timeout", value)
        assert code == 1
    assert not any(c[0] == "create" for c in cloud.calls)


def test_preflight_errors_are_plain(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, _, err = run(capsys, "up", "crm")
    assert code == 1 and "no server chosen" in err and "Startup failed" not in err


def test_project_startup_timeout(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, out, _ = run(capsys, "project", "set", "crm", "--startup-timeout", "12m")
    assert "startup timeout 12m" in out
    fail_cloud_init_timeout = startup.StageTimeout("cloud-init")

    def failing(self, deadline, progress, secrets=()):
        raise fail_cloud_init_timeout

    import devbox.cli as c
    c.Remote.wait_cloud_init, original = failing, c.Remote.wait_cloud_init
    try:
        code, out, _ = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    finally:
        c.Remote.wait_cloud_init = original
    assert json.loads(out)["limit_seconds"] == 720
