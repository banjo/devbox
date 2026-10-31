import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from devbox import DevboxError, admin, config, costs, databases, db, envsecrets, startup, watch
from devbox.hetzner import Hcloud, filter_offers
from devbox.remote import PULL_PRUNE, TUNNEL, Remote, git_files, git_ignored, to_script, validate_proc


EXIT_TIMEOUT = 124
# Hetzner server statuses in which nothing runs on the server.
POWERED_DOWN = ("off", "stopping")


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
            if server["setup_error"]:
                raise DevboxError(
                    f"setup of the server for '{project}' failed at {server['setup_stage']}; "
                    f"`devbox up {project}` retries it, `devbox down {project}` deletes it"
                )
            raise DevboxError(f"server for '{project}' is still being set up; run `devbox up {project}` again")
        if server["power"] in POWERED_DOWN:
            raise DevboxError(
                f"the server for '{project}' is powered off (and still billed); "
                f"`devbox up {project}` powers it on, `devbox pause {project}` or `devbox down {project}` removes it"
            )
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
        """Servers and snapshots adopted by reconcile (or saved without a price) get today's price.

        Servers and snapshots are handled independently, so a snapshot is
        priced even when no server needs a price.
        """
        missing = [s for s in db.servers_missing_price(self.conn) if s["status"] != "deleted"]
        if missing:
            try:
                offers = self.hcloud.offers()
            except DevboxError:
                offers = []
            prices = {(o["type"], o["location"]): o for o in offers}
            for s in missing:
                o = prices.get((s["server_type"], s["location"]))
                if o:
                    db.update_server(
                        self.conn, s["id"], price_hourly=o["price_hourly"], price_monthly=o["price_monthly"]
                    )
        unpriced = [snap for snap in db.snapshots(self.conn) if snap["price_per_gb_month"] is None]
        if unpriced:
            price = self.snapshot_price()
            if price is not None:
                for snap in unpriced:
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


def server_state(server) -> str:
    if server["power"] in POWERED_DOWN:
        return "powered off"
    if server["status"] == "creating" and server["setup_error"]:
        return f"setup failed at {server['setup_stage']}"
    return server["status"]


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
    db.require_project(ctx.conn, name)
    value = ctx.args.idle_timeout
    if value is not None:
        if value == "default":
            value = None
        else:
            config.parse_duration(value)
        db.set_idle_timeout(ctx.conn, name, value)
    value = ctx.args.startup_timeout
    if value is not None:
        if value == "default":
            value = None
        elif config.parse_duration(value) is None:
            raise DevboxError("the startup timeout can't be off")
        db.set_startup_timeout(ctx.conn, name, value)
    project = db.require_project(ctx.conn, name)
    idle = project["idle_timeout"] or f"{ctx.cfg.idle_timeout} (default)"
    start = project["startup_timeout"] or f"{ctx.cfg.startup_timeout} (default)"
    emit(
        ctx.args,
        {"name": name, "idle_timeout": project["idle_timeout"], "startup_timeout": project["startup_timeout"]},
        f"{name}: idle timeout {idle}, startup timeout {start}",
    )
    return 0


def cmd_project_remove(ctx: Context) -> int:
    db.remove_project(ctx.conn, ctx.args.name)
    envsecrets.SecretStore(config.config_dir(), ctx.args.name).delete_all()
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
    month = month_spend(ctx)
    spent = month["total"]
    return {
        "budget": budget, "spent": spent, "left": round(budget - spent, 4), "action": ctx.cfg.budget_action,
        # Servers or snapshots this month whose price is unknown are not in `spent`.
        "unpriced": month["unknown_price"],
    }


def check_budget(ctx: Context) -> list[str]:
    """Warnings before creating a server; raises when the budget blocks it."""
    status = budget_status(ctx)
    if status is None:
        return []
    spent, budget = status["spent"], status["budget"]
    warnings = []
    if status["unpriced"]:
        warnings.append(
            f"{status['unpriced']} server(s)/snapshot(s) this month have no known price, so the budget check "
            f"is incomplete (spent so far is at least {costs.money(spent)})"
        )
    if spent >= budget:
        msg = f"this month's spending ({costs.money(spent)}) has reached the budget of {costs.money(budget)}"
        if status["action"] == "block" and not ctx.args.over_budget:
            raise DevboxError(f"{msg}; pass --over-budget to create a server anyway")
        return warnings + [msg]
    if spent >= 0.8 * budget:
        warnings.append(f"{costs.money(spent)} of the {costs.money(budget)} monthly budget is used")
    return warnings


# Server lifecycle


def startup_limit(ctx: Context, project) -> tuple[str, float]:
    """The startup deadline for `up`: --startup-timeout, then the project's, then the config's."""
    value = ctx.args.startup_timeout or project["startup_timeout"] or ctx.cfg.startup_timeout
    limit = config.parse_duration(value)
    if limit is None or limit.total_seconds() <= 0:
        raise DevboxError(f"invalid startup timeout '{value}': use a duration like 5m (it can't be off)")
    return value, limit.total_seconds()


def cmd_up(ctx: Context) -> int:
    name = ctx.args.name
    project = db.require_project(ctx.conn, name)
    limit_label, limit = startup_limit(ctx, project)
    deadline = startup.Deadline(limit)
    progress = startup.Progress(deadline)
    state = {"server_id": None, "created": False, "snapshot": None}
    with ctx.lock():
        ctx.hcloud.deadline = deadline
        try:
            result = bring_up(ctx, project, deadline, progress, state)
        except (DevboxError, KeyboardInterrupt) as e:
            ctx.hcloud.deadline = None
            if isinstance(e, DevboxError) and not isinstance(e, (startup.StageTimeout, startup.StageFailed)) \
                    and state["server_id"] is None and progress.stage == "prepare":
                raise  # checks before anything was created: budget, server choice, ...
            raise startup_failure(ctx, e, state, deadline, progress, limit_label)
        finally:
            ctx.hcloud.deadline = None
    return report_up(ctx, result, deadline)


