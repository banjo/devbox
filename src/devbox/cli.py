import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from devbox import DevboxError, config, db
from devbox.hetzner import Hcloud
from devbox.remote import TUNNEL, Remote, to_script, validate_proc


class Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._cfg = None
        self._conn = None
        self._hcloud = None

    @property
    def cfg(self) -> config.Config:
        if self._cfg is None:
            self._cfg = config.load()
        return self._cfg

    @property
    def conn(self):
        if self._conn is None:
            self._conn = db.connect(self.cfg.db_path)
        return self._conn

    @property
    def hcloud(self) -> Hcloud:
        if self._hcloud is None:
            self._hcloud = Hcloud(self.cfg.hcloud_token())
        return self._hcloud

    def known_hosts(self, server) -> Path:
        path = self.cfg.state_dir / "known_hosts" / f"{server['project']}-{server['hetzner_id']}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def remote(self, server) -> Remote:
        return Remote(server["ip"], self.cfg.ssh_key, self.known_hosts(server))

    def running_server(self, project: str):
        db.require_project(self.conn, project)
        server = db.active_server(self.conn, project)
        if server is None:
            raise DevboxError(f"project '{project}' has no server; start one with `devbox up {project}`")
        if server["status"] != "running":
            raise DevboxError(f"server for '{project}' is still being set up; run `devbox up {project}` again")
        return server

    def reconcile(self) -> list[dict]:
        return db.reconcile(self.conn, self.hcloud.list_servers())

    @contextlib.contextmanager
    def lock(self):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.state_dir / "lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("waiting for another devbox command to finish...", file=sys.stderr)
                fcntl.flock(f, fcntl.LOCK_EX)
            yield


def emit(args, data, text: str | None = None) -> None:
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif text is not None:
        print(text)


def row(r) -> dict | None:
    return dict(r) if r is not None else None


def uptime(created_at: str | None) -> str | None:
    if not created_at:
        return None
    started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    secs = int((datetime.now(timezone.utc) - started).total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# Projects


def cmd_project_add(ctx: Context) -> int:
    path = Path(ctx.args.path).expanduser().resolve()
    if not path.is_dir():
        raise DevboxError(f"{path} is not a directory")
    db.add_project(ctx.conn, ctx.args.name, str(path))
    emit(ctx.args, {"name": ctx.args.name, "path": str(path)}, f"added {ctx.args.name} ({path})")
    return 0


def cmd_project_list(ctx: Context) -> int:
    projects = []
    for p in db.list_projects(ctx.conn):
        server = db.active_server(ctx.conn, p["name"])
        projects.append({
            "name": p["name"],
            "path": p["path"],
            "has_notes": bool(p["notes"].strip()),
            "server": server["status"] if server else None,
        })
    lines = [
        f"{p['name']:<20} {p['server'] or '-':<10} {'notes' if p['has_notes'] else 'no notes':<9} {p['path']}"
        for p in projects
    ]
    emit(ctx.args, projects, "\n".join(lines) or "no projects")
    return 0


def cmd_project_notes(ctx: Context) -> int:
    name = ctx.args.name
    if ctx.args.set is not None or ctx.args.set_file is not None:
        if ctx.args.set_file == "-":
            notes = sys.stdin.read()
        elif ctx.args.set_file is not None:
            notes = Path(ctx.args.set_file).read_text()
        else:
            notes = ctx.args.set
        db.set_notes(ctx.conn, name, notes.strip() + "\n")
        emit(ctx.args, {"name": name, "updated": True}, f"notes for {name} updated")
        return 0

    project = db.require_project(ctx.conn, name)
    notes = project["notes"]
    emit(
        ctx.args,
        {"name": name, "notes": notes, "updated_at": project["updated_at"]},
        notes.rstrip() or f"no notes for {name}",
    )
    return 0


def cmd_project_remove(ctx: Context) -> int:
    db.remove_project(ctx.conn, ctx.args.name)
    emit(ctx.args, {"name": ctx.args.name, "removed": True}, f"removed {ctx.args.name}")
    return 0


# Servers


def ensure_local_ssh_key(key: Path) -> str:
    pub = key.with_name(key.name + ".pub")
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "devbox", "-f", str(key)],
            check=True,
        )
    return pub.read_text().strip()


def resolve_ssh_allow(entries: list[str]) -> list[str]:
    cidrs = []
    for entry in entries:
        if entry == "auto":
            try:
                with urllib.request.urlopen("https://api.ipify.org", timeout=10) as resp:
                    ip = resp.read().decode().strip()
            except OSError as e:
                raise DevboxError(f"could not detect this host's public IP (set ssh_allow in config): {e}")
            cidrs.append(f"{ip}/32")
        else:
            cidrs.append(entry)
    return cidrs


