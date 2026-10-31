"""Housekeeping commands: doctor, cleanup and history.

These take the CLI `Context` and return plain data; formatting lives in cli.py.
"""

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

    if shutil.which("hcloud") is None:
        add(check("hcloud", FAIL, "hcloud CLI not found", "install it from https://github.com/hetznercloud/cli"))
        return checks + registry_checks(ctx)
    add(check("hcloud", OK, f"version {ctx.hcloud.version() or 'unknown'}"))

    try:
        token = cfg.hcloud_token()
    except DevboxError as e:
        add(check("token", FAIL, str(e), "check hcloud_token_file in the config"))
        return checks + registry_checks(ctx)
    if not token:
        add(check("token", WARN, "no hcloud_token_file or HCLOUD_TOKEN; relying on the active hcloud context",
                  "set hcloud_token_file in ~/.config/devbox/config.toml"))
    else:
        add(check("token", OK, "token found"))

    try:
        servers = ctx.hcloud.list_servers()
    except DevboxError as e:
        add(check("hetzner api", FAIL, str(e), "check the token and network access"))
        return checks + registry_checks(ctx)
    add(check("hetzner api", OK, f"reachable, {len(servers)} devbox server(s)"))

    try:
        offers = ctx.hcloud.offers()
        if offers and all(o["available"] is None for o in offers):
            add(check("availability", WARN, "hcloud does not report per-location availability",
                      "update hcloud to a recent version"))
        else:
            add(check("availability", OK, f"{sum(1 for o in offers if o['available'])} type/location offers available"))
    except DevboxError as e:
        add(check("availability", WARN, str(e)))

    pub = cfg.ssh_key.with_name(cfg.ssh_key.name + ".pub")
    if not cfg.ssh_key.exists() or not pub.exists():
        add(check("local ssh key", WARN, f"{cfg.ssh_key} does not exist yet", "it is created on the first `devbox up`"))
        public_key = None
    else:
        add(check("local ssh key", OK, str(cfg.ssh_key)))
        public_key = pub.read_text().strip()

    if public_key:
        try:
            found = ctx.hcloud.find_ssh_key(public_key)
            key = found["key"]
            if found["status"] == "owned":
                add(check("hetzner ssh key", OK, f"'{key['name']}' (id {key['id']})"))
            elif found["status"] == "legacy":
                add(check("hetzner ssh key", OK, f"using the key '{key['name']}' (id {key['id']}) from an older devbox version"))
            else:
                add(check("hetzner ssh key", OK, f"not registered yet; `up` creates '{ctx.hcloud.resource_name}'"))
        except DevboxError as e:
            add(check("hetzner ssh key", FAIL, str(e)))

    try:
        fw = ctx.hcloud.find_firewall()
        if fw is None:
            add(check("firewall", OK, f"not created yet; `up` creates '{ctx.hcloud.resource_name}'"))
        else:
            current = set(resolve_ssh_allow(cfg.ssh_allow))
            allowed = {ip for rule in fw.get("rules") or [] for ip in rule.get("source_ips") or []}
            if current <= allowed:
                add(check("firewall", OK, f"'{fw['name']}' allows SSH from {', '.join(sorted(current))}"))
            else:
                add(check("firewall", WARN,
                          f"'{fw['name']}' allows {', '.join(sorted(allowed)) or 'nothing'}, this machine is "
                          f"{', '.join(sorted(current))}",
                          "the next `devbox up` updates the rules; running servers can't be reached until then"))
    except DevboxError as e:
        add(check("firewall", FAIL, str(e)))

    checks += registry_checks(ctx)

    try:
        plan = cleanup_plan(ctx)
    except DevboxError as e:
        add(check("leftovers", WARN, f"could not check: {e}"))
    else:
        cloud = [c for c in plan if c["kind"] != "local file"]
        if cloud:
            add(check("leftovers", WARN, f"{len(cloud)} unused devbox resource(s) in Hetzner",
                      "see `devbox cleanup`"))
        else:
            add(check("leftovers", OK, "no unused devbox resources"))

    checks.append(schedule_check(ctx))
    budget = cfg.monthly_budget
    add(check("budget", OK, f"{costs.money(budget)} per month ({cfg.budget_action})" if budget else "no monthly budget set"))
    return checks


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