def bring_up(ctx: Context, project, deadline: "startup.Deadline", progress: "startup.Progress", state: dict) -> dict:
    """Every step of `up` under one deadline. Caller holds the lock."""
    name = project["name"]
    cfg = ctx.cfg
    offer = None
    snapshot = None
    warnings: list[str] = []
    hc = ctx.hcloud

    hc.stage = "prepare"
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
        ssh_key_id = hc.ensure_ssh_key(public_key)
        firewall_id = hc.ensure_firewall(resolve_ssh_allow(cfg.ssh_allow))

        source = f" from snapshot {snapshot['hetzner_image_id']}" if snapshot else ""
        hc.stage = "create"
        progress.step("create", f"Creating server: {describe_offer(offer)}{source}")
        user_data = resources.files("devbox").joinpath("cloud-init.yaml").read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write(user_data)
            f.flush()
            info = hc.create_server(
                f"devbox-{name}", name, offer["type"],
                str(snapshot["hetzner_image_id"]) if snapshot else cfg.image,
                offer["location"], Path(f.name),
                ssh_key_id=ssh_key_id, firewall_id=firewall_id,
            )
        if snapshot is None:
            # A clean server has none of the previously synced files.
            ctx.manifest(name).unlink(missing_ok=True)
        server_id = db.insert_server(
            ctx.conn, name, info["id"], "creating", power=info.get("status"),
            ip=info["ip"], server_type=info["server_type"] or offer["type"],
            location=info["location"] or offer["location"], created_at=info.get("created"),
            price_hourly=offer["price_hourly"], price_monthly=offer["price_monthly"],
            snapshot_id=snapshot["id"] if snapshot else None,
        )
        state.update(server_id=server_id, created=True, snapshot=snapshot)
        server = db.get_server(ctx.conn, server_id)
        created = True
        progress.say(f"Server {server['hetzner_id']} created at {server['ip']}")
        ctx.event(server, "restore" if snapshot else "create")
    else:
        state["server_id"] = server["id"]
        if server["setup_error"]:
            progress.say(f"Retrying setup of server {server['hetzner_id']} (failed before at {server['setup_stage']})")

    powered_on = False
    if server["power"] in POWERED_DOWN:
        hc.stage = "power-on"
        progress.step("power-on", f"Powering on server {server['hetzner_id']}...")
        hc.poweron(server["hetzner_id"])
        powered_on = True

    remote = ctx.remote(server)
    secret_values = project_secret_values(name)
    remote.wait_ssh(deadline, progress)
    if server["status"] != "running" or powered_on:
        warnings += remote.wait_cloud_init(deadline, progress, secrets=secret_values)
        remote.verify_tools(deadline, progress)
    db.update_server(
        ctx.conn, server["id"], status="running", power="running",
        setup_stage=None, setup_error=None,
    )

    progress.step("sync", "Syncing code...")
    sync = remote.sync(project["path"], ctx.manifest(name), timeout=deadline.bound("sync", deadline.remaining()))
    if sync["warning"]:
        warnings.append(sync["warning"])
    ctx.event(server, "up")
    ctx.event(server, "sync")
    progress.step("secrets", "Writing secrets...") if secret_store(ctx, name).load() else None
    deadline.check("secrets")
    secrets_written = push_secrets(ctx, server, name)
    progress.say("Ready")
    return {
        "project": project,
        "server": db.active_server(ctx.conn, name),
        "created": created,
        "powered_on": powered_on,
        "snapshot": snapshot,
        "offer": offer,
        "warnings": warnings,
        "secrets_written": secrets_written,
    }


def report_up(ctx: Context, r: dict, deadline: "startup.Deadline") -> int:
    project, server, snapshot, offer = r["project"], r["server"], r["snapshot"], r["offer"]
    name = project["name"]
    data = {
        "project": name,
        "created": r["created"],
        "restored_from_snapshot": row(snapshot) if r["created"] and snapshot else None,
        "secrets_written": r["secrets_written"],
        "server": row(server),
        "has_notes": bool(project["notes"].strip()),
        "offer": offer,
        "warnings": r["warnings"],
        "startup_seconds": round(deadline.elapsed(), 1),
    }
    verb = "powered on" if r["powered_on"] else "reusing"
    if r["created"]:
        verb = "restored" if snapshot else "created"
    text = [
        f"{name}: {verb} server {server['hetzner_id']} "
        f"({server['server_type']} in {server['location']}) at {server['ip']}, code synced "
        f"in {startup.duration_label(deadline.elapsed())}",
    ]
    if offer:
        text.append(f"  {offer['choice']}: {describe_offer(offer)}")
    if r["created"] and snapshot:
        text.append(
            f"  from snapshot taken {snapshot['created_at'][:16].replace('T', ' ')}: installed tools, "
            f"dependencies and data from then are back; background processes and the preview must be started again"
        )
    if r["secrets_written"]:
        text.append("  secrets written to " + ", ".join(f"{f} ({n} keys)" for f, n in r["secrets_written"].items()))
    text += [f"warning: {w}" for w in r["warnings"]]
    if not data["has_notes"]:
        text.append(f"no notes for {name} yet; work out how to start it, then save them with `devbox project notes {name} --set ...`")
    emit(ctx.args, data, "\n".join(text))
    return 0


def project_secret_values(name: str) -> list[str]:
    files = envsecrets.SecretStore(config.config_dir(), name).load()
    return [v for values in files.values() for v in values.values()]


