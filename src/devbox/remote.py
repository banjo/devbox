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
from devbox.startup import Deadline, Progress, StageFailed, StageTimeout, redact

APP_DIR = "/srv/app"
UNIT_PREFIX = "devbox-"
EXEC_UNIT_PREFIX = "devbox-exec-"
EXEC_KILL_AFTER = 10
KEEP_ALIVE_DESCRIPTION = "devbox-keepalive"
# Exit codes of a command killed by a signal: ssh reports 255, a shell 128+N.
SIGNAL_EXITS = (255, 128 + 15, 128 + 9, -15, -9)
TUNNEL = "tunnel"
# Folders never listed by `pull`: dependencies, VCS data and caches.
PULL_PRUNE = (".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".next", ".turbo")
PROC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
HOST_KEY_MISMATCH_RE = re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|"
                                  r"no matching host key type")
TUNNEL_REGISTERED_RE = re.compile(r"Registered tunnel connection")
# devbox's own files on the server, outside the project folder.
DEVBOX_DIR = "/root/.devbox"
TUNNEL_TOKEN_FILE = f"{DEVBOX_DIR}/tunnel-token"


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


CLOUD_INIT_POLL = (
    "cloud-init status 2>/dev/null; echo \"rc=$?\"; "
    "tail -n 1 /var/log/cloud-init-output.log 2>/dev/null"
)
TOOLS_CHECK = (
    "for t in docker rsync git mise cloudflared python3; do command -v $t >/dev/null || echo $t; done; "
    "docker compose version >/dev/null 2>&1 || echo 'docker compose'"
)
DIAGNOSTICS = (
    "echo '== cloud-init status'; cloud-init status --long 2>&1; "
    "echo '== last setup output'; tail -n 60 /var/log/cloud-init-output.log 2>&1; "
    "echo '== failed units'; systemctl --failed --no-legend --plain 2>&1"
)


def parse_cloud_init_poll(output: str) -> tuple[str | None, int | None, str | None]:
    """(status, exit code, last log line) from CLOUD_INIT_POLL output."""
    status = code = last = None
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("status:"):
            value = line.split(":", 1)[1].strip()
            # e.g. "running", "done", "degraded done", "error", "not started", "disabled"
            for known in ("error", "done", "running", "disabled"):
                if known in value.split():
                    status = known
                    break
            else:
                status = value or None
        elif line.startswith("rc="):
            try:
                code = int(line[3:])
            except ValueError:
                pass
            rest = [l for l in lines[i + 1:] if l.strip()]
            last = rest[-1].strip() if rest else None
            break
    return status, code, last