def cmd_up(ctx: Context) -> int:
    name = ctx.args.name
    cfg = ctx.cfg
    with ctx.lock():
        project = db.require_project(ctx.conn, name)
        orphans = ctx.reconcile()
        server = db.active_server(ctx.conn, name)
        created = False

        if server is None:
            others = [s["project"] for s in db.active_servers(ctx.conn)]
            others += [o["name"] for o in orphans]
            if others and not ctx.args.force:
                raise DevboxError(
                    f"another devbox server is running ({', '.join(others)}); "
                    "stop it with `devbox down` or pass --force"
                )
            public_key = ensure_local_ssh_key(cfg.ssh_key)
            ctx.hcloud.ensure_ssh_key(public_key)
            ctx.hcloud.ensure_firewall(resolve_ssh_allow(cfg.ssh_allow))

            print(f"creating {cfg.server_type} in {cfg.location} for {name}...", file=sys.stderr)
            user_data = resources.files("devbox").joinpath("cloud-init.yaml").read_text()
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                f.write(user_data)
                f.flush()
                info = ctx.hcloud.create_server(
                    f"devbox-{name}", name, cfg.server_type, cfg.image, cfg.location, Path(f.name)
                )
            db.insert_server(
                ctx.conn, name, info["id"], "creating",
                ip=info["ip"], server_type=info["server_type"], location=info["location"],
            )
            server = db.active_server(ctx.conn, name)
            created = True

        remote = ctx.remote(server)
        if server["status"] != "running":
            print(f"waiting for {server['ip']} to finish setup...", file=sys.stderr)
        warnings = remote.wait_ready()
        db.update_server(ctx.conn, server["id"], status="running")

        print("syncing code...", file=sys.stderr)
        remote.sync(project["path"])
        server = db.active_server(ctx.conn, name)

    data = {
        "project": name,
        "created": created,
        "server": row(server),
        "has_notes": bool(project["notes"].strip()),
        "warnings": warnings,
    }
    text = [
        f"{name}: {'created' if created else 'reusing'} server {server['hetzner_id']} at {server['ip']}, code synced",
        *(f"warning: {w}" for w in warnings),
    ]
    if not data["has_notes"]:
        text.append(f"no notes for {name} yet; work out how to start it, then save them with `devbox project notes {name} --set ...`")
    emit(ctx.args, data, "\n".join(text))
    return 0


def cmd_down(ctx: Context) -> int:
    name = ctx.args.name
    with ctx.lock():
        db.require_project(ctx.conn, name)
        ctx.reconcile()
        server = db.active_server(ctx.conn, name)
        if server is None:
            emit(ctx.args, {"project": name, "deleted": None}, f"{name} has no server")
            return 0
        ctx.hcloud.delete_server(server["hetzner_id"])
        db.mark_deleted(ctx.conn, server["id"])
        ctx.known_hosts(server).unlink(missing_ok=True)
    emit(
        ctx.args,
        {"project": name, "deleted": server["hetzner_id"]},
        f"deleted server {server['hetzner_id']} for {name}",
    )
    return 0


def cmd_status(ctx: Context) -> int:
    orphans = ctx.reconcile()
    if ctx.args.name:
        projects = [db.require_project(ctx.conn, ctx.args.name)]
    else:
        projects = db.list_projects(ctx.conn)

    result = []
    for p in projects:
        server = db.active_server(ctx.conn, p["name"])
        entry = {"project": p["name"], "server": row(server), "processes": None}
        if server:
            entry["uptime"] = uptime(server["created_at"])
            if server["status"] == "running":
                try:
                    entry["processes"] = ctx.remote(server).list_units()
                except DevboxError as e:
                    entry["processes_error"] = str(e)
        result.append(entry)

    lines = []
    for e in result:
        s = e["server"]
        if not s:
            if ctx.args.name:
                lines.append(f"{e['project']}: no server")
            continue
        lines.append(
            f"{e['project']}: {s['status']} {s['server_type']} {s['location']} {s['ip']} "
            f"(id {s['hetzner_id']}, up {e['uptime']})"
        )
        if s["preview_url"]:
            lines.append(f"  preview: {s['preview_url']} -> port {s['preview_port']}")
        for proc in e["processes"] or []:
            lines.append(f"  {proc['name']}: {proc['active']} ({proc['sub']})")
        if "processes_error" in e:
            lines.append(f"  processes: {e['processes_error']}")
    for o in orphans:
        lines.append(f"unregistered devbox server {o['name']} (id {o['id']}, {o['ip']})")
    if not lines:
        lines.append("no devbox servers")
    emit(ctx.args, {"projects": result, "unregistered": orphans}, "\n".join(lines))
    return 0


