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


class ScopeServer:
    """Fake `_ssh` for timed exec: the command's exit code and the scope's systemd state."""

    def __init__(self, returncode, result=None, active=False, reachable=True):
        self.returncode = returncode
        self.result = result
        self.active = active
        self.reachable = reachable
        self.calls = []

    def __call__(self, remote_cmd, *a, **kw):
        self.calls.append(remote_cmd)
        if "systemd-run --scope" in remote_cmd:
            assert kw.get("raise_on_255") is False
            return subprocess.CompletedProcess([], self.returncode, "partial out", "")
        if not self.reachable:
            from devbox import DevboxError
            raise DevboxError("ssh to 192.0.2.1 failed")
        if "show -p Result" in remote_cmd:
            return subprocess.CompletedProcess([], 0, f"{self.result}\n" if self.result else "", "")
        if "is-active" in remote_cmd:
            return subprocess.CompletedProcess([], 0 if self.active else 3, "", "")
        return subprocess.CompletedProcess([], 0, "", "")


def remote_with(monkeypatch, server):
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    monkeypatch.setattr(r, "_ssh", server)
    return r


@pytest.mark.parametrize("code", [255, 143, 137])
def test_exec_timeout_is_reported_as_timeout(monkeypatch, code):
    # OpenSSH exits 255 when the remote command is killed by a signal.
    server = ScopeServer(code, result="timeout")
    result = remote_with(monkeypatch, server).exec("sleep 5", timeout=2, capture=True)
    assert result.timed_out
    assert result.stdout == "partial out"
    assert "--collect" not in server.calls[0]
    assert any("reset-failed" in c for c in server.calls)


def test_exec_real_ssh_failure_is_still_an_error(monkeypatch):
    from devbox import DevboxError

    server = ScopeServer(255, reachable=False)
    with pytest.raises(DevboxError, match="ssh to 192.0.2.1 failed"):
        remote_with(monkeypatch, server).exec("true", timeout=2)


def test_exec_connection_lost_while_still_running(monkeypatch):
    from devbox import DevboxError

    server = ScopeServer(255, result="success", active=True)
    with pytest.raises(DevboxError, match="still running"):
        remote_with(monkeypatch, server).exec("sleep 60", timeout=30)


def test_exec_killed_by_something_else(monkeypatch):
    from devbox import DevboxError

    server = ScopeServer(255, result="signal")
    with pytest.raises(DevboxError, match="killed on the server"):
        remote_with(monkeypatch, server).exec("sleep 60", timeout=30)


def test_exec_normal_exit_codes_pass_through(monkeypatch):
    server = ScopeServer(17)
    result = remote_with(monkeypatch, server).exec("exit 17", timeout=30)
    assert result.returncode == 17 and not result.timed_out
    assert not any("show -p Result" in c for c in server.calls)


def test_exec_without_timeout_still_runs_in_a_scope(monkeypatch):
    # Issue #14: idle-check must be able to see foreground commands.
    server = ScopeServer(0)
    seen = {}

    def spy(remote_cmd, *a, **kw):
        if "systemd-run --scope" in remote_cmd:
            seen["timeout"] = kw.get("timeout")
        return server(remote_cmd, *a, **kw)

    r = remote_with(monkeypatch, spy)
    result = r.exec("make build")
    assert result.returncode == 0 and not result.timed_out
    cmd = server.calls[0]
    assert "systemd-run --scope" in cmd and "--unit devbox-exec-" in cmd
    assert "RuntimeMaxSec" not in cmd
    assert seen["timeout"] is None


def test_detached_keep_alive_gets_marked(monkeypatch):
    r = Remote("192.0.2.1", Path("/key"), Path("/kh"))
    calls = []
    monkeypatch.setattr(r, "run", lambda script, **kw: calls.append(script))
    r.start_unit("build", "make", keep_alive=True)
    r.start_unit("web", "npm run dev")
    assert "--description 'devbox-keepalive build'" in calls[0]
    assert "--description 'devbox web'" in calls[1]
