import re
import shlex
import subprocess
import time
from pathlib import Path

from devbox import DevboxError

APP_DIR = "/srv/app"
UNIT_PREFIX = "devbox-"
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
    ) -> subprocess.CompletedProcess:
        remote = f"bash -lc {shlex.quote(script)}"
        if cwd:
            remote = f"cd {shlex.quote(cwd)} && {remote}"
        cmd = self.ssh_cmd(remote, connect_timeout=connect_timeout)
        try:
            proc = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DevboxError(f"remote command timed out after {timeout}s")
        if proc.returncode == 255:
            detail = (proc.stderr or "").strip() if capture else ""
            raise DevboxError(f"ssh to {self.ip} failed{': ' + detail if detail else ''}")
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
            raise DevboxError(f"remote command failed (exit {proc.returncode}){': ' + detail if detail else ''}")
        return proc

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

    def sync(self, local_path: str) -> None:
        src = local_path.rstrip("/") + "/"
        ssh = shlex.join(["ssh", *self.ssh_opts()])
        cmd = [
            "rsync", "-az", "--delete",
            "--exclude", ".git",
            "--filter", ":- .gitignore",
            "-e", ssh,
            src,
            f"root@{self.ip}:{APP_DIR}/",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DevboxError(f"rsync failed: {proc.stderr.strip()}")

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
        if len(parts) < 4 or not parts[0].startswith(UNIT_PREFIX):
            continue
        unit, _load, active, sub = parts[:4]
        name = unit.removeprefix(UNIT_PREFIX).removesuffix(".service")
        units.append({"name": name, "active": active, "sub": sub})
    return units
