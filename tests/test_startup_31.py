"""Tests for issue #31: stage timings, no apt-get update on restore, smaller snapshots."""

import json
from importlib import resources

from devbox import DevboxError, cli, config, db
from devbox.remote import SNAPSHOT_CLEANUP

from test_cli import cloud, isolated, run  # noqa: F401  (fixtures)
from test_startup import deadline_and_progress


def test_progress_adds_up_time_per_stage():
    clock, deadline, progress, _ = deadline_and_progress()
    clock.now = 1.0
    progress.step("create", "Creating...")
    clock.now = 11.0
    progress.step("ssh", "Waiting for SSH...")
    clock.now = 32.0
    progress.step("cloud-init", "Waiting for cloud-init...")
    clock.now = 91.0
    progress.step("sync", "Syncing...")
    clock.now = 92.2
    assert progress.stage_seconds() == {"prepare": 1.0, "create": 10.0, "ssh": 21.0, "cloud-init": 59.0, "sync": 1.2}
    assert cli.stage_summary(progress.stage_seconds()) == "prepare 1s, create 10s, ssh 21s, cloud-init 59s, sync 1s"


def test_up_reports_and_records_stage_timings(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    code, out, err = run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1", "--json")
    assert code == 0, out + err
    stages = json.loads(out)["stages"]
    assert list(stages)[:2] == ["prepare", "create"] and "sync" in stages

    conn = db.connect(config.load().db_path)
    recorded = json.loads(db.active_server(conn, "crm")["startup"])
    assert recorded["kind"] == "create" and recorded["stages"].keys() == stages.keys()

    code, out, _ = run(capsys, "up", "crm")
    assert "startup:" not in out  # reusing a running server isn't a startup
    code, out, _ = run(capsys, "history", "--json")
    assert json.loads(out)["servers"][0]["startup"]["kind"] == "create"
    code, out, _ = run(capsys, "history")
    assert "up in " in out

    run(capsys, "pause", "crm")
    code, out, _ = run(capsys, "up", "crm")
    assert "startup: " in out
    code, out, _ = run(capsys, "history", "--json")
    assert json.loads(out)["servers"][0]["startup"]["kind"] == "restore"


def test_pause_cleans_up_before_the_snapshot(capsys, isolated, cloud):
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    code, _, err = run(capsys, "pause", "crm")
    assert code == 0 and "cleaning up crm before the snapshot" in err
    kinds = [c[0] for c in cloud.calls]
    assert kinds[-4:] == ["prepare_snapshot", "shutdown", "snapshot", "delete"]


def test_snapshot_goes_ahead_when_cleanup_fails(capsys, isolated, cloud, monkeypatch):
    def unreachable(self, timeout=60):
        raise DevboxError("ssh to 192.0.2.1 failed")

    monkeypatch.setattr(cli.Remote, "prepare_snapshot", unreachable)
    run(capsys, "project", "add", "crm", "--path", str(isolated / "crm"))
    run(capsys, "up", "crm", "--type", "cx23", "--location", "fsn1")
    code, out, err = run(capsys, "pause", "crm")
    assert code == 0 and "paused server" in out
    assert "cleanup before the snapshot failed, snapshotting as is" in err


def test_snapshot_cleanup_keeps_containers_and_build_cache():
    assert "docker image prune -f" in SNAPSHOT_CLEANUP
    assert "system prune" not in SNAPSHOT_CLEANUP and "builder prune" not in SNAPSHOT_CLEANUP
    assert " -a" not in SNAPSHOT_CLEANUP.split("docker image prune")[1].split(";")[0]


def test_cloud_init_installs_packages_only_when_missing():
    text = resources.files("devbox").joinpath("cloud-init.yaml").read_text()
    assert "package_update" not in text and "packages:" not in text
    assert "dpkg-query" in text and "apt-get update &&" in text and "|| exit 1" in text
