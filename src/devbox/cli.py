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
        self._snapshot_price = None
        self.snapshot_orphans: list[dict] = []

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

    def manifest(self, project: str) -> Path:
        """Files synced to the project's server (kept across pause/resume)."""
        return self.cfg.state_dir / "manifests" / f"{project}.json"

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
        self.snapshot_orphans = db.reconcile_snapshots(self.conn, self.hcloud.list_snapshots())
        self.fill_missing_prices()
        return orphans

    def snapshot_price(self) -> float | None:
        if self._snapshot_price is None:
            self._snapshot_price = (
                self.hcloud.snapshot_price_per_gb_month() or self.cfg.snapshot_price_per_gb_month
            )
        return self._snapshot_price

    def cost_items(self, project: str | None = None) -> list[dict]:
        """Servers and snapshots, ready for `costs.summarize`."""
        items = [costs.server_item(s) for s in db.all_servers(self.conn, project)]
        items += [costs.snapshot_item(s) for s in db.snapshots(self.conn, project, include_deleted=True)]
        return items

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
        for snap in db.snapshots(self.conn):
            if snap["price_per_gb_month"] is None:
                price = self.snapshot_price()
                if price is None:
                    break
                db.update_snapshot(self.conn, snap["id"], price_per_gb_month=price)

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


def cmd_project_set(ctx: Context) -> int:
    name = ctx.args.name
    value = ctx.args.idle_timeout
    if value is not None:
        if value == "default":
            value = None
        else:
            config.parse_duration(value)
        db.set_idle_timeout(ctx.conn, name, value)
    project = db.require_project(ctx.conn, name)
    effective = project["idle_timeout"] or f"{ctx.cfg.idle_timeout} (default)"
    emit(ctx.args, {"name": name, "idle_timeout": project["idle_timeout"]}, f"{name}: idle timeout {effective}")
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


def choose_offer(ctx: Context, project: str, snapshot=None) -> dict:
    """Pick the offer for a new server.

    Order: --type/--location, then config defaults, then the project's last
    server. When restoring a snapshot, the server must have the same
    architecture and at least the snapshot's disk size.
    """
    server_type = ctx.args.type or ctx.cfg.server_type
    location = ctx.args.location or ctx.cfg.location
    source = "requested" if (ctx.args.type or ctx.args.location) else "config default"
    if not server_type and not location:
        last = db.last_server(ctx.conn, project)
        if last and last["server_type"] and last["location"]:
            server_type, location, source = last["server_type"], last["location"], "last used"
    if not server_type or not location:
        raise DevboxError(
            "no server chosen: run `devbox server-types` and pass --type and --location"
        )

    def fits(o: dict) -> bool:
        if snapshot is None:
            return True
        if snapshot["architecture"] and o["architecture"] != snapshot["architecture"]:
            return False
        return not (snapshot["disk_gb"] and (o["disk_gb"] or 0) < snapshot["disk_gb"])

    offers = ctx.hcloud.offers()
    if not any(o["type"] == server_type for o in offers):
        raise DevboxError(f"unknown server type '{server_type}'; see `devbox server-types`")
    match = next((o for o in offers if o["type"] == server_type and o["location"] == location), None)
    if match and match["available"] is not False and fits(match):
        return {**match, "choice": source}

    if match is None:
        reason = f"{server_type} is not offered in {location}"
    elif not fits(match):
        reason = (
            f"{server_type} cannot hold the snapshot "
            f"({snapshot['architecture']}, {snapshot['disk_gb']} GB disk)"
        )
    else:
        reason = f"{server_type} is not available in {location}"
    wanted = match or next(o for o in offers if o["type"] == server_type)
    alternatives = [
        o for o in filter_offers(offers, min_cores=wanted["cores"], min_memory=wanted["memory_gb"])
        if fits(o)
    ]
    # Prefer the same region, then price.
    alternatives.sort(key=lambda o: o["network_zone"] != wanted["network_zone"])
    lines = [f"{reason} ({source}). Cheapest available alternatives:"]
    lines += [f"  {describe_offer(o)}" for o in alternatives[:5]] or ["  none found"]
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