def cmd_sync(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    project = db.require_project(ctx.conn, ctx.args.name)
    ctx.remote(server).sync(project["path"])
    emit(ctx.args, {"project": ctx.args.name, "synced": True}, "synced")
    return 0


def cmd_exec(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    script = to_script(ctx.args.cmd)
    remote = ctx.remote(server)

    if ctx.args.detach:
        if not ctx.args.proc:
            raise DevboxError("--detach needs --name PROC")
        validate_proc(ctx.args.proc)
        if ctx.args.proc == TUNNEL:
            raise DevboxError(f"'{TUNNEL}' is reserved for the preview tunnel; use `devbox expose`")
        remote.start_unit(ctx.args.proc, script)
        emit(
            ctx.args,
            {"project": ctx.args.name, "process": ctx.args.proc, "started": True},
            f"started {ctx.args.proc}; follow it with `devbox logs {ctx.args.name} {ctx.args.proc} -f`",
        )
        return 0

    if ctx.args.json:
        proc = remote.run(script, capture=True, check=False, timeout=ctx.args.timeout)
        emit(ctx.args, {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        return proc.returncode
    proc = remote.run(script, capture=False, check=False, timeout=ctx.args.timeout)
    return proc.returncode


def cmd_ps(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    units = ctx.remote(server).list_units()
    lines = [f"{u['name']:<20} {u['active']:<10} {u['sub']}" for u in units]
    emit(ctx.args, units, "\n".join(lines) or "no background processes")
    return 0


def cmd_stop(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    validate_proc(ctx.args.proc)
    stopped = ctx.remote(server).stop_unit(ctx.args.proc)
    if ctx.args.proc == TUNNEL and stopped:
        db.update_server(ctx.conn, server["id"], preview_port=None, preview_url=None)
    emit(
        ctx.args,
        {"process": ctx.args.proc, "stopped": stopped},
        f"stopped {ctx.args.proc}" if stopped else f"{ctx.args.proc} was not running",
    )
    return 0


def cmd_logs(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    validate_proc(ctx.args.proc)
    return ctx.remote(server).logs(ctx.args.proc, ctx.args.lines, ctx.args.follow)


def cmd_expose(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    port = ctx.args.port
    if not 1 <= port <= 65535:
        raise DevboxError(f"invalid port {port}")
    url = ctx.remote(server).start_tunnel(port)
    db.update_server(ctx.conn, server["id"], preview_port=port, preview_url=url)
    emit(ctx.args, {"project": ctx.args.name, "port": port, "url": url}, url)
    return 0


def cmd_ssh(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    remote = ctx.remote(server)
    cmd = remote.ssh_cmd(tty=True)
    os.execvp(cmd[0], cmd)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")

    parser = argparse.ArgumentParser(
        prog="devbox", description="Temporary Hetzner development servers.", parents=[common]
    )
    parser.set_defaults(json=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help, **kw):
        p = sub.add_parser(name, help=help, parents=[common], **kw)
        p.set_defaults(func=func)
        return p

    proj = sub.add_parser("project", help="manage projects", parents=[common])
    proj_sub = proj.add_subparsers(dest="project_command", required=True)

    p = proj_sub.add_parser("add", help="register a project", parents=[common])
    p.add_argument("name")
    p.add_argument("--path", required=True, help="local project folder")
    p.set_defaults(func=cmd_project_add)

    p = proj_sub.add_parser("list", help="list projects", parents=[common])
    p.set_defaults(func=cmd_project_list)

    p = proj_sub.add_parser("notes", help="show or replace a project's notes", parents=[common])
    p.add_argument("name")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--set", metavar="TEXT")
    group.add_argument("--set-file", metavar="FILE", help="read notes from FILE ('-' for stdin)")
    p.set_defaults(func=cmd_project_notes)

    p = proj_sub.add_parser("remove", help="forget a project", parents=[common])
    p.add_argument("name")
    p.set_defaults(func=cmd_project_remove)

    p = add("up", cmd_up, "create or reuse the server and sync code")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="allow a second devbox server")

    p = add("down", cmd_down, "delete the server")
    p.add_argument("name")

    p = add("status", cmd_status, "show servers, processes and preview URLs")
    p.add_argument("name", nargs="?")

    p = add("sync", cmd_sync, "copy local code to the server")
    p.add_argument("name")

    p = add(
        "exec", cmd_exec, "run a command on the server",
        description="Run a command in /srv/app. A single argument is run as a shell script; "
        "several arguments are quoted as one command.",
    )
    p.add_argument("name")
    p.add_argument("--detach", action="store_true", help="run in the background as a named process")
    p.add_argument("--name", dest="proc", metavar="PROC", help="process name for --detach")
    p.add_argument("--timeout", type=float, help="seconds before giving up (foreground only)")
    p.epilog = "usage: devbox exec NAME [--detach --name PROC] -- CMD..."

    p = add("ps", cmd_ps, "list background processes")
    p.add_argument("name")

    p = add("stop", cmd_stop, "stop a background process")
    p.add_argument("name")
    p.add_argument("proc")

    p = add("logs", cmd_logs, "show output from a background process")
    p.add_argument("name")
    p.add_argument("proc")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=200)

    p = add("expose", cmd_expose, "open a public preview URL to a port on the server")
    p.add_argument("name")
    p.add_argument("port", type=int)

    p = add("ssh", cmd_ssh, "open a shell on the server")
    p.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` is the remote command for `exec`.
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.cmd = cmd
    ctx = Context(args)
    try:
        return args.func(ctx)
    except DevboxError as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
