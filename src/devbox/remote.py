import json
import math
import os
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from devbox import DevboxError

APP_DIR = "/srv/app"
UNIT_PREFIX = "devbox-"
EXEC_UNIT_PREFIX = "devbox-exec-"
EXEC_KILL_AFTER = 10
# Exit codes of a command killed by a signal: ssh reports 255, a shell 128+N.
SIGNAL_EXITS = (255, 128 + 15, 128 + 9, -15, -9)
TUNNEL = "tunnel"
PROC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def validate_proc(name: str) -> None:
    if not PROC_RE.match(name):
        raise DevboxError(f"invalid process name '{name}': use lowercase letters, digits, - and _")


def to_script(cmd: list[str]) -> str:
    """One argument is used as a shell script, several are quoted as a command."""
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise DevboxError("no command given")
    return cmd[0] if len(cmd) == 1 else shlex.join(cmd)


@dataclass
class ExecResult:
    returncode: int
    stdout: str | None
    stderr: str | None
    timed_out: bool


def git_files(root: Path) -> list[str] | None:
    """Files Git considers part of the working tree, or None outside a Git repo."""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    files = set()
    for name in proc.stdout.decode().split("\0"):
        if not name:
            continue
        path = root / name
        # Skip tracked files deleted locally, and submodule directories.
        if not os.path.lexists(path) or (path.is_dir() and not path.is_symlink()):
            continue
        files.add(name)
    return sorted(files)