# Budget


def month_spend(ctx: Context, project: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return costs.summarize(ctx.cost_items(project), since=costs.month_start(now), now=now)


def budget_status(ctx: Context) -> dict | None:
    budget = ctx.cfg.monthly_budget
    if budget is None:
        return None
    spent = month_spend(ctx)["total"]
    return {"budget": budget, "spent": spent, "left": round(budget - spent, 4), "action": ctx.cfg.budget_action}


def check_budget(ctx: Context) -> list[str]:
    """Warnings before creating a server; raises when the budget blocks it."""
    status = budget_status(ctx)
    if status is None:
        return []
    spent, budget = status["spent"], status["budget"]
    if spent >= budget:
        msg = f"this month's spending ({costs.money(spent)}) has reached the budget of {costs.money(budget)}"
        if status["action"] == "block" and not ctx.args.over_budget:
            raise DevboxError(f"{msg}; pass --over-budget to create a server anyway")
        return [msg]
    if spent >= 0.8 * budget:
        return [f"{costs.money(spent)} of the {costs.money(budget)} monthly budget is used"]
    return []


# Server lifecycle


def cmd_up(ctx: Context) -> int:
    name = ctx.args.name
    cfg = ctx.cfg
    offer = None
    snapshot = None
    warnings: list[str] = []
    with ctx.lock():
        project = db.require_project(ctx.conn, name)
        orphans = ctx.reconcile()
        server = db.active_server(ctx.conn, name)
        created = False

        if server is None:
            snapshot = None if ctx.args.fresh else db.latest_snapshot(ctx.conn, name)
            if ctx.args.require_snapshot and snapshot is None:
                raise DevboxError(f"{name} has no snapshot to resume from; use `devbox up {name}`")
            others = [s["project"] for s in db.active_servers(ctx.conn)]
            others += [o["name"] for o in orphans]
            if others and not ctx.args.force:
                raise DevboxError(
                    f"another devbox server is running ({', '.join(others)}); "
                    "stop it with `devbox down` or pass --force"
                )
            warnings += check_budget(ctx)
            offer = choose_offer(ctx, name, snapshot)
            public_key = ensure_local_ssh_key(cfg.ssh_key)
            ssh_key_id = ctx.hcloud.ensure_ssh_key(public_key)
            firewall_id = ctx.hcloud.ensure_firewall(resolve_ssh_allow(cfg.ssh_allow))

            source = f" from snapshot {snapshot['hetzner_image_id']}" if snapshot else ""
            print(f"creating {describe_offer(offer)} for {name}{source}...", file=sys.stderr)
            user_data = resources.files("devbox").joinpath("cloud-init.yaml").read_text()
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
                f.write(user_data)
                f.flush()
                info = ctx.hcloud.create_server(
                    f"devbox-{name}", name, offer["type"],
                    str(snapshot["hetzner_image_id"]) if snapshot else cfg.image,
                    offer["location"], Path(f.name),
                    ssh_key_id=ssh_key_id, firewall_id=firewall_id,
                )
            if snapshot is None:
                # A clean server has none of the previously synced files.
                ctx.manifest(name).unlink(missing_ok=True)
            db.insert_server(
                ctx.conn, name, info["id"], "creating",
                ip=info["ip"], server_type=info["server_type"] or offer["type"],
                location=info["location"] or offer["location"], created_at=info.get("created"),
                price_hourly=offer["price_hourly"], price_monthly=offer["price_monthly"],
                snapshot_id=snapshot["id"] if snapshot else None,
            )
            server = db.active_server(ctx.conn, name)
            created = True
            ctx.event(server, "restore" if snapshot else "create")

        remote = ctx.remote(server)
        if server["status"] != "running":
            print(f"waiting for {server['ip']} to finish setup...", file=sys.stderr)
        warnings += remote.wait_ready()
        db.update_server(ctx.conn, server["id"], status="running")

        print("syncing code...", file=sys.stderr)
        sync = remote.sync(project["path"], ctx.manifest(name))
        if sync["warning"]:
            warnings.append(sync["warning"])
        ctx.event(server, "up")
        ctx.event(server, "sync")
        server = db.active_server(ctx.conn, name)

    data = {
        "project": name,
        "created": created,
        "restored_from_snapshot": row(snapshot) if created and snapshot else None,
        "server": row(server),
        "has_notes": bool(project["notes"].strip()),
        "offer": offer,
        "warnings": warnings,
    }
    verb = "reusing"
    if created:
        verb = "restored" if snapshot else "created"
    text = [
        f"{name}: {verb} server {server['hetzner_id']} "
        f"({server['server_type']} in {server['location']}) at {server['ip']}, code synced",
    ]
    if offer:
        text.append(f"  {offer['choice']}: {describe_offer(offer)}")
    if created and snapshot:
        text.append(
            f"  from snapshot taken {snapshot['created_at'][:16].replace('T', ' ')}: installed tools, "
            f"dependencies and data from then are back; background processes and the preview must be started again"
        )
    text += [f"warning: {w}" for w in warnings]
    if not data["has_notes"]:
        text.append(f"no notes for {name} yet; work out how to start it, then save them with `devbox project notes {name} --set ...`")
    emit(ctx.args, data, "\n".join(text))
    return 0


def server_summary(ctx: Context, server, snapshot=None, pruned=None) -> dict:
    """Usage, cost and activity for one server over its whole lifetime."""
    usage = costs.server_usage(server)
    project_total = costs.summarize(ctx.cost_items(server["project"]))
    snap = None
    if snapshot is not None:
        item = costs.snapshot_item(snapshot)
        snap = {**dict(snapshot), "monthly_cost": item["price_monthly"]}
    return {
        "server": row(server),
        "uptime_seconds": usage["seconds"],
        "billed_hours": usage["billed_hours"],
        "cost": usage["cost"],
        "currency": costs.CURRENCY,
        "activity": db.event_counts(ctx.conn, server["id"]),
        "snapshot": snap,
        "deleted_snapshots": pruned or [],
        "project_total": project_total["total"],
        "project_servers": project_total["servers"],
    }


ACTIVITY_LABELS = [
    ("sync", "sync"), ("exec", "exec"), ("exec_detach", "background start"),
    ("expose", "expose"), ("up", "up"), ("touch", "touch"),
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
    snap = summary["snapshot"]
    if snap:
        size = f"{snap['size_gb']:.1f} GB" if snap["size_gb"] is not None else "size unknown"
        lines.append(
            f"  snapshot {snap['hetzner_image_id']}: {size}, ~{costs.money(snap['monthly_cost'])}/month "
            f"while kept; `devbox up {s['project']}` restores it"
        )
    for image_id in summary["deleted_snapshots"]:
        lines.append(f"  deleted older snapshot {image_id}")
    lines.append(
        f"  project total: {costs.money(summary['project_total'])} over {summary['project_servers']} "
        f"server{'s' if summary['project_servers'] != 1 else ''} (incl. snapshots)"
    )
    return lines


def prune_snapshots(ctx: Context, project: str) -> list[int]:
    """Delete snapshots beyond `keep_snapshots`, oldest first. Returns deleted image ids."""
    deleted = []
    for snap in db.snapshots(ctx.conn, project)[ctx.cfg.keep_snapshots:]:
        try:
            ctx.hcloud.delete_image(snap["hetzner_image_id"])
        except DevboxError as e:
            print(f"warning: could not delete old snapshot {snap['hetzner_image_id']}: {e}", file=sys.stderr)
            continue
        db.mark_snapshot_deleted(ctx.conn, snap["id"])
        deleted.append(snap["hetzner_image_id"])
    return deleted


def take_down(ctx: Context, server, snapshot: bool, event: str) -> dict:
    """Delete a project's server, optionally snapshotting it first. Caller holds the lock."""
    name = server["project"]
    snap = None
    pruned: list[int] = []
    if snapshot:
        print(f"shutting down {name} for a snapshot...", file=sys.stderr)
        ctx.hcloud.shutdown(server["hetzner_id"])
        print("creating snapshot (this can take a few minutes)...", file=sys.stderr)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        try:
            info = ctx.hcloud.create_snapshot(server["hetzner_id"], name, f"devbox {name} {stamp}")
        except DevboxError as e:
            try:
                ctx.hcloud.poweron(server["hetzner_id"])
                kept = "the server was kept and powered on again"
            except DevboxError:
                kept = "the server was kept but is powered off; start it in the Hetzner console"
            raise DevboxError(f"snapshot failed, {kept}: {e}")
        snap_id = db.insert_snapshot(
            ctx.conn, name, info["id"], server_id=server["id"],
            server_type=server["server_type"], location=server["location"],
            architecture=info["architecture"], size_gb=info["size_gb"], disk_gb=info["disk_gb"],
            price_per_gb_month=ctx.snapshot_price(), created_at=info.get("created"),
        )
        snap = db.get_snapshot(ctx.conn, snap_id)

    ctx.hcloud.delete_server(server["hetzner_id"])
    ctx.event(server, event)
    db.mark_deleted(ctx.conn, server["id"])
    ctx.known_hosts(server).unlink(missing_ok=True)
    if snapshot:
        pruned = prune_snapshots(ctx, name)
    else:
        ctx.manifest(name).unlink(missing_ok=True)
    return server_summary(ctx, db.get_server(ctx.conn, server["id"]), snap, pruned)


def cmd_down(ctx: Context) -> int:
    name = ctx.args.name
    snapshot = ctx.args.snapshot
    with ctx.lock():
        db.require_project(ctx.conn, name)
        ctx.reconcile()
        server = db.active_server(ctx.conn, name)
        if server is None:
            emit(ctx.args, {"project": name, "deleted": None}, f"{name} has no server")
            return 0
        summary = take_down(ctx, server, snapshot, "pause" if snapshot else "down")
        remaining = db.latest_snapshot(ctx.conn, name)
    lines = [f"{'paused' if snapshot else 'deleted'} server {server['hetzner_id']} for {name}", *summary_lines(summary)]
    if not snapshot and remaining:
        lines.append(
            f"  note: snapshot {remaining['hetzner_image_id']} from {remaining['created_at'][:10]} is kept; "
            f"the next `devbox up {name}` restores it (use --fresh for a clean server)"
        )
    emit(ctx.args, {"project": name, "deleted": server["hetzner_id"], **summary}, "\n".join(lines))
    return 0


def project_idle_timeout(ctx: Context, project) -> tuple[str, timedelta | None]:
    value = project["idle_timeout"] or ctx.cfg.idle_timeout
    return value, config.parse_duration(value)


def idle_info(ctx: Context, server, now: datetime) -> dict:
    project = db.get_project(ctx.conn, server["project"])
    setting, timeout = project_idle_timeout(ctx, project)
    last = db.last_activity(ctx.conn, server)
    idle = now - last
    return {
        "last_activity": last.isoformat(timespec="seconds"),
        "idle_seconds": int(idle.total_seconds()),
        "idle_timeout": setting,
        "remaining_seconds": None if timeout is None else int((timeout - idle).total_seconds()),
    }


def cmd_idle_check(ctx: Context) -> int:
    now = datetime.now(timezone.utc)
    action = ctx.cfg.idle_action
    results = []
    failed = False
    with ctx.lock():
        ctx.reconcile()
        for server in db.active_servers(ctx.conn):
            info = idle_info(ctx, server, now)
            entry = {"project": server["project"], "server": server["hetzner_id"], **info, "action": None}
            remaining = info["remaining_seconds"]
            if remaining is None or remaining > 0:
                results.append(entry)
                continue
            entry["action"] = action
            if ctx.args.dry_run:
                entry["dry_run"] = True
            else:
                try:
                    entry["summary"] = take_down(ctx, server, action == "pause", f"idle_{action}")
                except DevboxError as e:
                    entry["error"] = str(e)
                    failed = True
            results.append(entry)

    lines = []
    for e in results:
        idle = costs.duration(e["idle_seconds"])
        if e["action"] is None:
            if e["remaining_seconds"] is None:
                lines.append(f"{e['project']}: idle {idle}, idle shutdown off")
            else:
                lines.append(f"{e['project']}: idle {idle}, "
                             f"{costs.duration(e['remaining_seconds'])} left of {e['idle_timeout']}")
        elif e.get("dry_run"):
            lines.append(f"{e['project']}: idle {idle} (limit {e['idle_timeout']}), would {e['action']}")
        elif "error" in e:
            lines.append(f"{e['project']}: idle {idle}, {e['action']} FAILED: {e['error']}")
        else:
            verb = "paused" if e["action"] == "pause" else "deleted"
            lines.append(f"{e['project']}: idle {idle} (limit {e['idle_timeout']}), {verb}")
            lines += summary_lines(e["summary"])
    emit(ctx.args, results, "\n".join(lines) or "no devbox servers")
    return 1 if failed else 0


def cmd_touch(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    ctx.event(server, "touch")
    info = idle_info(ctx, server, datetime.now(timezone.utc))
    left = info["remaining_seconds"]
    text = "activity recorded; " + (
        "idle shutdown is off" if left is None else f"idle shutdown in {costs.duration(left)} without activity"
    )
    emit(ctx.args, {"project": ctx.args.name, **info}, text)
    return 0


def cmd_status(ctx: Context) -> int:
    orphans = ctx.reconcile()
    if ctx.args.name:
        projects = [db.require_project(ctx.conn, ctx.args.name)]
    else:
        projects = db.list_projects(ctx.conn)

    now = datetime.now(timezone.utc)
    result = []
    for p in projects:
        server = db.active_server(ctx.conn, p["name"])
        snap = db.latest_snapshot(ctx.conn, p["name"])
        entry = {
            "project": p["name"],
            "server": row(server),
            "processes": None,
            "snapshot": {**dict(snap), "monthly_cost": costs.snapshot_item(snap)["price_monthly"]} if snap else None,
        }
        if server:
            entry["uptime"] = uptime(server["created_at"])
            entry["usage"] = costs.server_usage(server)
            entry["idle"] = idle_info(ctx, server, now)
            if server["status"] == "running":
                try:
                    entry["processes"] = ctx.remote(server).list_units()
                except DevboxError as e:
                    entry["processes_error"] = str(e)
        result.append(entry)

    month = month_spend(ctx, ctx.args.name, now)
    budget = budget_status(ctx)

    lines = []
    for e in result:
        s = e["server"]
        snap = e["snapshot"]
        if not s:
            if snap:
                size = f"{snap['size_gb']:.1f} GB" if snap["size_gb"] is not None else "size unknown"
                lines.append(
                    f"{e['project']}: paused, snapshot {snap['hetzner_image_id']} from "
                    f"{snap['created_at'][:16].replace('T', ' ')} ({size}, ~{costs.money(snap['monthly_cost'])}/month)"
                )
            elif ctx.args.name:
                lines.append(f"{e['project']}: no server")
            continue
        lines.append(
            f"{e['project']}: {s['status']} {s['server_type']} {s['location']} {s['ip']} "
            f"(id {s['hetzner_id']}, up {e['uptime']})"
        )
        lines.append(f"  cost: {cost_line(e['usage'], s)}")
        idle = e["idle"]
        if idle["remaining_seconds"] is None:
            lines.append(f"  idle {costs.duration(idle['idle_seconds'])} (idle shutdown off)")
        else:
            lines.append(
                f"  idle {costs.duration(idle['idle_seconds'])}, {ctx.cfg.idle_action} after {idle['idle_timeout']} "
                f"({costs.duration(max(0, idle['remaining_seconds']))} left)"
            )
        if s["preview_url"]:
            lines.append(f"  preview: {s['preview_url']} -> port {s['preview_port']}")
        for proc in e["processes"] or []:
            lines.append(f"  {proc['name']}: {proc['active']} ({proc['sub']})")
        if "processes_error" in e:
            lines.append(f"  processes: {e['processes_error']}")
        if snap:
            lines.append(f"  snapshot {snap['hetzner_image_id']} kept (~{costs.money(snap['monthly_cost'])}/month)")
    for o in orphans:
        lines.append(f"unregistered devbox server {o['name']} (id {o['id']}, {o['ip']})")
    for o in ctx.snapshot_orphans:
        lines.append(f"unregistered devbox snapshot {o['id']} ({o['description']})")
    if not lines:
        lines.append("no devbox servers")
    scope = f" for {ctx.args.name}" if ctx.args.name else ""
    month_line = f"this month{scope}: {costs.money(month['total'])}"
    if budget and not ctx.args.name:
        month_line += f" of {costs.money(budget['budget'])} budget ({costs.money(budget['left'])} left)"
    lines.append(month_line + " (see `devbox costs`)")
    emit(
        ctx.args,
        {"projects": result, "unregistered": orphans, "unregistered_snapshots": ctx.snapshot_orphans,
         "month": month, "budget": budget},
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

    summary = costs.summarize(ctx.cost_items(a.project), since, until, a.by, now)
    period = " to ".join(
        x for x in (
            since.date().isoformat() if since else "start",
            (until - timedelta(seconds=1)).date().isoformat() if until else "now",
        )
    )
    lines = [f"{'KEY':<24} {'SERVERS':>7} {'SNAPS':>5} {'TIME':>8} {'HOURS':>6} {'COST':>9}"]
    for g in summary["groups"]:
        cost = costs.money(g["cost"]) + ("*" if g["unknown_price"] else "")
        lines.append(
            f"{g['key']:<24} {g['servers']:>7} {g['snapshots']:>5} {costs.duration(g['seconds']):>8} "
            f"{g['billed_hours']:>6} {cost:>9}"
        )
    lines.append(
        f"total {period}: {costs.money(summary['total'])} over {summary['servers']} "
        f"server{'s' if summary['servers'] != 1 else ''}, {costs.duration(summary['seconds'])} running"
        + (f", incl. {costs.money(summary['snapshot_total'])} for snapshots" if summary["snapshots"] else "")
    )
    if summary["unknown_price"]:
        lines.append(f"* {summary['unknown_price']} item(s) without a known price are not counted")
    lines.append("Estimates incl. VAT: servers per started hour and snapshots per GB, both capped monthly. IPv4 and traffic not included.")
    emit(ctx.args, summary, "\n".join(lines))
    return 0


def cmd_snapshot_list(ctx: Context) -> int:
    ctx.reconcile()
    snaps = db.snapshots(ctx.conn, ctx.args.name)
    data = []
    for s in snaps:
        item = costs.snapshot_item(s)
        data.append({**dict(s), "monthly_cost": item["price_monthly"], "cost_so_far": costs.server_usage(item)["cost"]})
    lines = [f"{'IMAGE':<10} {'PROJECT':<16} {'CREATED':<16} {'SIZE':>7} {'FROM':<12} {'€/MONTH':>8} {'SO FAR':>8}"]
    for d in data:
        size = f"{d['size_gb']:.1f}G" if d["size_gb"] is not None else "?"
        lines.append(
            f"{d['hetzner_image_id']:<10} {d['project']:<16} {d['created_at'][:16].replace('T', ' '):<16} "
            f"{size:>7} {(d['server_type'] or '?'):<12} {costs.money(d['monthly_cost']):>8} {costs.money(d['cost_so_far']):>8}"
        )
    for o in ctx.snapshot_orphans:
        lines.append(f"unregistered devbox snapshot {o['id']} ({o['description']})")
    emit(ctx.args, {"snapshots": data, "unregistered": ctx.snapshot_orphans},
         "\n".join(lines) if data or ctx.snapshot_orphans else "no snapshots")
    return 0


def cmd_snapshot_delete(ctx: Context) -> int:
    image_id = ctx.args.image_id
    with ctx.lock():
        ctx.reconcile()
        snap = db.get_snapshot_by_image(ctx.conn, image_id)
        if snap is None or snap["status"] == "deleted":
            raise DevboxError(f"no devbox snapshot {image_id}; see `devbox snapshot list`")
        ctx.hcloud.delete_image(image_id)
        db.mark_snapshot_deleted(ctx.conn, snap["id"])
    emit(ctx.args, {"deleted": image_id, "project": snap["project"]}, f"deleted snapshot {image_id} of {snap['project']}")
    return 0


def cmd_sync(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    project = db.require_project(ctx.conn, ctx.args.name)
    result = ctx.remote(server).sync(project["path"], ctx.manifest(ctx.args.name))
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

    p = proj_sub.add_parser("set", help="change project settings", parents=[common])
    p.add_argument("name")
    p.add_argument("--idle-timeout", metavar="DURATION", help="e.g. 2h, 1d, off, or 'default'")
    p.set_defaults(func=cmd_project_set)

    p = proj_sub.add_parser("remove", help="hide a project (cost history is kept)", parents=[common])
    p.add_argument("name")
    p.set_defaults(func=cmd_project_remove)

    p = add("server-types", cmd_server_types, "list server types that can be ordered, cheapest first")
    p.add_argument("--min-cores", type=int, metavar="N")
    p.add_argument("--min-memory", type=float, metavar="GB")
    p.add_argument("--arch", choices=["x86", "arm"])
    p.add_argument("--location", help="e.g. fsn1, nbg1, hel1, ash, hil, sin")
    p.add_argument("--zone", help="network zone, e.g. eu-central, us-east, us-west, ap-southeast")
    p.add_argument("--all", action="store_true", help="include types that are currently unavailable")

    for name, help, require_snapshot in [
        ("up", "create (restoring the latest snapshot, if any) or reuse the server, and sync code", False),
        ("resume", "like up, but only from a snapshot", True),
    ]:
        p = add(name, cmd_up, help)
        p.add_argument("name")
        p.add_argument("--type", help="server type for a new server (default: last used)")
        p.add_argument("--location", help="location for a new server (default: last used)")
        p.add_argument("--force", action="store_true", help="allow a second devbox server")
        p.add_argument("--over-budget", action="store_true", help="create even if the monthly budget is used up")
        p.set_defaults(require_snapshot=require_snapshot, fresh=False)
        if not require_snapshot:
            p.add_argument("--fresh", action="store_true", help="ignore snapshots and start from a clean image")

    p = add("down", cmd_down, "delete the server")
    p.add_argument("name")
    p.add_argument("--snapshot", action="store_true", help="snapshot the server first (same as pause)")

    p = add("pause", cmd_down, "snapshot the server, then delete it; `up` restores it")
    p.add_argument("name")
    p.set_defaults(snapshot=True)

    p = add("touch", cmd_touch, "record activity so idle shutdown waits")
    p.add_argument("name")

    p = add("idle-check", cmd_idle_check, "pause or delete servers idle past their timeout (run from cron)")
    p.add_argument("--dry-run", action="store_true", help="only report what would happen")

    snap = sub.add_parser("snapshot", help="manage snapshots", parents=[common])
    snap_sub = snap.add_subparsers(dest="snapshot_command", required=True)
    p = snap_sub.add_parser("list", help="list snapshots and their cost", parents=[common])
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_snapshot_list)
    p = snap_sub.add_parser("delete", help="delete a snapshot", parents=[common])
    p.add_argument("image_id", type=int)
    p.set_defaults(func=cmd_snapshot_delete)

    p = add("costs", cmd_costs, "estimated server and snapshot costs for a period, grouped")
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