def startup_failure(ctx: Context, exc, state: dict, deadline, progress, limit_label: str) -> DevboxError:
    """Record a failed or interrupted `up`, collect diagnostics, optionally clean up, and explain."""
    interrupted = isinstance(exc, KeyboardInterrupt)
    timed_out = isinstance(exc, startup.StageTimeout)
    stage = getattr(exc, "stage", None) or progress.stage
    name = ctx.args.name
    reason = "interrupted (Ctrl-C)" if interrupted else str(exc)
    code = "interrupted" if interrupted else ("startup_timeout" if timed_out else "startup_failed")
    exit_code = startup.EXIT_INTERRUPTED if interrupted else (startup.EXIT_TIMEOUT if timed_out else 1)
    secret_values = project_secret_values(name)
    reason = startup.redact(reason, secret_values)

    server = db.get_server(ctx.conn, state["server_id"]) if state["server_id"] else None
    details = {
        "code": code,
        "stage": stage,
        "elapsed_seconds": round(deadline.elapsed(), 1),
        "limit_seconds": deadline.limit,
        "project": name,
        "server_id": server["hetzner_id"] if server else None,
        "ip": server["ip"] if server else None,
        "server_retained": bool(server),
        "billable": bool(server),
        "remote_may_still_run": stage in ("cloud-init", "tools") and (timed_out or interrupted),
        "diagnostics": None,
        "cleanup": None,
    }

    if timed_out:
        headline = f"Startup timed out during {stage} after {startup.duration_label(deadline.elapsed())} (limit: {limit_label})"
    elif interrupted:
        headline = f"Startup interrupted during {stage} after {startup.duration_label(deadline.elapsed())}"
    else:
        headline = f"Startup failed during {stage} after {startup.duration_label(deadline.elapsed())}"
    lines = [headline, f"  reason: {reason}", "", f"Project: {name}"]

    if server is None:
        if stage == "create":
            lines += [
                "Server: unknown. The create request may still complete at Hetzner; "
                f"`devbox status` picks up a server created for {name} (it would be billed).",
            ]
            details["billable"] = None
        else:
            lines.append("Server: none was created")
        return DevboxError("\n".join(lines), code=code, exit_code=exit_code, details=details)

    remote = ctx.remote(server)
    diag_path = None
    try:
        text = remote.collect_diagnostics(timeout=15)
        diag_dir = ctx.cfg.state_dir / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        diag_path = diag_dir / f"{name}-{server['hetzner_id']}-{stamp}.log"
        header = f"devbox up {name}: {headline}\nreason: {reason}\nserver: {server['hetzner_id']} {server['ip']}\n\n"
        diag_path.write_text(startup.redact(header + text, secret_values))
        details["diagnostics"] = str(diag_path)
    except (DevboxError, OSError) as e:
        details["diagnostics_error"] = str(e)

    cleanup = None
    if ctx.args.cleanup_on_failure:
        if state["created"] and state["snapshot"] is None:
            cleanup = {"requested": True, "deleted": False}
            try:
                ctx.hcloud.delete_server(server["hetzner_id"])
                still_there = any(s["id"] == server["hetzner_id"] for s in ctx.hcloud.list_servers())
                if still_there:
                    cleanup["error"] = "Hetzner still lists the server after deleting it"
                else:
                    cleanup["deleted"] = True
                    ctx.event(server, "setup_cleanup")
                    db.mark_deleted(ctx.conn, server["id"])
                    ctx.known_hosts(server).unlink(missing_ok=True)
            except DevboxError as e:
                cleanup["error"] = str(e)
        else:
            cleanup = {"requested": True, "deleted": False,
                       "skipped": "only a fresh server created by this run is deleted; this one was reused or restored"}
    details["cleanup"] = cleanup

    deleted = bool(cleanup and cleanup.get("deleted"))
    details["server_retained"] = details["billable"] = not deleted
    if not deleted:
        db.update_server(ctx.conn, server["id"], setup_stage=stage, setup_error=reason[:500],
                         diagnostics=str(diag_path) if diag_path else None)
        ctx.event(server, "setup_failed")

    rate = f" (~€{server['price_hourly']:.4f}/h)" if server["price_hourly"] is not None else ""
    lines.append(f"Server: {server['hetzner_id']} ({server['ip']})")
    if deleted:
        lines.append("State: deleted (--cleanup-on-failure); Hetzner no longer lists it")
    else:
        lines.append(f"State: setup failed at {stage}")
        lines.append(f"Server retained: still billable{rate}")
        if cleanup and cleanup.get("error"):
            lines.append(f"Cleanup failed: {cleanup['error']}")
        elif cleanup and cleanup.get("skipped"):
            lines.append(f"Cleanup skipped: {cleanup['skipped']}")
        if details["remote_may_still_run"]:
            lines.append("Remote setup may still be running.")
    if diag_path:
        lines.append(f"Diagnostics: {diag_path}")
    else:
        lines.append(f"Diagnostics: not collected ({details.get('diagnostics_error', 'unknown error')})")
    if not deleted:
        lines += ["", f"Retry startup: devbox up {name}", f"Delete server: devbox down {name}"]
    return DevboxError("\n".join(lines), code=code, exit_code=exit_code, details=details)


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
    ("expose", "expose"), ("up", "up"), ("touch", "touch"), ("pull", "pull"),
    ("forward", "port forward"), ("db_dump", "database dump"), ("db_restore", "database restore"),
    ("ps", "ps"), ("logs", "logs"), ("stop", "stop"), ("ssh", "ssh"),
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


def snapshot_server(ctx: Context, server):
    """Shut the server down and snapshot it. On failure, try to power it back on."""
    name = server["project"]
    print(f"shutting down {name} for a snapshot...", file=sys.stderr)
    ctx.hcloud.shutdown(server["hetzner_id"])
    db.update_server(ctx.conn, server["id"], power="off")
    print("creating snapshot (this can take a few minutes)...", file=sys.stderr)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        info = ctx.hcloud.create_snapshot(server["hetzner_id"], name, f"devbox {name} {stamp}")
    except DevboxError as e:
        try:
            ctx.hcloud.poweron(server["hetzner_id"])
            db.update_server(ctx.conn, server["id"], power="running")
            kept = "the server was kept and powered on again"
        except DevboxError:
            kept = (
                f"the server was kept but is powered off (still billed); "
                f"`devbox up {name}` powers it on"
            )
        raise DevboxError(f"snapshot failed, {kept}: {e}")
    snap_id = db.insert_snapshot(
        ctx.conn, name, info["id"], server_id=server["id"],
        server_type=server["server_type"], location=server["location"],
        architecture=info["architecture"], size_gb=info["size_gb"], disk_gb=info["disk_gb"],
        price_per_gb_month=ctx.snapshot_price(), created_at=info.get("created"),
    )
    return db.get_snapshot(ctx.conn, snap_id)


