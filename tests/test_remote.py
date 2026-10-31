import subprocess
from pathlib import Path

import pytest

from devbox import remote as remote_mod
from devbox.remote import ExecResult, Remote, git_files


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".env\n.env.*\n!.env.example\nnode_modules/\n")
    (root / "apps/api").mkdir(parents=True)
    (root / "apps/api/.env.example").write_text("A=1\n")
    (root / "apps/api/.env").write_text("SECRET=1\n")
    (root / "apps/api/index.js").write_text("x\n")
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "node_modules/pkg/index.js").write_text("x\n")
    git(root, "add", ".")
    (root / "new.txt").write_text("untracked, not ignored\n")
    return root


def test_git_files_follow_git_ignore_rules_including_negation(repo):
    assert git_files(repo) == [".gitignore", "apps/api/.env.example", "apps/api/index.js", "new.txt"]


def test_git_files_skip_tracked_files_deleted_locally(repo):
    (repo / "apps/api/index.js").unlink()
    assert "apps/api/index.js" not in git_files(repo)


def test_git_files_outside_git(tmp_path):
    assert git_files(tmp_path) is None


class Recorder:
    def __init__(self):
        self.rsync_input = None
        self.ssh_calls = []
        self.real_run = subprocess.run

    def run(self, cmd, **kw):
        if cmd[0] != "rsync":
            return self.real_run(cmd, **kw)
        self.rsync_cmd = cmd
        self.rsync_input = kw.get("input")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture
def remote(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(remote_mod.subprocess, "run", rec.run)
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))

    def fake_ssh(remote_cmd, *a, **kw):
        rec.ssh_calls.append((remote_cmd, kw.get("input")))
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(r, "_ssh", fake_ssh)
    return r, rec


def test_sync_sends_exactly_the_git_file_list(repo, remote, tmp_path, monkeypatch):
    r, rec = remote
    result = r.sync(str(repo), tmp_path / "manifest.json")
    assert result == {"mode": "git", "files": 4, "deleted": 0, "warning": None}
    sent = rec.rsync_input.decode().split("\0")
    assert "apps/api/.env.example" in sent
    assert "apps/api/.env" not in sent
    assert "--delete" not in rec.rsync_cmd
    assert "--files-from=-" in rec.rsync_cmd
    assert rec.ssh_calls == []


def test_sync_deletes_only_files_removed_locally(repo, remote, tmp_path, monkeypatch):
    r, rec = remote
    manifest = tmp_path / "manifest.json"
    r.sync(str(repo), manifest)

    (repo / "new.txt").unlink()
    # Newly ignored but still present locally: must not be deleted remotely.
    with open(repo / ".gitignore", "a") as f:
        f.write("apps/api/index.js\n")
    git(repo, "rm", "-q", "--cached", "apps/api/index.js")

    result = r.sync(str(repo), manifest)
    assert result["deleted"] == 1
    cmd, stdin = rec.ssh_calls[-1]
    assert "rm -f" in cmd
    assert stdin.split("\0") == ["new.txt"]


def test_exec_timeout_runs_in_scope_with_runtime_limit(monkeypatch):
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    calls = []

    def fake_ssh(remote_cmd, capture=True, check=True, timeout=120, *a, **kw):
        calls.append((remote_cmd, timeout))
        if "systemd-run --scope" in remote_cmd:
            return subprocess.CompletedProcess([], 0, "ok", "")
        return subprocess.CompletedProcess([], 1, "", "")

    monkeypatch.setattr(r, "_ssh", fake_ssh)
    result = r.exec("sleep 4", timeout=1.5)
    assert result == ExecResult(0, "ok", "", timed_out=False)
    cmd, local_timeout = calls[0]
    assert "-p RuntimeMaxSec=2s" in cmd
    assert "--unit devbox-exec-" in cmd
    assert local_timeout > 2


def test_exec_timeout_reports_and_verifies_stop(monkeypatch):
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    calls = []
    clock = iter([0.0, 5.0])
    monkeypatch.setattr(remote_mod.time, "monotonic", lambda: next(clock))

    def fake_ssh(remote_cmd, *a, **kw):
        calls.append(remote_cmd)
        if "systemd-run --scope" in remote_cmd:
            return subprocess.CompletedProcess([], 143, "", "")
        # is-active: scope is gone
        return subprocess.CompletedProcess([], 3, "", "")

    monkeypatch.setattr(r, "_ssh", fake_ssh)
    result = r.exec("sleep 10", timeout=1)
    assert result.timed_out
    assert any("is-active" in c for c in calls)