def read_manifest(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return set()


class Remote:
    def __init__(self, ip: str, key: Path, known_hosts: Path):
        if not ip:
            raise DevboxError("server has no IPv4 address")
        self.ip = ip
        self.key = key
        self.known_hosts = known_hosts

    def ssh_opts(self, connect_timeout: int = 10) -> list[str]:
        return [
            "-i", str(self.key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "ServerAliveInterval=30",
            "-o", "LogLevel=ERROR",
        ]

    def ssh_cmd(self, remote: str | None = None, tty: bool = False, connect_timeout: int = 10) -> list[str]:
        cmd = ["ssh", *self.ssh_opts(connect_timeout)]
        if tty:
            cmd.append("-t")
        cmd.append(f"root@{self.ip}")
        if remote is not None:
            cmd += ["--", remote]
        return cmd

    def run(
        self,
        script: str,
        capture: bool = True,
        check: bool = True,
        timeout: float | None = 120,
        cwd: str | None = APP_DIR,
        connect_timeout: int = 10,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        remote = f"bash -lc {shlex.quote(script)}"
        if cwd:
            remote = f"cd {shlex.quote(cwd)} && {remote}"
        return self._ssh(remote, capture, check, timeout, connect_timeout, input)

    def _ssh(
        self,
        remote: str,
        capture: bool = True,
        check: bool = True,
        timeout: float | None = 120,
        connect_timeout: int = 10,
        input: str | None = None,
        raise_on_255: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = self.ssh_cmd(remote, connect_timeout=connect_timeout)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=timeout,
                input=input,
                stdin=None if input is not None or not capture else subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise DevboxError(f"remote command timed out after {timeout}s")
        if proc.returncode == 255 and raise_on_255:
            detail = (proc.stderr or "").strip() if capture else ""
            raise DevboxError(f"ssh to {self.ip} failed{': ' + detail if detail else ''}")
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
            raise DevboxError(f"remote command failed (exit {proc.returncode}){': ' + detail if detail else ''}")
        return proc

    def exec(self, script: str, timeout: float | None = None, capture: bool = False) -> ExecResult:
        """Run a foreground command in the app folder.

        With a timeout, the command runs in its own systemd scope with
        RuntimeMaxSec, so the server itself stops it (SIGTERM to every process
        in the scope, SIGKILL after 10s) even if the SSH connection is lost.
        Whether the deadline was hit is read back from the scope's result.
        """
        if timeout is None:
            proc = self.run(script, capture=capture, check=False, timeout=None)
            return ExecResult(proc.returncode, proc.stdout, proc.stderr, timed_out=False)

        secs = max(1, math.ceil(timeout))
        unit = f"{EXEC_UNIT_PREFIX}{secrets.token_hex(4)}"
        # No --collect: a scope stopped by RuntimeMaxSec stays loaded as failed
        # with Result=timeout until reset-failed, so it can be checked below.
        remote = (
            f"cd {APP_DIR} && systemd-run --scope --quiet --unit {unit} "
            f"-p RuntimeMaxSec={secs}s -p TimeoutStopSec={EXEC_KILL_AFTER}s "
            f"--setenv=HOME=/root bash -lc {shlex.quote(script)}"
        )
        try:
            proc = self._ssh(
                remote, capture=capture, check=False, timeout=secs + EXEC_KILL_AFTER + 30,
                raise_on_255=False,
            )
        except DevboxError as e:
            if "timed out" not in str(e):
                raise
            stopped = self.stop_scope(unit)
            state = "stopped" if stopped else "could NOT be confirmed stopped"
            raise DevboxError(
                f"no response {secs}s after the deadline; remote command {state} (scope {unit})"
            )

        # Exit 255 is either an SSH failure or the remote command being killed
        # by a signal (which is what a deadline does). Ask the server which.
        result = self.scope_result(unit) if proc.returncode in SIGNAL_EXITS else None
        timed_out = result == "timeout"
        if proc.returncode == 255 and not timed_out:
            detail = (proc.stderr or "").strip() if capture else ""
            if result is None:
                raise DevboxError(f"ssh to {self.ip} failed{': ' + detail if detail else ''}")
            if not self.scope_gone(unit):
                raise DevboxError(
                    f"lost the connection; the command is still running on the server "
                    f"and will be stopped at the deadline (scope {unit})"
                )
            self._ssh(f"systemctl reset-failed {unit}.scope 2>/dev/null; true", check=False, timeout=30)
            raise DevboxError(f"the command was killed on the server (systemd result: {result})")
        if timed_out and not self.scope_gone(unit):
            self.stop_scope(unit)
        self._ssh(f"systemctl reset-failed {unit}.scope 2>/dev/null; true", check=False, timeout=30)
        return ExecResult(proc.returncode, proc.stdout, proc.stderr, timed_out=timed_out)

    def scope_result(self, unit: str) -> str | None:
        """systemd's Result for an exec scope ('timeout', 'signal', ...), or None if unknown."""
        try:
            proc = self._ssh(
                f"systemctl show -p Result --value {unit}.scope", check=False, timeout=30
            )
        except DevboxError:
            return None
        if proc.returncode != 0:
            return None
        value = (proc.stdout or "").strip()
        return value or None

    def stop_scope(self, unit: str) -> bool:
        self._ssh(f"systemctl kill --signal=SIGKILL {unit}.scope 2>/dev/null; true", check=False, timeout=30)
        return self.scope_gone(unit)

    def scope_gone(self, unit: str) -> bool:
        proc = self._ssh(f"systemctl is-active --quiet {unit}.scope", check=False, timeout=30)
        return proc.returncode != 0

    def wait_ready(self, timeout: float = 900) -> list[str]:
        """Wait for SSH and cloud-init. Returns warnings."""
        deadline = time.monotonic() + timeout
        while True:
            proc = subprocess.run(
                self.ssh_cmd("true", connect_timeout=5), capture_output=True, text=True
            )
            if proc.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise DevboxError(f"server {self.ip} did not accept SSH within {int(timeout)}s")
            time.sleep(5)

        remaining = max(60, deadline - time.monotonic())
        proc = self.run("cloud-init status --wait", check=False, timeout=remaining, cwd=None)
        if proc.returncode == 0:
            return []
        if proc.returncode == 2:
            return ["cloud-init finished with recoverable errors; see `cloud-init status --long`"]
        long = self.run("cloud-init status --long", check=False, cwd=None)
        raise DevboxError(f"cloud-init failed:\n{long.stdout.strip()}")

    def sync(self, local_path: str, manifest: Path) -> dict:
        """Copy the project to the server.

        In a Git repo, exactly the files Git would consider part of the working
        tree are sent: tracked files plus untracked files that are not ignored,
        using Git's own ignore rules. Ignored files on the server (node_modules,
        .env, build output) are left alone. Files that were sent before and are
        now deleted locally are deleted on the server too.
        """
        root = Path(local_path)
        files = git_files(root)
        ssh = shlex.join(["ssh", *self.ssh_opts()])
        dest = f"root@{self.ip}:{APP_DIR}/"

        if files is None:
            proc = subprocess.run(
                ["rsync", "-az", "--exclude", ".git", "-e", ssh, f"{root}/", dest],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise DevboxError(f"rsync failed: {proc.stderr.strip()}")
            return {"mode": "copy", "files": None, "deleted": 0,
                    "warning": "not a Git repository: copied everything except .git, deleted nothing"}

        previous = read_manifest(manifest)
        current = set(files)
        to_delete = sorted(f for f in previous - current if not os.path.lexists(root / f))

        proc = subprocess.run(
            ["rsync", "-az", "--from0", "--files-from=-", "-e", ssh, f"{root}/", dest],
            input="\0".join(files).encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            raise DevboxError(f"rsync failed: {proc.stderr.decode(errors='replace').strip()}")

        if to_delete:
            self._ssh(
                f"cd {APP_DIR} && xargs -0 -r rm -f --",
                input="\0".join(to_delete),
                timeout=120,
            )

        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(sorted(current)))
        return {"mode": "git", "files": len(files), "deleted": len(to_delete), "warning": None}

    # Background processes (systemd units on the server)

    def start_unit(self, name: str, script: str) -> None:
        unit = UNIT_PREFIX + name
        self.run(
            f"systemctl stop {unit} 2>/dev/null; systemctl reset-failed {unit} 2>/dev/null; "
            f"systemd-run --quiet --unit {unit} --working-directory={APP_DIR} --setenv=HOME=/root "
            f"bash -lc {shlex.quote(script)}",
            cwd=None,
        )

    def stop_unit(self, name: str) -> bool:
        unit = UNIT_PREFIX + name
        proc = self.run(
            f"systemctl stop {unit}; rc=$?; systemctl reset-failed {unit} 2>/dev/null; exit $rc",
            check=False,
            cwd=None,
        )
        return proc.returncode == 0

    def list_units(self) -> list[dict]:
        proc = self.run(
            f"systemctl list-units '{UNIT_PREFIX}*' --all --no-legend --plain --no-pager",
            cwd=None,
            timeout=30,
        )
        return parse_units(proc.stdout)

    def logs(self, name: str, lines: int, follow: bool) -> int:
        args = ["journalctl", "-u", UNIT_PREFIX + name, "--no-pager", "-o", "cat", "-n", str(lines)]
        if follow:
            args.append("-f")
        cmd = self.ssh_cmd(shlex.join(args))
        try:
            return subprocess.run(cmd).returncode
        except KeyboardInterrupt:
            return 0

    def start_tunnel(self, port: int, timeout: float = 60) -> str:
        self.start_unit(
            TUNNEL, f"exec cloudflared tunnel --no-autoupdate --url http://localhost:{port}"
        )
        unit = UNIT_PREFIX + TUNNEL
        script = (
            f"id=$(systemctl show -p InvocationID --value {unit}); "
            f"journalctl _SYSTEMD_INVOCATION_ID=$id -o cat --no-pager"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.run(script, check=False, cwd=None).stdout
            match = TUNNEL_URL_RE.search(out)
            if match:
                return match.group(0)
            time.sleep(2)
        raise DevboxError(f"no tunnel URL after {int(timeout)}s; see `devbox logs <project> {TUNNEL}`")


def parse_units(output: str) -> list[dict]:
    units = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].startswith(UNIT_PREFIX) or not parts[0].endswith(".service"):
            continue
        unit, _load, active, sub = parts[:4]
        name = unit.removeprefix(UNIT_PREFIX).removesuffix(".service")
        units.append({"name": name, "active": active, "sub": sub})
    return units