def take_down(ctx: Context, server, snapshot: bool, event: str) -> dict:
    """Delete a project's server, optionally snapshotting it first. Caller holds the lock."""
    name = server["project"]
    snap = None
    pruned: list[int] = []
    if snapshot:
        latest = db.latest_snapshot(ctx.conn, name)
        if server["power"] in POWERED_DOWN and latest and latest["server_id"] == server["id"]:
            # An earlier pause saved this snapshot but failed to delete the
            # (already shut down) server; finish that instead of a second snapshot.
            print(f"reusing snapshot {latest['hetzner_image_id']} from the earlier pause...", file=sys.stderr)
            snap = latest
        else:
            snap = snapshot_server(ctx, server)

    try:
        ctx.hcloud.delete_server(server["hetzner_id"])
    except DevboxError as e:
        if snap is None:
            raise
        raise DevboxError(
            f"snapshot {snap['hetzner_image_id']} is saved, but deleting server {server['hetzner_id']} failed: {e}. "
            f"The server is shut down and still billed. Run `devbox pause {name}` again to finish "
            f"(it reuses this snapshot), or `devbox up {name}` to power the server back on."
        )
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
        dumps = []
        if getattr(ctx.args, "dump", False):
            if server["status"] != "running":
                raise DevboxError(f"server for {name} is not running; cannot dump databases")
            dbs = db.list_databases(ctx.conn, name)
            if not dbs:
                raise DevboxError(f"{name} has no databases to dump; see `devbox db add`")
            for database in dbs:
                print(f"dumping {database['name']}...", file=sys.stderr)
                dumps.append(dump_database(ctx, server, name, database))
        summary = take_down(ctx, server, snapshot, "pause" if snapshot else "down")
        remaining = db.latest_snapshot(ctx.conn, name)
    summary["dumps"] = dumps
    lines = [f"{'paused' if snapshot else 'deleted'} server {server['hetzner_id']} for {name}", *summary_lines(summary)]
    lines += [f"  dumped {d['database']} ({human_size(d['bytes'])}) to {d['path']}" for d in dumps]
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
            # Never interrupt work in progress: foreground `exec` commands and
            # keep-alive background jobs. If that can't be checked, do nothing.
            if server["power"] != "off":
                try:
                    busy = ctx.remote(server).running_work()
                except DevboxError as e:
                    entry["skipped"] = f"could not check for running work: {e}"
                    results.append(entry)
                    continue
                if busy:
                    entry["busy"] = busy
                    results.append(entry)
                    continue
            # Recheck activity right before acting; a command may have just started.
            latest = idle_info(ctx, db.get_server(ctx.conn, server["id"]), datetime.now(timezone.utc))
            if latest["remaining_seconds"] is not None and latest["remaining_seconds"] > 0:
                entry.update(latest)
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
        if e.get("busy"):
            kinds = ", ".join(sorted({b["kind"] for b in e["busy"]}))
            lines.append(f"{e['project']}: idle {idle} (limit {e['idle_timeout']}), but {len(e['busy'])} "
                         f"{kinds}(s) still running; left alone")
        elif e.get("skipped"):
            lines.append(f"{e['project']}: idle {idle} (limit {e['idle_timeout']}), {e['skipped']}; left alone")
        elif e["action"] is None:
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
            if server["status"] == "running" and server["power"] not in POWERED_DOWN:
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
            f"{e['project']}: {server_state(s)} {s['server_type']} {s['location']} {s['ip']} "
            f"(id {s['hetzner_id']}, up {e['uptime']})"
        )
        if s["power"] in POWERED_DOWN:
            lines.append(f"  powered off but still billed: `devbox up {e['project']}` starts it, "
                         f"`devbox pause {e['project']}` removes it")
        elif s["status"] == "creating" and s["setup_error"]:
            lines.append(f"  {s['setup_error'].splitlines()[0]}")
            if s["diagnostics"]:
                lines.append(f"  diagnostics: {s['diagnostics']}")
            lines.append(f"  still billed: `devbox up {e['project']}` retries setup, `devbox down {e['project']}` deletes it")
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
    if month["unknown_price"]:
        month_line += f", plus {month['unknown_price']} item(s) with unknown price"
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
    since, until = costs.resolve_period(a.month, a.days, a.since, a.until, now)
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
            f"{costs.hours(g['billed_hours']):>6} {cost:>9}"
        )
    lines.append(
        f"total {period}: {costs.money(summary['total'])} over {summary['servers']} "
        f"server{'s' if summary['servers'] != 1 else ''}, {costs.duration(summary['seconds'])} running"
        + (f", incl. {costs.money(summary['snapshot_total'])} for snapshots" if summary["snapshots"] else "")
    )
    if summary["unknown_price"]:
        lines.append(f"* {summary['unknown_price']} item(s) without a known price are not counted")
    lines.append(
        "Estimates incl. VAT: servers per started hour and snapshots per GB, both capped per calendar month, "
        "with each month's cost spread over the time running in it. IPv4 and traffic not included."
    )
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


STATUS_MARK = {admin.OK: "ok  ", admin.WARN: "warn", admin.FAIL: "FAIL"}


def cmd_doctor(ctx: Context) -> int:
    checks = admin.doctor(ctx, resolve_ssh_allow)
    lines = []
    for c in checks:
        lines.append(f"[{STATUS_MARK[c['status']]}] {c['check']}: {c['detail']}")
        if c["fix"] and c["status"] != admin.OK:
            lines.append(f"       fix: {c['fix']}")
    failed = any(c["status"] == admin.FAIL for c in checks)
    emit(ctx.args, {"ok": not failed, "checks": checks}, "\n".join(lines))
    return 1 if failed else 0


