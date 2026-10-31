import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from devbox import DevboxError, config, costs, db
from devbox.hetzner import Hcloud, filter_offers
from devbox.remote import TUNNEL, Remote, to_script, validate_proc


EXIT_TIMEOUT = 124


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
            self._hcloud = Hcloud(self.cfg.hcloud_token(), self.cfg.get_controller_id())
        return self._hcloud

    def known_hosts(self, server) -> Path:
        path = self.cfg.state_dir / "known_hosts" / f"{server['project']}-{server['hetzner_id']}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def manifest(self, server) -> Path:
        return self.cfg.state_dir / "manifests" / f"{server['project']}-{server['hetzner_id']}.json"

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
        orphans = db.reconcile(self.conn, self.hcloud.list_servers())
        self.fill_missing_prices()
        return orphans

    def fill_missing_prices(self) -> None:
        """Servers adopted by reconcile (or created before prices were stored) get today's price."""
        missing = [s for s in db.servers_missing_price(self.conn) if s["status"] != "deleted"]
        if not missing:
            return
        try:
            offers = self.hcloud.offers()
        except DevboxError:
            return
        prices = {(o["type"], o["location"]): o for o in offers}
        for s in missing:
            o = prices.get((s["server_type"], s["location"]))
            if o:
                db.update_server(
                    self.conn, s["id"], price_hourly=o["price_hourly"], price_monthly=o["price_monthly"]
                )

    def event(self, server, kind: str) -> None:
        db.add_event(self.conn, server["project"], server["id"], kind)

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
    started = costs.parse_ts(created_at)
    return costs.duration((datetime.now(timezone.utc) - started).total_seconds())


def cost_line(usage: dict, server) -> str:
    rate = f"€{server['price_hourly']:.4f}/h" if server["price_hourly"] is not None else "price unknown"
    return f"{costs.money(usage['cost'])} so far ({usage['billed_hours']} billed h at {rate})"


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


def describe_offer(o: dict) -> str:
    where = ", ".join(x for x in (o["city"], o["country"]) if x)
    price = f"€{o['price_monthly']:.2f}/mo" if o["price_monthly"] is not None else "price ?"
    return (
        f"{o['type']} in {o['location']} ({where}): {o['cores']} vCPU, {o['memory_gb']:g} GB RAM, "
        f"{o['disk_gb']} GB disk, {o['architecture']}, {price}"
    )


def choose_offer(ctx: Context) -> dict:
    server_type = ctx.args.type or ctx.cfg.server_type
    location = ctx.args.location or ctx.cfg.location
    if not server_type or not location:
        raise DevboxError(
            "no server chosen: run `devbox server-types` and pass --type and --location"
        )
    offers = ctx.hcloud.offers()
    for o in offers:
        if o["type"] == server_type and o["location"] == location:
            if o["available"] is False:
                break
            return o
    else:
        if not any(o["type"] == server_type for o in offers):
            raise DevboxError(f"unknown server type '{server_type}'; see `devbox server-types`")

    wanted = next((o for o in offers if o["type"] == server_type), None)
    alternatives = filter_offers(
        offers,
        min_cores=wanted["cores"] if wanted else None,
        min_memory=wanted["memory_gb"] if wanted else None,
    )[:5]
    lines = [f"{server_type} is not available in {location}. Cheapest available alternatives:"]
    lines += [f"  {describe_offer(o)}" for o in alternatives] or ["  none found"]
    raise DevboxError("\n".join(lines))


