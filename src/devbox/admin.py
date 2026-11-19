"""Housekeeping commands: doctor, cleanup and history.

These take the CLI `Context` and return plain data; formatting lives in cli.py.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from devbox import DevboxError, config, costs, db

OK, WARN, FAIL = "ok", "warn", "fail"


def check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


# Doctor


def doctor(ctx, resolve_ssh_allow) -> list[dict]:
    checks: list[dict] = []
    add = checks.append

    try:
        cfg = ctx.cfg
    except DevboxError as e:
        add(check("config", FAIL, str(e), "fix ~/.config/devbox/config.toml"))
        return checks
    add(check("config", OK, f"controller id {cfg.get_controller_id()}, state in {cfg.state_dir}"))

    missing = [t for t in ("ssh", "ssh-keygen", "rsync", "git") if shutil.which(t) is None]
    if missing:
        add(check("local tools", FAIL, f"missing: {', '.join(missing)}", "install them with your package manager"))
    else:
        add(check("local tools", OK, "ssh, ssh-keygen, rsync and git found"))
    if cfg.preview_domain:
        add(cloudflare_check(cfg))

    provider = ctx.provider
    found, usable = provider.check_connection(cfg)
    checks += found
    if not usable:
        return checks + registry_checks(ctx)

    pub = cfg.ssh_key.with_name(cfg.ssh_key.name + ".pub")
    if not cfg.ssh_key.exists() or not pub.exists():
        add(check("local ssh key", WARN, f"{cfg.ssh_key} does not exist yet", "it is created on the first `devbox up`"))
        public_key = None
    else:
        add(check("local ssh key", OK, str(cfg.ssh_key)))
        public_key = pub.read_text().strip()

    checks += provider.check_access(public_key, lambda: resolve_ssh_allow(cfg.ssh_allow))

    checks += registry_checks(ctx)

    try:
        notes: list[str] = []
        plan = cleanup_plan(ctx, notes)
    except DevboxError as e:
        add(check("leftovers", WARN, f"could not check: {e}"))
    else:
        cloud = [c for c in plan if c["kind"] != "local file"]
        if cloud:
            add(check("leftovers", WARN, f"{len(cloud)} unused devbox resource(s) in {ctx.provider.display_name} or Cloudflare",
                      "see `devbox cleanup`"))
        else:
            add(check("leftovers", OK, "no unused devbox resources"))
        for note in notes:
            add(check("leftovers", WARN, note))

    checks.append(schedule_check(ctx))
    budget = cfg.monthly_budget
    add(check("budget", OK, f"{costs.money(budget)} per month ({cfg.budget_action})" if budget else "no monthly budget set"))
    return checks


def cloudflare_check(cfg) -> dict:
    """Can stable previews be set up: token, zone, and access to Cloudflare Tunnel?"""
    from devbox import previews

    fix = ("create a token with Account > Cloudflare Tunnel > Edit and Zone > DNS > Edit for the zone, "
           "and put it in cloudflare_token_file")
    try:
        cf = previews.Cloudflare(cfg.cloudflare_token())
        zone = cf.zone(cfg.preview_domain)
        status = cf.verify_token(zone["account"])
        if status != "active":
            return check("cloudflare", FAIL, f"the Cloudflare token is {status}", fix)
        cf.tunnels(zone["account"], previews.tunnel_name(cfg.get_controller_id(), ""))
    except DevboxError as e:
        return check("cloudflare", FAIL, str(e), fix)
    detail = f"token active, zone {zone['name']}, tunnels readable"
    if cfg.preview_domain != zone["name"]:
        return check("cloudflare", WARN, f"{detail}; previews like x.{cfg.preview_domain} are more than one level "
                     f"below {zone['name']}, which Cloudflare's free certificate doesn't cover",
                     f"set preview_domain = \"{zone['name']}\" or order an Advanced Certificate")
    return check("cloudflare", OK, detail + " (DNS edit rights are only tested by the first `expose`)")


def registry_checks(ctx) -> list[dict]:
    try:
        conn = ctx.conn
    except Exception as e:  # sqlite errors, permissions
        return [check("registry", FAIL, f"cannot open {ctx.cfg.db_path}: {e}")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    projects = db.list_projects(conn)
    result = [check("registry", OK, f"{ctx.cfg.db_path}, schema {version}, {len(projects)} project(s)")]
    gone = [p["name"] for p in projects if not Path(p["path"]).is_dir()]
    if gone:
        result.append(check("project paths", WARN, f"missing folders for: {', '.join(gone)}",
                            "re-add them with `devbox project add NAME --path PATH` after `project remove`"))
    return result


CRON_ENTRY_RE = re.compile(
    r"^(@\w+|(\S+\s+){4}\S+)\s+.*\bdevbox\s+idle-check\b"
)


def cron_has_idle_check(crontab: str) -> bool:
    """An active (uncommented) cron entry whose command runs `devbox idle-check`."""
    for line in crontab.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if CRON_ENTRY_RE.match(line):
            return True
    return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


def schedule_check(ctx) -> dict:
    from devbox import autostop

    if config.parse_idle_timeout(ctx.cfg.idle_timeout) is None:
        return check("idle shutdown", OK, "idle_timeout is off")
    return autostop.enforcement(ctx.cfg)


def idle_scheduler(crontab: str | None = None) -> dict:
    """Whether `devbox idle-check` is scheduled on this machine (cron or the systemd user timer).

    `crontab` replaces reading the user's crontab (autostop passes it without
    its own entries). Only detects a schedule; it can't tell which config or
    controller the job uses.
    """
    if crontab is None:
        cron = _run(["crontab", "-l"])
        crontab = cron.stdout if cron and cron.returncode == 0 else ""
    if cron_has_idle_check(crontab):
        return check("idle shutdown", OK, "scheduled (cron)")
    timer = "devbox-idle-check.timer"
    enabled = _run(["systemctl", "--user", "is-enabled", timer])
    active = _run(["systemctl", "--user", "is-active", timer])
    is_enabled = bool(enabled and enabled.returncode == 0 and enabled.stdout.strip() == "enabled")
    is_active = bool(active and active.returncode == 0 and active.stdout.strip() == "active")
    if is_enabled and is_active:
        return check("idle shutdown", OK, "scheduled (systemd timer)")
    if is_enabled:
        return check("idle shutdown", WARN, f"{timer} is enabled but not active, so idle servers keep running",
                     f"systemctl --user start {timer}")
    return check("idle shutdown", WARN, "`devbox idle-check` does not seem to be scheduled, so idle servers keep running",
                 "devbox autostop enable")


# Cleanup


def cleanup_plan(ctx, notes: list[str] | None = None) -> list[dict]:
    """Everything devbox created that is no longer used. Nothing is deleted here.

    Parts that can't be decided safely are skipped and explained in `notes`.
    """
    notes = notes if notes is not None else []
    plan: list[dict] = []
    orphans = ctx.reconcile()
    for s in orphans:
        plan.append({"kind": "server", "id": s["id"], "name": s["name"],
                     "reason": f"labeled devbox server not in the registry (project {s.get('project') or '?'})"})
    for i in ctx.snapshot_orphans:
        plan.append({"kind": "snapshot", "id": i["id"], "name": i.get("description"),
                     "reason": f"labeled devbox snapshot not in the registry (project {i.get('project') or '?'})"})

    pub = ctx.cfg.ssh_key.with_name(ctx.cfg.ssh_key.name + ".pub")
    local_key = pub.read_text().strip() if pub.exists() else None
    plan += ctx.provider.leftovers(local_key, notes)

    active = {(s["project"], str(s["provider_id"])) for s in db.active_servers(ctx.conn)}
    known_hosts = ctx.cfg.state_dir / "known_hosts"
    if known_hosts.is_dir():
        for f in sorted(known_hosts.iterdir()):
            project, _, server_id = f.name.rpartition("-")
            if (project, server_id) not in active:
                plan.append({"kind": "local file", "id": str(f), "name": f.name,
                             "reason": "SSH host keys of a server that no longer exists"})
    projects = {p["name"] for p in db.list_projects(ctx.conn)}
    plan += cloudflare_leftovers(ctx, projects, notes)
    manifests = ctx.cfg.state_dir / "manifests"
    if manifests.is_dir():
        for f in sorted(manifests.glob("*.json")):
            if f.stem not in projects:
                plan.append({"kind": "local file", "id": str(f), "name": f.name,
                             "reason": "sync manifest of a removed project"})
    return plan


def cloudflare_leftovers(ctx, projects: set[str], notes: list[str]) -> list[dict]:
    """Preview tunnels (and their DNS records) of projects that were removed."""
    from devbox import previews

    if not ctx.cfg.preview_domain:
        return []
    try:
        cf = previews.Cloudflare(ctx.cfg.cloudflare_token())
        zone = cf.zone(ctx.cfg.preview_domain)
        prefix = previews.tunnel_name(ctx.cfg.get_controller_id(), "")
        plan = []
        for t in cf.tunnels(zone["account"], prefix):
            project = t["name"][len(prefix):]
            if project in projects:
                continue
            for r in cf.tunnel_records(zone["id"], t["id"]):
                plan.append({"kind": "dns record", "id": r["id"], "name": r["name"], "zone": zone["id"],
                             "reason": f"preview hostname of removed project {project}"})
            plan.append({"kind": "cloudflare tunnel", "id": t["id"], "name": t["name"], "account": zone["account"],
                         "reason": f"preview tunnel of removed project {project}"})
        return plan
    except DevboxError as e:
        notes.append(f"Cloudflare preview tunnels not checked: {e}")
        return []


def run_cleanup(ctx, plan: list[dict]) -> list[dict]:
    results = []
    for item in plan:
        try:
            if item["kind"] == "server":
                ctx.provider.delete_server(item["id"])
            elif item["kind"] == "snapshot":
                ctx.provider.delete_image(item["id"])
            elif item["kind"] in ("firewall", "ssh-key"):
                ctx.provider.delete_resource(item["kind"], item["id"])
            elif item["kind"] == "local file":
                Path(item["id"]).unlink(missing_ok=True)
            elif item["kind"] == "dns record":
                cloudflare(ctx).delete_record(item["zone"], item["id"])
            elif item["kind"] == "cloudflare tunnel":
                cloudflare(ctx).delete_tunnel(item["account"], item["id"])
            results.append({**item, "deleted": True})
        except DevboxError as e:
            results.append({**item, "deleted": False, "error": str(e)})
    return results


def cloudflare(ctx):
    from devbox import previews

    return previews.Cloudflare(ctx.cfg.cloudflare_token())


# History


def history(ctx, project: str | None, limit: int | None) -> dict:
    conn = ctx.conn
    snapshots = {s["id"]: s for s in db.snapshots(conn, project, include_deleted=True)}
    servers = list(reversed(db.all_servers(conn, project)))
    if limit:
        servers = servers[:limit]
    entries = []
    for s in servers:
        usage = costs.server_usage(s)
        restored = snapshots.get(s["snapshot_id"]) if s["snapshot_id"] else None
        entries.append({
            "project": s["project"],
            "provider_id": s["provider_id"],
            "hetzner_id": s["provider_id"],
            "provider": s["provider"],
            "server_type": s["server_type"],
            "location": s["location"],
            "status": s["status"],
            "created_at": s["created_at"],
            "deleted_at": s["deleted_at"],
            "seconds": usage["seconds"],
            "billed_hours": usage["billed_hours"],
            "cost": usage["cost"],
            "restored_from_snapshot": restored["image_id"] if restored else None,
            "startup": json.loads(s["startup"]) if s["startup"] else None,
            "activity": db.event_counts(conn, s["id"]),
        })
    snaps = []
    for s in sorted(snapshots.values(), key=lambda r: r["created_at"], reverse=True):
        item = costs.snapshot_item(s)
        snaps.append({
            "project": s["project"],
            "image_id": s["image_id"],
            "hetzner_image_id": s["image_id"],
            "status": s["status"],
            "created_at": s["created_at"],
            "deleted_at": s["deleted_at"],
            "size_gb": s["size_gb"],
            "monthly_cost": item["price_monthly"],
            "cost": costs.server_usage(item)["cost"],
        })
    if limit:
        snaps = snaps[:limit]
    return {"servers": entries, "snapshots": snaps, "total": costs.summarize(ctx.cost_items(project))}


def same_public_key(a: str, b: str) -> bool:
    return a.split()[:2] == b.split()[:2]