def cmd_cleanup(ctx: Context) -> int:
    with ctx.lock():
        plan = admin.cleanup_plan(ctx)
        if not plan:
            emit(ctx.args, {"items": [], "deleted": False}, "nothing to clean up")
            return 0
        if not ctx.args.yes:
            lines = ["would delete:"]
            lines += [f"  {i['kind']} {i['name']} ({i['id']}): {i['reason']}" for i in plan]
            lines.append("run `devbox cleanup --yes` to delete these")
            emit(ctx.args, {"items": plan, "deleted": False}, "\n".join(lines))
            return 0
        results = admin.run_cleanup(ctx, plan)
    lines = []
    for r in results:
        state = "deleted" if r["deleted"] else f"FAILED: {r['error']}"
        lines.append(f"{r['kind']} {r['name']} ({r['id']}): {state}")
    failed = any(not r["deleted"] for r in results)
    emit(ctx.args, {"items": results, "deleted": True}, "\n".join(lines))
    return 1 if failed else 0


def cmd_history(ctx: Context) -> int:
    if ctx.args.name and not db.get_project(ctx.conn, ctx.args.name) and not db.all_servers(ctx.conn, ctx.args.name):
        raise DevboxError(f"unknown project '{ctx.args.name}'")
    if ctx.args.limit is not None and ctx.args.limit < 1:
        raise DevboxError("--limit must be 1 or more")
    data = admin.history(ctx, ctx.args.name, ctx.args.limit)
    lines = []
    if data["servers"]:
        lines.append(f"{'STARTED':<16} {'PROJECT':<14} {'SERVER':<16} {'RAN':>7} {'COST':>7}  ACTIVITY")
    for e in data["servers"]:
        where = f"{e['server_type']} {e['location']}"
        counts = e["activity"]
        activity = ", ".join(
            f"{counts[k]} {label}{'s' if counts[k] != 1 else ''}" for k, label in ACTIVITY_LABELS if counts.get(k)
        )
        notes = []
        if e["restored_from_snapshot"]:
            notes.append(f"from snapshot {e['restored_from_snapshot']}")
        if e["status"] != "deleted":
            notes.append("running")
        elif counts.get("pause") or counts.get("idle_pause"):
            notes.append("paused" + (" (idle)" if counts.get("idle_pause") else ""))
        tail = activity + (f" [{'; '.join(notes)}]" if notes else "")
        lines.append(
            f"{e['created_at'][:16].replace('T', ' '):<16} {e['project']:<14} {where:<16} "
            f"{costs.duration(e['seconds']):>7} {costs.money(e['cost']):>7}  {tail}"
        )
    if data["snapshots"]:
        lines.append("")
        lines.append(f"{'SNAPSHOT':<10} {'PROJECT':<14} {'CREATED':<16} {'SIZE':>6} {'STATUS':<9} {'COST':>7}")
        for sn in data["snapshots"]:
            size = f"{sn['size_gb']:.1f}G" if sn["size_gb"] is not None else "?"
            lines.append(
                f"{sn['hetzner_image_id']:<10} {sn['project']:<14} {sn['created_at'][:16].replace('T', ' '):<16} "
                f"{size:>6} {sn['status']:<9} {costs.money(sn['cost']):>7}"
            )
    t = data["total"]
    if lines:
        lines.append("")
    lines.append(
        f"total: {costs.money(t['total'])} over {t['servers']} server{'s' if t['servers'] != 1 else ''} "
        f"and {t['snapshots']} snapshot{'s' if t['snapshots'] != 1 else ''}, {costs.duration(t['seconds'])} running"
    )
    emit(ctx.args, data, "\n".join(lines))
    return 0


def cmd_sync(ctx: Context) -> int:
    name = ctx.args.name
    server = ctx.running_server(name)
    project = db.require_project(ctx.conn, name)
    remote = ctx.remote(server)

    def do_sync() -> dict:
        result = remote.sync(project["path"], ctx.manifest(name))
        ctx.event(server, "sync")
        return result

    def describe(result: dict) -> str:
        if result["mode"] == "git":
            return f"synced {result['files']} files, deleted {result['deleted']}"
        return f"synced (warning: {result['warning']})"

    result = do_sync()
    if not ctx.args.watch:
        emit(ctx.args, {"project": name, **result}, describe(result))
        return 0

    if ctx.args.interval <= 0:
        raise DevboxError("--interval must be more than 0")
    root = Path(project["path"])
    print(f"{describe(result)}; watching {root} (Ctrl-C to stop)", file=sys.stderr)

    def on_change(paths: list[str]) -> None:
        shown = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
        r = do_sync()
        stamp = datetime.now().strftime("%H:%M:%S")
        if ctx.args.json:
            print(json.dumps({"at": stamp, "changed": paths, **r}), flush=True)
        else:
            print(f"{stamp} {shown}: {describe(r)}", flush=True)

    try:
        watch.watch(lambda: watch.snapshot(root), on_change, interval=ctx.args.interval)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_pull(ctx: Context) -> int:
    name = ctx.args.name
    server = ctx.running_server(name)
    project = db.require_project(ctx.conn, name)
    root = Path(project["path"])
    remote = ctx.remote(server)

    files = remote.list_files()
    on_server = set(files)
    if ctx.args.paths:
        prefixes = [p.strip("/") for p in ctx.args.paths]
        files = [f for f in files if any(f == p or f.startswith(p + "/") for p in prefixes)]
    elif not ctx.args.all:
        ignored = git_ignored(root, files)
        if ignored:
            files = [f for f in files if f not in ignored]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    staging = ctx.cfg.state_dir / "pulls" / name / stamp
    n = 1
    while staging.exists():
        n += 1
        staging = staging.with_name(f"{stamp}-{n}")
    pulled = remote.pull(files, staging, root) if files else []
    if not pulled:
        shutil.rmtree(staging, ignore_errors=True)

    entries = [{"path": p, "change": "changed" if (root / p).exists() else "new"} for p in pulled]
    missing = []
    if not ctx.args.paths:
        local = git_files(root) or []
        missing = [f for f in local if f not in on_server and not any(part in PULL_PRUNE for part in Path(f).parts)]
    ctx.event(server, "pull")

    data = {"project": name, "staging": str(staging) if pulled else None, "files": entries, "missing_on_server": missing}
    if not pulled:
        lines = ["no files on the server differ from the local copy"]
    else:
        lines = [f"{len(pulled)} file(s) differ on the server; copied to {staging}"]
        lines += [f"  {e['change']:<8} {e['path']}" for e in entries[:50]]
        if len(entries) > 50:
            lines.append(f"  ... and {len(entries) - 50} more")
        lines.append(f"review with: diff -ru {root} {staging}   (new files show as 'Only in')")
        lines.append(f"apply with:  rsync -a {staging}/ {root}/   (or copy single files)")
    if missing:
        lines.append(f"{len(missing)} local file(s) do not exist on the server (deleted there, or not synced yet):")
        lines += [f"  {m}" for m in missing[:20]]
    emit(ctx.args, data, "\n".join(lines))
    return 0