def cmd_server_types(ctx: Context) -> int:
    a = ctx.args
    offers = filter_offers(
        ctx.hcloud.offers(),
        min_cores=a.min_cores,
        min_memory=a.min_memory,
        architecture=a.arch,
        location=a.location,
        network_zone=a.zone,
        include_unavailable=a.all,
    )
    lines = [
        f"{'TYPE':<8} {'LOC':<5} {'ZONE':<13} {'CPU':>3} {'RAM':>5} {'DISK':>5} {'ARCH':<5} {'€/MONTH':>8} {'€/HOUR':>7}  NOTE"
    ]
    for o in offers:
        note = []
        if o["available"] is False:
            note.append("unavailable")
        elif o["available"] is None:
            note.append("availability unknown")
        if o["recommended"]:
            note.append("recommended")
        monthly = f"{o['price_monthly']:.2f}" if o["price_monthly"] is not None else "?"
        hourly = f"{o['price_hourly']:.4f}" if o["price_hourly"] is not None else "?"
        lines.append(
            f"{o['type']:<8} {o['location']:<5} {o['network_zone'] or '?':<13} {o['cores']:>3} "
            f"{o['memory_gb']:>4g}G {o['disk_gb']:>4}G {o['architecture']:<5} {monthly:>8} {hourly:>7}  {', '.join(note)}"
        )
    emit(ctx.args, offers, "\n".join(lines) if offers else "no matching server types")
    return 0


def cmd_up(ctx: Context) -> int:
    name = ctx.args.name
    cfg = ctx.cfg
    offer = None
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
            offer = choose_offer(ctx)
            public_key = ensure_local_ssh_key(cfg.ssh_key)
            ssh_key_id = ctx.hcloud.ensure_ssh_key(public_key)
            firewall_id = ctx.hcloud.ensure_firewall(resolve_ssh_allow(cfg.ssh_allow))

            print(f"creating {describe_offer(offer)} for {name}...", file=sys.stderr)
            user_data = resources.files("devbox").joinpath("cloud-init.yaml").read_text()
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                f.write(user_data)
                f.flush()
                info = ctx.hcloud.create_server(
                    f"devbox-{name}", name, offer["type"], cfg.image, offer["location"], Path(f.name),
                    ssh_key_id=ssh_key_id, firewall_id=firewall_id,
                )
            db.insert_server(
                ctx.conn, name, info["id"], "creating",
                ip=info["ip"], server_type=info["server_type"] or offer["type"],
                location=info["location"] or offer["location"], created_at=info.get("created"),
                price_hourly=offer["price_hourly"], price_monthly=offer["price_monthly"],
            )
            server = db.active_server(ctx.conn, name)
            created = True
            ctx.event(server, "create")

        remote = ctx.remote(server)
        if server["status"] != "running":
            print(f"waiting for {server['ip']} to finish setup...", file=sys.stderr)
        warnings = remote.wait_ready()
        db.update_server(ctx.conn, server["id"], status="running")

        print("syncing code...", file=sys.stderr)
        sync = remote.sync(project["path"], ctx.manifest(server))
        if sync["warning"]:
            warnings.append(sync["warning"])
        ctx.event(server, "up")
        ctx.event(server, "sync")
        server = db.active_server(ctx.conn, name)

    data = {
        "project": name,
        "created": created,
        "server": row(server),
        "has_notes": bool(project["notes"].strip()),
        "offer": offer,
        "warnings": warnings,
    }
    text = [
        f"{name}: {'created' if created else 'reusing'} server {server['hetzner_id']} "
        f"({server['server_type']} in {server['location']}) at {server['ip']}, code synced",
        *(f"warning: {w}" for w in warnings),
    ]
    if not data["has_notes"]:
        text.append(f"no notes for {name} yet; work out how to start it, then save them with `devbox project notes {name} --set ...`")
    emit(ctx.args, data, "\n".join(text))
    return 0


def server_summary(ctx: Context, server) -> dict:
    """Usage, cost and activity for one server over its whole lifetime."""
    usage = costs.server_usage(server)
    project_total = costs.summarize(db.all_servers(ctx.conn, server["project"]))
    return {
        "server": row(server),
        "uptime_seconds": usage["seconds"],
        "billed_hours": usage["billed_hours"],
        "cost": usage["cost"],
        "currency": costs.CURRENCY,
        "activity": db.event_counts(ctx.conn, server["id"]),
        "project_total": project_total["total"],
        "project_servers": project_total["servers"],
    }