def _run_bounded(cmd: list[str], timeout: float | None, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise StageTimeout("sync", "syncing code did not finish before the startup deadline")


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


def git_ignored(root: Path, paths: list[str]) -> set[str] | None:
    """Which of `paths` Git would ignore in this repo (tracked files never are), or None outside Git."""
    if not paths:
        return set()
    proc = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
        input="\0".join(paths).encode(),
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        return None
    return {p for p in proc.stdout.decode().split("\0") if p}


def read_manifest(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return set()


class Remote:
    def __init__(self, ip: str, key: Path, known_hosts: Path, pinned: bool = False):
        if not ip:
            raise DevboxError("server has no IPv4 address")
        self.ip = ip
        self.key = key
        self.known_hosts = known_hosts
        # Pinned: known_hosts holds the host key devbox gave the server, and nothing else is trusted.
        self.pinned = pinned

    def ssh_opts(self, connect_timeout: int = 10) -> list[str]:
        return [
            "-i", str(self.key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            *(("-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519") if self.pinned
              else ("-o", "StrictHostKeyChecking=accept-new")),
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

        The command always runs in its own systemd scope (`devbox-exec-*`), so
        `idle-check` can see that work is in progress. With a timeout the scope
        gets RuntimeMaxSec, so the server itself stops it (SIGTERM to every
        process in the scope, SIGKILL after 10s) even if the SSH connection is
        lost. Whether the deadline was hit is read back from the scope's result.
        """
        secs = max(1, math.ceil(timeout)) if timeout is not None else None
        unit = f"{EXEC_UNIT_PREFIX}{secrets.token_hex(4)}"
        # No --collect: a scope stopped by RuntimeMaxSec stays loaded as failed
        # with Result=timeout until reset-failed, so it can be checked below.
        limits = f"-p RuntimeMaxSec={secs}s -p TimeoutStopSec={EXEC_KILL_AFTER}s " if secs else ""
        remote = (
            f"cd {APP_DIR} && systemd-run --scope --quiet --unit {unit} {limits}"
            f"--setenv=HOME=/root bash -lc {shlex.quote(script)}"
        )
        try:
            proc = self._ssh(
                remote, capture=capture, check=False,
                timeout=secs + EXEC_KILL_AFTER + 30 if secs else None,
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
                after = "will be stopped at the deadline" if secs else f"keeps running until it ends (stop it with `systemctl stop {unit}.scope` on the server)"
                raise DevboxError(
                    f"lost the connection; the command is still running on the server and {after} (scope {unit})"
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

    # Startup stages for `up`. All of them stop at the shared deadline.

    def wait_ssh(self, deadline: Deadline, progress: Progress, sleep=time.sleep) -> None:
        progress.step("ssh", f"Waiting for SSH on {self.ip}...")
        while True:
            limit = deadline.bound("ssh", 15)
            try:
                proc = subprocess.run(
                    self.ssh_cmd("true", connect_timeout=max(1, min(10, int(limit)))),
                    capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL,
                )
                if proc.returncode == 0:
                    progress.say("SSH connected")
                    return
                if self.pinned and HOST_KEY_MISMATCH_RE.search(proc.stderr or ""):
                    # Before cloud-init installs devbox's host key, sshd may still use the
                    # image's or snapshot's key. Never accept it; wait for the right one.
                    progress.waiting(f"Waiting for SSH on {self.ip}; the server doesn't present devbox's host key yet")
                    sleep(max(0.0, min(5.0, deadline.remaining())))
                    deadline.check("ssh")
                    continue
            except subprocess.TimeoutExpired:
                pass
            progress.waiting(f"Waiting for SSH on {self.ip}; not reachable yet")
            sleep(max(0.0, min(5.0, deadline.remaining())))
            deadline.check("ssh")

    def wait_cloud_init(self, deadline: Deadline, progress: Progress, secrets=(), sleep=time.sleep) -> list[str]:
        """Wait for first-boot setup. Fails at once if cloud-init reports an error."""
        progress.step("cloud-init", "Waiting for cloud-init (server setup)...")
        while True:
            limit = deadline.bound("cloud-init", 20)
            try:
                proc = self._ssh(CLOUD_INIT_POLL, check=False, timeout=limit)
            except DevboxError as e:
                deadline.check("cloud-init")
                progress.waiting(f"Waiting for cloud-init; could not ask the server ({e})")
                sleep(max(0.0, min(5.0, deadline.remaining())))
                continue
            status, code, last = parse_cloud_init_poll(proc.stdout)
            if code == 1 or status == "error":
                detail = self._ssh("cloud-init status --long", check=False, timeout=deadline.bound("cloud-init", 15)).stdout
                raise StageFailed("cloud-init", "cloud-init reported an error:\n" + redact(detail.strip(), secrets))
            if status in ("done", "disabled"):
                progress.say("cloud-init finished")
                if code == 2:
                    return ["cloud-init finished with recoverable errors; see `cloud-init status --long` on the server"]
                return []
            note = redact(last, secrets)[:140] if last else None
            progress.waiting(
                f"cloud-init {status or 'state unknown'}; last output: {note}" if note
                else f"cloud-init {status or 'state unknown'}; no further progress reported"
            )
            sleep(max(0.0, min(5.0, deadline.remaining())))
            deadline.check("cloud-init")

    def verify_tools(self, deadline: Deadline, progress: Progress) -> None:
        """Make sure first-boot setup actually installed what devbox relies on."""
        progress.step("tools", "Checking installed tools...")
        proc = self._ssh(TOOLS_CHECK, check=False, timeout=deadline.bound("tools", 30))
        missing = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if missing:
            raise StageFailed("tools", f"server setup finished but these tools are missing: {', '.join(missing)}")

    def collect_diagnostics(self, timeout: float = 20) -> str:
        """Recent setup output from the server, for failure reports. Bounded by `timeout`."""
        proc = self._ssh(DIAGNOSTICS, check=False, timeout=timeout)
        return proc.stdout + (proc.stderr or "")

    def sync(self, local_path: str, manifest: Path, timeout: float | None = None) -> dict:
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
            proc = _run_bounded(
                ["rsync", "-az", "--exclude", ".git", "-e", ssh, f"{root}/", dest],
                timeout, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise DevboxError(f"rsync failed: {proc.stderr.strip()}")
            return {"mode": "copy", "files": None, "deleted": 0,
                    "warning": "not a Git repository: copied everything except .git, deleted nothing"}

        previous = read_manifest(manifest)
        current = set(files)
        to_delete = sorted(f for f in previous - current if not os.path.lexists(root / f))

        proc = _run_bounded(
            ["rsync", "-az", "--from0", "--files-from=-", "-e", ssh, f"{root}/", dest],
            timeout,
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

    # Files on the server

    def read_file(self, path: str) -> str | None:
        """Text of a file in the project folder, or None if it does not exist."""
        quoted = shlex.quote(path)
        proc = self.run(f"if [ -f {quoted} ]; then cat {quoted}; else exit 3; fi", check=False)
        if proc.returncode == 3:
            return None
        if proc.returncode != 0:
            raise DevboxError(f"cannot read {path} on the server: {(proc.stderr or '').strip()}")
        return proc.stdout

    def write_file(self, path: str, content: str, mode: str = "600") -> None:
        """Replace a file in the project folder atomically, creating parent folders."""
        quoted = shlex.quote(path)
        tmp = shlex.quote(path + ".devbox-tmp")
        self.run(
            f"mkdir -p \"$(dirname {quoted})\" && umask 077 && cat > {tmp} && chmod {mode} {tmp} && mv {tmp} {quoted}",
            input=content,
        )

    def stream_out(self, script: str, dest: Path) -> int:
        """Run a command in the project folder and save its stdout to `dest`. Returns bytes written."""
        remote = f"cd {APP_DIR} && bash -o pipefail -lc {shlex.quote(script)}"
        tmp = dest.with_name(dest.name + ".partial")
        tmp.unlink(missing_ok=True)
        try:
            # Dumps often hold credentials and personal data: owner-only, whatever the umask.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as out:
                proc = subprocess.run(self.ssh_cmd(remote), stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            if proc.returncode != 0:
                detail = proc.stderr.decode(errors="replace").strip()
                raise DevboxError(f"remote command failed (exit {proc.returncode}){': ' + detail if detail else ''}")
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest.stat().st_size

    def stream_in(self, script: str, src: Path) -> None:
        """Run a command in the project folder with `src` as its stdin."""
        remote = f"cd {APP_DIR} && bash -o pipefail -lc {shlex.quote(script)}"
        with open(src, "rb") as f:
            proc = subprocess.run(self.ssh_cmd(remote), stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            raise DevboxError(f"remote command failed (exit {proc.returncode}){': ' + detail if detail else ''}")

    def list_files(self, prune: tuple[str, ...] = PULL_PRUNE) -> list[str]:
        """Regular files in the project folder, relative paths, skipping pruned folder names."""
        prune_expr = " -o ".join(f"-name {shlex.quote(p)}" for p in prune)
        script = f"find . \\( {prune_expr} \\) -prune -o -type f -print0"
        proc = self._ssh(f"cd {APP_DIR} && {script}", timeout=300)
        return sorted(f[2:] if f.startswith("./") else f for f in proc.stdout.split("\0") if f)

    def pull(self, files: list[str], staging: Path, compare_dest: Path) -> list[str]:
        """Copy the given server files that differ from `compare_dest` into `staging`."""
        staging.mkdir(parents=True, exist_ok=True)
        ssh = shlex.join(["ssh", *self.ssh_opts()])
        proc = subprocess.run(
            [
                "rsync", "-a", "--checksum", "--from0", "--files-from=-",
                f"--compare-dest={compare_dest.resolve()}/", "-e", ssh,
                f"root@{self.ip}:{APP_DIR}/", f"{staging}/",
            ],
            input="\0".join(files).encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            raise DevboxError(f"rsync failed: {proc.stderr.decode(errors='replace').strip()}")
        # rsync creates folders for skipped files too; drop the empty ones.
        for dirpath, dirnames, filenames in os.walk(staging, topdown=False):
            if Path(dirpath) != staging and not os.listdir(dirpath):
                os.rmdir(dirpath)
        return sorted(
            str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file() or p.is_symlink()
        )

    def forward_cmd(self, ports: list[tuple[int, int]]) -> list[str]:
        """ssh command that forwards local ports to ports on the server until stopped."""
        cmd = ["ssh", *self.ssh_opts(), "-N", "-o", "ExitOnForwardFailure=yes"]
        for local, remote in ports:
            cmd += ["-L", f"127.0.0.1:{local}:localhost:{remote}"]
        return [*cmd, f"root@{self.ip}"]

    # Background processes (systemd units on the server)

    def start_unit(self, name: str, script: str, keep_alive: bool = False) -> None:
        """Start a background process. Keep-alive processes block idle shutdown while running."""
        unit = UNIT_PREFIX + name
        description = f"{KEEP_ALIVE_DESCRIPTION} {name}" if keep_alive else f"devbox {name}"
        self.run(
            f"systemctl stop {unit} 2>/dev/null; systemctl reset-failed {unit} 2>/dev/null; "
            f"systemd-run --quiet --unit {unit} --description {shlex.quote(description)} "
            f"--working-directory={APP_DIR} --setenv=HOME=/root "
            f"bash -lc {shlex.quote(script)}",
            cwd=None,
        )

    def running_work(self) -> list[dict]:
        """Foreground commands and keep-alive background jobs currently running on the server."""
        proc = self.run(
            f"systemctl list-units '{UNIT_PREFIX}*' --state=active --no-legend --plain --no-pager",
            cwd=None,
            timeout=30,
        )
        return busy_units(proc.stdout)

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

    def _wait_journal(self, name: str, pattern: re.Pattern, timeout: float) -> re.Match | None:
        """Wait for `pattern` in the output of the current run of a background process."""
        unit = UNIT_PREFIX + name
        script = (
            f"id=$(systemctl show -p InvocationID --value {unit}); "
            f"journalctl _SYSTEMD_INVOCATION_ID=$id -o cat --no-pager"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = pattern.search(self.run(script, check=False, cwd=None).stdout)
            if match:
                return match
            time.sleep(2)
        return None

    def start_tunnel(self, port: int, unit: str = TUNNEL, timeout: float = 60) -> str:
        """Cloudflare quick tunnel to a local port; returns its random trycloudflare.com URL."""
        self.start_unit(unit, f"exec cloudflared tunnel --no-autoupdate --url http://localhost:{port}")
        match = self._wait_journal(unit, TUNNEL_URL_RE, timeout)
        if match:
            return match.group(0)
        raise DevboxError(f"no tunnel URL after {int(timeout)}s; see `devbox logs <project> {unit}`")

    def unit_active(self, name: str) -> bool:
        return self.run(f"systemctl is-active --quiet {UNIT_PREFIX + name}", check=False, cwd=None).returncode == 0

    def start_named_tunnel(self, unit: str, token: str, timeout: float = 60) -> None:
        """Run the connector of a remotely configured Cloudflare tunnel (once per server)."""
        if self.unit_active(unit):
            return
        self.write_file(TUNNEL_TOKEN_FILE, token)
        self.start_unit(unit, f"TUNNEL_TOKEN=$(cat {TUNNEL_TOKEN_FILE}) exec cloudflared tunnel --no-autoupdate run")
        if not self._wait_journal(unit, TUNNEL_REGISTERED_RE, timeout):
            raise DevboxError(f"the tunnel did not connect within {int(timeout)}s; see `devbox logs <project> {unit}`")

    def wait_http(self, port: int, path: str, timeout: float, accept: str = "2|3") -> tuple[bool, str]:
        """Poll http://localhost:PORT/PATH on the server until the status code starts with one of
        `accept` (e.g. "2|3"). Returns (ok, last status code or "none" when nothing answered)."""
        url = shlex.quote(f"http://localhost:{port}{path}")
        prefixes = "|".join(f"{a}*" for a in accept.split("|"))
        script = (
            f"end=$((SECONDS+{int(timeout)})); while :; do "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {url}); "
            f"case $code in {prefixes}) echo \"ok $code\"; exit 0;; esac; "
            f"[ $SECONDS -ge $end ] && {{ echo \"fail $code\"; exit 1; }}; sleep 2; done"
        )
        proc = self.run(script, check=False, cwd=None, timeout=timeout + 30)
        words = (proc.stdout or "").split()
        code = words[1] if len(words) > 1 and words[1] != "000" else "none"
        return proc.returncode == 0, code

    def free_port(self) -> int:
        proc = self.run(
            "python3 -c 'import socket; s = socket.socket(); s.bind((\"127.0.0.1\", 0)); print(s.getsockname()[1])'",
            cwd=None,
        )
        return int(proc.stdout.strip())

    def start_auth_proxy(self, unit: str, port: int, password: str, timeout: float = 120) -> int:
        """Caddy with basic auth in front of a local port; returns the port it listens on."""
        from devbox import previews

        pulled = self.run(f"docker pull -q {previews.CADDY_IMAGE}", check=False, cwd=None, timeout=timeout)
        if pulled.returncode != 0:
            raise DevboxError(f"cannot pull {previews.CADDY_IMAGE} for the password proxy: {(pulled.stderr or '').strip()}")
        hashed = self.run(
            f"docker run --rm {previews.CADDY_IMAGE} caddy hash-password --plaintext \"$(cat)\"",
            input=password, cwd=None, timeout=60,
        ).stdout.strip().splitlines()
        hashed = hashed[-1] if hashed else ""
        if not hashed.startswith("$2"):
            raise DevboxError(f"could not hash the preview password with caddy (got '{hashed[:40]}')")
        listen = self.free_port()
        config = f"{DEVBOX_DIR}/{unit}.Caddyfile"
        self.write_file(config, previews.caddyfile(listen, port, hashed), mode="644")
        container = UNIT_PREFIX + unit
        self.start_unit(unit, (
            f"docker rm -f {container} >/dev/null 2>&1; exec docker run --rm --name {container} "
            f"--network host -v {config}:/etc/caddy/Caddyfile:ro {previews.CADDY_IMAGE}"
        ))
        ok, code = self.wait_http(listen, "/", 30, accept="401")
        if not ok:
            raise DevboxError(f"the password proxy did not start (got {code}); see `devbox logs <project> {unit}`")
        return listen


def parse_units(output: str) -> list[dict]:
    """Background processes (services) from `systemctl list-units` output."""
    units = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].startswith(UNIT_PREFIX) or not parts[0].endswith(".service"):
            continue
        unit, _load, active, sub = parts[:4]
        description = parts[4] if len(parts) > 4 else ""
        name = unit.removeprefix(UNIT_PREFIX).removesuffix(".service")
        units.append({
            "name": name, "active": active, "sub": sub,
            "keep_alive": description.startswith(KEEP_ALIVE_DESCRIPTION),
        })
    return units


def busy_units(output: str) -> list[dict]:
    """Active work that must not be interrupted: exec scopes and keep-alive jobs."""
    busy = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or parts[2] != "active":
            continue
        unit = parts[0]
        description = parts[4] if len(parts) > 4 else ""
        if unit.startswith(EXEC_UNIT_PREFIX) and unit.endswith(".scope"):
            busy.append({"unit": unit, "kind": "command"})
        elif unit.endswith(".service") and description.startswith(KEEP_ALIVE_DESCRIPTION):
            busy.append({"unit": unit, "kind": "keep-alive job"})
    return busy