def parse_port_spec(spec: str) -> tuple[int, int]:
    local, sep, remote_port = spec.partition(":")
    if not sep:
        remote_port = local
    try:
        ports = int(local), int(remote_port)
    except ValueError:
        raise DevboxError(f"invalid port '{spec}': use PORT or LOCAL:REMOTE, e.g. 5432 or 15432:5432")
    if not all(1 <= p <= 65535 for p in ports):
        raise DevboxError(f"invalid port '{spec}'")
    return ports


def cmd_forward(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    ports = [parse_port_spec(p) for p in ctx.args.ports]
    ctx.event(server, "forward")
    cmd = ctx.remote(server).forward_cmd(ports)
    for local, remote_port in ports:
        print(f"forwarding localhost:{local} -> {ctx.args.name}:{remote_port}", file=sys.stderr)
    print("Ctrl-C to stop", file=sys.stderr)
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 0


# Secrets


def secret_store(ctx: Context, name: str) -> envsecrets.SecretStore:
    return envsecrets.SecretStore(config.config_dir(), name)


def push_secrets(ctx: Context, server, name: str) -> dict[str, int]:
    """Write stored secrets into the server's env files. Returns file -> number of keys."""
    files = secret_store(ctx, name).load()
    remote = ctx.remote(server)
    written = {}
    for path, values in sorted(files.items()):
        existing = remote.read_file(path) or ""
        remote.write_file(path, envsecrets.merge_env(existing, values))
        written[path] = len(values)
    return written


def cmd_secrets_set(ctx: Context) -> int:
    name = ctx.args.name
    db.require_project(ctx.conn, name)
    values = dict(envsecrets.parse_assignment(a) for a in ctx.args.assignments)
    if ctx.args.from_file:
        text = sys.stdin.read() if ctx.args.from_file == "-" else Path(ctx.args.from_file).read_text()
        values = {**envsecrets.parse_env(text), **values}
    if not values:
        raise DevboxError("nothing to set: pass KEY=VALUE or --from-file")
    file = envsecrets.validate_file(ctx.args.file)
    secret_store(ctx, name).set(file, values)
    pushed = None
    server = db.active_server(ctx.conn, name)
    if server and server["status"] == "running" and not ctx.args.no_push:
        pushed = push_secrets(ctx, server, name)
        ctx.event(server, "secrets")
    text = f"stored {len(values)} key(s) for {file}: {', '.join(sorted(values))}"
    if pushed is not None:
        text += "; written to the server"
    emit(ctx.args, {"project": name, "file": file, "keys": sorted(values), "pushed": pushed is not None}, text)
    return 0


def cmd_secrets_list(ctx: Context) -> int:
    name = ctx.args.name
    db.require_project(ctx.conn, name)
    summary = secret_store(ctx, name).summary()
    lines = [f"{f}: {', '.join(keys)}" for f, keys in summary.items()]
    emit(ctx.args, {"project": name, "files": summary}, "\n".join(lines) or f"no secrets stored for {name}")
    return 0


def cmd_secrets_unset(ctx: Context) -> int:
    name = ctx.args.name
    db.require_project(ctx.conn, name)
    file = envsecrets.validate_file(ctx.args.file)
    removed = secret_store(ctx, name).unset(file, ctx.args.keys)
    emit(
        ctx.args, {"project": name, "file": file, "removed": removed},
        f"removed {', '.join(removed) or 'nothing'} from stored secrets for {file} "
        "(the server's env file is unchanged)",
    )
    return 0


def cmd_secrets_push(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    written = push_secrets(ctx, server, ctx.args.name)
    ctx.event(server, "secrets")
    lines = [f"{path}: {n} key(s) written" for path, n in written.items()]
    emit(ctx.args, {"project": ctx.args.name, "written": written}, "\n".join(lines) or "no secrets stored")
    return 0


# Databases


def pick_database(ctx: Context, project: str, name: str | None) -> dict:
    dbs = db.list_databases(ctx.conn, project)
    if name:
        found = db.get_database(ctx.conn, project, name)
        if not found:
            raise DevboxError(f"{project} has no database '{name}'; see `devbox db list {project}`")
        return found
    if len(dbs) == 1:
        return dbs[0]
    if not dbs:
        raise DevboxError(f"{project} has no databases; add one with `devbox db add {project} NAME --engine ...`")
    raise DevboxError(f"{project} has several databases ({', '.join(d['name'] for d in dbs)}); name one")


def dumps_dir(ctx: Context, project: str) -> Path:
    return ctx.cfg.state_dir / "dumps" / project


def list_dumps(ctx: Context, project: str, database: str | None = None) -> list[dict]:
    folder = dumps_dir(ctx, project)
    if not folder.is_dir():
        return []
    result = []
    for f in sorted(folder.iterdir(), key=lambda p: p.name, reverse=True):
        if not f.is_file() or f.name.endswith(".partial"):
            continue
        db_name = f.name.rsplit("-", 2)[0]
        if database and db_name != database:
            continue
        result.append({"database": db_name, "path": str(f), "bytes": f.stat().st_size, "name": f.name})
    return result


def dump_database(ctx: Context, server, project: str, database: dict, output: Path | None = None) -> dict:
    engine = databases.get_engine(database["engine"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = output or dumps_dir(ctx, project) / f"{database['name']}-{stamp}.{engine.extension}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = ctx.remote(server).stream_out(engine.dump_command(database["settings"]), dest)
    ctx.event(server, "db_dump")
    return {"database": database["name"], "engine": engine.name, "path": str(dest), "bytes": size}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_db_engines(ctx: Context) -> int:
    data = [
        {"engine": e.name, "description": e.description,
         "options": [{"name": o.name, "required": o.required, "default": o.default, "help": o.help} for o in e.options]}
        for e in databases.ENGINES.values()
    ]
    lines = []
    for e in data:
        lines.append(f"{e['engine']}: {e['description']}")
        for o in e["options"]:
            flag = "required" if o["required"] else (f"default {o['default']}" if o["default"] else "optional")
            lines.append(f"  {o['name']:<10} {flag:<16} {o['help']}")
    emit(ctx.args, data, "\n".join(lines))
    return 0


def cmd_db_add(ctx: Context) -> int:
    engine = databases.get_engine(ctx.args.engine)
    settings = engine.validate(databases.parse_options(ctx.args.option or []))
    db.add_database(ctx.conn, ctx.args.name, ctx.args.database, engine.name, settings)
    emit(
        ctx.args, {"project": ctx.args.name, "database": ctx.args.database, "engine": engine.name, "settings": settings},
        f"added {engine.name} database '{ctx.args.database}' to {ctx.args.name} ({engine.describe(settings)})",
    )
    return 0


def cmd_db_list(ctx: Context) -> int:
    name = ctx.args.name
    db.require_project(ctx.conn, name)
    dbs = db.list_databases(ctx.conn, name)
    dumps = list_dumps(ctx, name)
    data = []
    lines = []
    for d in dbs:
        own = [x for x in dumps if x["database"] == d["name"]]
        data.append({**d, "dumps": own})
        engine = databases.get_engine(d["engine"])
        latest = f"latest dump {own[0]['name']} ({human_size(own[0]['bytes'])})" if own else "no dumps"
        lines.append(f"{d['name']}: {d['engine']} ({engine.describe(d['settings'])}), {latest}")
    emit(ctx.args, data, "\n".join(lines) or f"no databases for {name}; see `devbox db engines`")
    return 0


def cmd_db_remove(ctx: Context) -> int:
    db.require_project(ctx.conn, ctx.args.name)
    if not db.remove_database(ctx.conn, ctx.args.name, ctx.args.database):
        raise DevboxError(f"{ctx.args.name} has no database '{ctx.args.database}'")
    emit(ctx.args, {"removed": ctx.args.database}, f"removed database '{ctx.args.database}' (dump files are kept)")
    return 0


def cmd_db_dump(ctx: Context) -> int:
    name = ctx.args.name
    server = ctx.running_server(name)
    database = pick_database(ctx, name, ctx.args.database)
    output = Path(ctx.args.output).expanduser().resolve() if ctx.args.output else None
    result = dump_database(ctx, server, name, database, output)
    emit(ctx.args, result, f"dumped {result['database']} ({human_size(result['bytes'])}) to {result['path']}")
    return 0


def cmd_db_restore(ctx: Context) -> int:
    name = ctx.args.name
    server = ctx.running_server(name)
    database = pick_database(ctx, name, ctx.args.database)
    if ctx.args.file:
        src = Path(ctx.args.file).expanduser().resolve()
        if not src.is_file():
            raise DevboxError(f"{src} does not exist")
    else:
        dumps = list_dumps(ctx, name, database["name"])
        if not dumps:
            raise DevboxError(f"no dumps of '{database['name']}'; pass --file or run `devbox db dump {name}`")
        src = Path(dumps[0]["path"])
    if not ctx.args.yes:
        raise DevboxError(
            f"this replaces the data in '{database['name']}' on the server with {src.name}; "
            "pass --yes to confirm"
        )
    engine = databases.get_engine(database["engine"])
    ctx.remote(server).stream_in(engine.restore_command(database["settings"]), src)
    ctx.event(server, "db_restore")
    emit(ctx.args, {"database": database["name"], "restored_from": str(src)}, f"restored {database['name']} from {src}")
    return 0


def cmd_exec(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    script = to_script(ctx.args.cmd)
    remote = ctx.remote(server)

    if ctx.args.keep_alive and not ctx.args.detach:
        raise DevboxError("--keep-alive only applies to --detach (foreground commands always count as running work)")
    if ctx.args.detach:
        if not ctx.args.proc:
            raise DevboxError("--detach needs --name PROC")
        if ctx.args.timeout is not None:
            raise DevboxError("--timeout only applies to foreground commands")
        validate_proc(ctx.args.proc)
        if ctx.args.proc == TUNNEL:
            raise DevboxError(f"'{TUNNEL}' is reserved for the preview tunnel; use `devbox expose`")
        remote.start_unit(ctx.args.proc, script, keep_alive=ctx.args.keep_alive)
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
    ctx.event(server, "ps")
    lines = [
        f"{u['name']:<20} {u['active']:<10} {u['sub']}{'  (keep-alive)' if u['keep_alive'] else ''}" for u in units
    ]
    emit(ctx.args, units, "\n".join(lines) or "no background processes")
    return 0


def cmd_stop(ctx: Context) -> int:
    server = ctx.running_server(ctx.args.name)
    validate_proc(ctx.args.proc)
    stopped = ctx.remote(server).stop_unit(ctx.args.proc)
    ctx.event(server, "stop")
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
    ctx.event(server, "logs")
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
    ctx.event(server, "ssh")
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
    p.add_argument("--startup-timeout", metavar="DURATION", help="deadline for `up`, e.g. 10m, or 'default'")
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
        p.add_argument("--startup-timeout", metavar="DURATION", help="deadline for the whole startup, e.g. 5m (default from config/project)")
        p.add_argument("--cleanup-on-failure", action="store_true",
                       help="if startup fails, delete the server, but only a fresh one created by this run")
        p.set_defaults(require_snapshot=require_snapshot, fresh=False)
        if not require_snapshot:
            p.add_argument("--fresh", action="store_true", help="ignore snapshots and start from a clean image")

    p = add("down", cmd_down, "delete the server")
    p.add_argument("name")
    p.add_argument("--snapshot", action="store_true", help="snapshot the server first (same as pause)")
    p.add_argument("--dump", action="store_true", help="dump all project databases locally first")

    p = add("pause", cmd_down, "snapshot the server, then delete it; `up` restores it")
    p.add_argument("name")
    p.add_argument("--dump", action="store_true", help="also dump all project databases locally first")
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

    p = add("doctor", cmd_doctor, "check config, tools, Hetzner access, SSH key, firewall and leftovers")

    p = add("cleanup", cmd_cleanup, "list unused devbox resources; delete them with --yes")
    p.add_argument("--yes", action="store_true", help="really delete the listed resources")

    p = add("history", cmd_history, "past servers and snapshots with dates, cost and activity")
    p.add_argument("name", nargs="?")
    p.add_argument("--limit", type=int, metavar="N", help="only the N most recent servers and snapshots")

    p = add("costs", cmd_costs, "estimated server and snapshot costs for a period, grouped")
    p.add_argument("--project", help="only this project")
    period = p.add_mutually_exclusive_group()
    period.add_argument("--month", metavar="YYYY-MM", help="one calendar month (UTC)")
    period.add_argument("--days", metavar="N", help="the last N days (1 or more)")
    period.add_argument("--since", metavar="YYYY-MM-DD", help="from this day (UTC)")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="up to, not including, this day")
    p.add_argument("--by", choices=costs.GROUPINGS, default="project", help="grouping (default: project)")

    p = add("status", cmd_status, "show servers, processes, preview URLs and cost so far")
    p.add_argument("name", nargs="?")

    p = add("sync", cmd_sync, "copy local code to the server")
    p.add_argument("name")
    p.add_argument("--watch", action="store_true", help="keep running and sync whenever local files change")
    p.add_argument("--interval", type=float, default=1.0, metavar="SECONDS", help="how often --watch checks (default 1)")

    p = add("pull", cmd_pull, "copy files that differ on the server into a local staging folder for review")
    p.add_argument("name")
    p.add_argument("paths", nargs="*", help="only these files or folders (Git-ignored ones included)")
    p.add_argument("--all", action="store_true", help="include files Git ignores (dependencies are still skipped)")

    p = add("forward", cmd_forward, "forward local ports to ports on the server until Ctrl-C")
    p.add_argument("name")
    p.add_argument("ports", nargs="+", metavar="PORT", help="REMOTE or LOCAL:REMOTE, e.g. 5432 or 15432:5432")

    sec = sub.add_parser("secrets", help="env values stored locally and written to the server", parents=[common])
    sec_sub = sec.add_subparsers(dest="secrets_command", required=True)
    p = sec_sub.add_parser("set", help="store KEY=VALUE pairs (written to the server now and on every up)", parents=[common])
    p.add_argument("name")
    p.add_argument("assignments", nargs="*", metavar="KEY=VALUE")
    p.add_argument("--file", default=envsecrets.DEFAULT_FILE, help="env file in the project (default .env)")
    p.add_argument("--from-file", metavar="PATH", help="read KEY=VALUE lines from a .env file ('-' for stdin)")
    p.add_argument("--no-push", action="store_true", help="only store; don't write to a running server")
    p.set_defaults(func=cmd_secrets_set)
    p = sec_sub.add_parser("list", help="show stored key names (never values)", parents=[common])
    p.add_argument("name")
    p.set_defaults(func=cmd_secrets_list)
    p = sec_sub.add_parser("unset", help="forget stored keys", parents=[common])
    p.add_argument("name")
    p.add_argument("keys", nargs="+", metavar="KEY")
    p.add_argument("--file", default=envsecrets.DEFAULT_FILE)
    p.set_defaults(func=cmd_secrets_unset)
    p = sec_sub.add_parser("push", help="write stored secrets to the running server", parents=[common])
    p.add_argument("name")
    p.set_defaults(func=cmd_secrets_push)

    dbp = sub.add_parser("db", help="database dumps and restores", parents=[common])
    db_sub = dbp.add_subparsers(dest="db_command", required=True)
    p = db_sub.add_parser("engines", help="supported engines and their options", parents=[common])
    p.set_defaults(func=cmd_db_engines)
    p = db_sub.add_parser("add", help="describe a project database", parents=[common])
    p.add_argument("name")
    p.add_argument("database", help="a short name for this database, e.g. main")
    p.add_argument("--engine", required=True, choices=sorted(databases.ENGINES))
    p.add_argument("--option", action="append", metavar="KEY=VALUE", help="engine option (see `devbox db engines`)")
    p.set_defaults(func=cmd_db_add)
    p = db_sub.add_parser("list", help="project databases and their latest dumps", parents=[common])
    p.add_argument("name")
    p.set_defaults(func=cmd_db_list)
    p = db_sub.add_parser("remove", help="forget a database (dump files are kept)", parents=[common])
    p.add_argument("name")
    p.add_argument("database")
    p.set_defaults(func=cmd_db_remove)
    p = db_sub.add_parser("dump", help="dump a database from the server to a local file", parents=[common])
    p.add_argument("name")
    p.add_argument("database", nargs="?")
    p.add_argument("--output", "-o", metavar="FILE", help="default: ~/.local/state/devbox/dumps/NAME/")
    p.set_defaults(func=cmd_db_dump)
    p = db_sub.add_parser("restore", help="replace a database on the server with a dump", parents=[common])
    p.add_argument("name")
    p.add_argument("database", nargs="?")
    p.add_argument("--file", metavar="FILE", help="default: the latest dump of this database")
    p.add_argument("--yes", action="store_true", help="confirm replacing the data")
    p.set_defaults(func=cmd_db_restore)

    p = add(
        "exec", cmd_exec, "run a command on the server",
        description="Run a command in /srv/app. A single argument is run as a shell script; "
        "several arguments are quoted as one command.",
    )
    p.add_argument("name")
    p.add_argument("--detach", action="store_true", help="run in the background as a named process")
    p.add_argument("--name", dest="proc", metavar="PROC", help="process name for --detach")
    p.add_argument(
        "--keep-alive", action="store_true",
        help="with --detach: idle shutdown waits while this process runs (for long builds, not dev servers)",
    )
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
            print(json.dumps({"error": str(e), **({"code": e.code} if e.code else {}), **e.details}, default=str))
        else:
            print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