def schedule_check(ctx) -> dict:
    if config.parse_duration(ctx.cfg.idle_timeout) is None:
        return check("idle shutdown", OK, "idle_timeout is off")
    for cmd in (["crontab", "-l"], ["systemctl", "--user", "is-enabled", "devbox-idle-check.timer"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and ("idle-check" in proc.stdout or proc.stdout.strip() == "enabled"):
            return check("idle shutdown", OK, f"scheduled ({cmd[0]}), timeout {ctx.cfg.idle_timeout}")
    return check("idle shutdown", WARN, "`devbox idle-check` does not seem to be scheduled, so idle servers keep running",
                 "add the cron line or systemd timer from the README")


# Cleanup


def cleanup_plan(ctx) -> list[dict]:
    """Everything devbox created that is no longer used. Nothing is deleted here."""
    plan: list[dict] = []
    orphans = ctx.reconcile()
    for s in orphans:
        plan.append({"kind": "server", "id": s["id"], "name": s["name"],
                     "reason": f"labeled devbox server not in the registry (project {s.get('project') or '?'})"})
    for i in ctx.snapshot_orphans:
        plan.append({"kind": "snapshot", "id": i["id"], "name": i.get("description"),
                     "reason": f"labeled devbox snapshot not in the registry (project {i.get('project') or '?'})"})

    hc = ctx.hcloud
    for fw in hc.list_firewalls():
        if fw["name"] == hc.resource_name or not (hc.owns(fw) or hc.is_legacy(fw)):
            continue
        if fw.get("applied_to"):
            continue
        plan.append({"kind": "firewall", "id": fw["id"], "name": fw["name"],
                     "reason": "devbox firewall not attached to any server" + (" (older version)" if hc.is_legacy(fw) else "")})

    pub = ctx.cfg.ssh_key.with_name(ctx.cfg.ssh_key.name + ".pub")
    if pub.exists():
        try:
            in_use = hc.find_ssh_key(pub.read_text().strip())["key"]
        except DevboxError:
            in_use = None
        in_use_id = in_use["id"] if in_use else None
        for key in hc.list_ssh_keys():
            if key["id"] == in_use_id or not (hc.owns(key) or hc.is_legacy(key)):
                continue
            plan.append({"kind": "ssh-key", "id": key["id"], "name": key["name"],
                         "reason": "devbox SSH key that is not the local key"})

    active = {(s["project"], str(s["hetzner_id"])) for s in db.active_servers(ctx.conn)}
    known_hosts = ctx.cfg.state_dir / "known_hosts"
    if known_hosts.is_dir():
        for f in sorted(known_hosts.iterdir()):
            project, _, server_id = f.name.rpartition("-")
            if (project, server_id) not in active:
                plan.append({"kind": "local file", "id": str(f), "name": f.name,
                             "reason": "SSH host keys of a server that no longer exists"})
    projects = {p["name"] for p in db.list_projects(ctx.conn)}
    manifests = ctx.cfg.state_dir / "manifests"
    if manifests.is_dir():
        for f in sorted(manifests.glob("*.json")):
            if f.stem not in projects:
                plan.append({"kind": "local file", "id": str(f), "name": f.name,
                             "reason": "sync manifest of a removed project"})
    return plan


def run_cleanup(ctx, plan: list[dict]) -> list[dict]:
    results = []
    for item in plan:
        try:
            if item["kind"] == "server":
                ctx.hcloud.delete_server(item["id"])
            elif item["kind"] == "snapshot":
                ctx.hcloud.delete_image(item["id"])
            elif item["kind"] == "firewall":
                ctx.hcloud.delete_firewall(item["id"])
            elif item["kind"] == "ssh-key":
                ctx.hcloud.delete_ssh_key(item["id"])
            elif item["kind"] == "local file":
                Path(item["id"]).unlink(missing_ok=True)
            results.append({**item, "deleted": True})
        except DevboxError as e:
            results.append({**item, "deleted": False, "error": str(e)})
    return results


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
            "hetzner_id": s["hetzner_id"],
            "server_type": s["server_type"],
            "location": s["location"],
            "status": s["status"],
            "created_at": s["created_at"],
            "deleted_at": s["deleted_at"],
            "seconds": usage["seconds"],
            "billed_hours": usage["billed_hours"],
            "cost": usage["cost"],
            "restored_from_snapshot": restored["hetzner_image_id"] if restored else None,
            "activity": db.event_counts(conn, s["id"]),
        })
    snaps = []
    for s in sorted(snapshots.values(), key=lambda r: r["created_at"], reverse=True):
        item = costs.snapshot_item(s)
        snaps.append({
            "project": s["project"],
            "hetzner_image_id": s["hetzner_image_id"],
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