ACTIVITY_LABELS = [
    ("sync", "sync"), ("exec", "exec"), ("exec_detach", "background start"),
    ("expose", "expose"), ("up", "up"),
]


def summary_lines(summary: dict) -> list[str]:
    s = summary["server"]
    rate = f"€{s['price_hourly']:.4f}/h" if s["price_hourly"] is not None else "unknown price"
    lines = [
        f"  {s['server_type']} in {s['location']}, up {costs.duration(summary['uptime_seconds'])} "
        f"({summary['billed_hours']} billed hours at {rate})",
        f"  cost: {costs.money(summary['cost'])} (estimate, server only, incl. VAT)",
    ]
    counts = summary["activity"]
    parts = [f"{counts[k]} {label}{'s' if counts[k] != 1 else ''}" for k, label in ACTIVITY_LABELS if counts.get(k)]
    if parts:
        lines.append(f"  activity: {', '.join(parts)}")
    lines.append(
        f"  project total: {costs.money(summary['project_total'])} over {summary['project_servers']} "
        f"server{'s' if summary['project_servers'] != 1 else ''}"
    )
    return lines


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
        ctx.event(server, "down")
        db.mark_deleted(ctx.conn, server["id"])
        ctx.known_hosts(server).unlink(missing_ok=True)
        ctx.manifest(server).unlink(missing_ok=True)
        summary = server_summary(ctx, db.get_server(ctx.conn, server["id"]))
    emit(
        ctx.args,
        {"project": name, "deleted": server["hetzner_id"], **summary},
        "\n".join([f"deleted server {server['hetzner_id']} for {name}", *summary_lines(summary)]),
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
            entry["usage"] = costs.server_usage(server)
            if server["status"] == "running":
                try:
                    entry["processes"] = ctx.remote(server).list_units()
                except DevboxError as e:
                    entry["processes_error"] = str(e)
        result.append(entry)

    now = datetime.now(timezone.utc)
    month_start = costs.month_start(now)
    month = costs.summarize(
        db.all_servers(ctx.conn, ctx.args.name), since=month_start, now=now
    )

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
        lines.append(f"  cost: {cost_line(e['usage'], s)}")
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
    scope = f" for {ctx.args.name}" if ctx.args.name else ""
    lines.append(f"this month{scope}: {costs.money(month['total'])} (see `devbox costs`)")
    emit(
        ctx.args,
        {"projects": result, "unregistered": orphans, "month": month},
        "\n".join(lines),
    )
    return 0


def cmd_costs(ctx: Context) -> int:
    a = ctx.args
    now = datetime.now(timezone.utc)
    since = until = None
    if a.month:
        since, until = costs.parse_month(a.month)
    if a.days:
        since = now - timedelta(days=a.days)
    if a.since:
        since = costs.parse_day(a.since)
    if a.until:
        until = costs.parse_day(a.until)
    if a.project and not db.get_project(ctx.conn, a.project) and not db.all_servers(ctx.conn, a.project):
        raise DevboxError(f"unknown project '{a.project}'")

    summary = costs.summarize(db.all_servers(ctx.conn, a.project), since, until, a.by, now)
    period = " to ".join(
        x for x in (
            since.date().isoformat() if since else "start",
            (until - timedelta(seconds=1)).date().isoformat() if until else "now",
        )
    )
    lines = [f"{'KEY':<24} {'SERVERS':>7} {'TIME':>8} {'HOURS':>6} {'COST':>9}"]
    for g in summary["groups"]:
        cost = costs.money(g["cost"]) + ("*" if g["unknown_price"] else "")
        lines.append(
            f"{g['key']:<24} {g['servers']:>7} {costs.duration(g['seconds']):>8} "
            f"{g['billed_hours']:>6} {cost:>9}"
        )
    lines.append(
        f"total {period}: {costs.money(summary['total'])} over {summary['servers']} "
        f"server{'s' if summary['servers'] != 1 else ''}, {costs.duration(summary['seconds'])} running"
    )
    if summary["unknown_price"]:
        lines.append(f"* {summary['unknown_price']} server(s) without a known price are not counted")
    lines.append("Estimates: server price only (incl. VAT), per started hour, capped monthly.")
    emit(ctx.args, summary, "\n".join(lines))
    return 0


def cmd_sync(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    project = db.require_project(ctx.conn, ctx.args.name)
    result = ctx.remote(server).sync(project["path"], ctx.manifest(server))
    ctx.event(server, "sync")
    text = (
        f"synced {result['files']} files, deleted {result['deleted']}"
        if result["mode"] == "git"
        else f"synced (warning: {result['warning']})"
    )
    emit(ctx.args, {"project": ctx.args.name, **result}, text)
    return 0


def cmd_exec(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    script = to_script(ctx.args.cmd)
    remote = ctx.remote(server)

    if ctx.args.detach:
        if not ctx.args.proc:
            raise DevboxError("--detach needs --name PROC")
        if ctx.args.timeout is not None:
            raise DevboxError("--timeout only applies to foreground commands")
        validate_proc(ctx.args.proc)
        if ctx.args.proc == TUNNEL:
            raise DevboxError(f"'{TUNNEL}' is reserved for the preview tunnel; use `devbox expose`")
        remote.start_unit(ctx.args.proc, script)
        ctx.event(server, "exec_detach")
        emit(
            ctx.args,
            {"project": ctx.args.name, "process": ctx.args.proc, "started": True},
            f"started {ctx.args.proc}; follow it with `devbox logs {ctx.args.name} {ctx.args.proc} -f`",
        )
        return 0

    ctx.event(server, "exec")
    result = remote.exec(script, timeout=ctx.args.timeout, capture=ctx.args.json)
    code = EXIT_TIMEOUT if result.timed_out else result.returncode
    if ctx.args.json:
        emit(ctx.args, {
            "exit_code": code,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
    elif result.timed_out:
        print(
            f"error: timed out after {ctx.args.timeout:g}s; the command was stopped on the server",
            file=sys.stderr,
        )
    return code


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
    ctx.event(server, "expose")
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

    p = add("server-types", cmd_server_types, "list server types that can be ordered, cheapest first")
    p.add_argument("--min-cores", type=int, metavar="N")
    p.add_argument("--min-memory", type=float, metavar="GB")
    p.add_argument("--arch", choices=["x86", "arm"])
    p.add_argument("--location", help="e.g. fsn1, nbg1, hel1, ash, hil, sin")
    p.add_argument("--zone", help="network zone, e.g. eu-central, us-east, us-west, ap-southeast")
    p.add_argument("--all", action="store_true", help="include types that are currently unavailable")

    p = add("up", cmd_up, "create or reuse the server and sync code")
    p.add_argument("name")
    p.add_argument("--type", help="server type for a new server (see `devbox server-types`)")
    p.add_argument("--location", help="location for a new server (see `devbox server-types`)")
    p.add_argument("--force", action="store_true", help="allow a second devbox server")

    p = add("down", cmd_down, "delete the server")
    p.add_argument("name")

    p = add("costs", cmd_costs, "estimated server costs for a period, grouped")
    p.add_argument("--project", help="only this project")
    period = p.add_mutually_exclusive_group()
    period.add_argument("--month", metavar="YYYY-MM", help="one calendar month (UTC)")
    period.add_argument("--days", type=int, metavar="N", help="the last N days")
    period.add_argument("--since", metavar="YYYY-MM-DD", help="from this day (UTC)")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="up to, not including, this day")
    p.add_argument("--by", choices=costs.GROUPINGS, default="project", help="grouping (default: project)")

    p = add("status", cmd_status, "show servers, processes, preview URLs and cost so far")
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
    p.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="stop the command on the server after this many seconds (foreground only; exit code 124)",
    )
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
