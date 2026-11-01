"""Managed scheduling of `devbox idle-check` (`devbox autostop`).

`enable` adds one cron entry per config/state home. A marker comment above
the entry names the homes it covers, so devbox only ever finds, replaces or
removes its own lines. The entry sets those homes and PATH explicitly
because cron starts jobs with an almost empty environment; the Hetzner
token must therefore come from `hcloud_token_file`.

`idle-check` records each run in the state folder, which is how `status`
tells a working schedule from a stale or failing one.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devbox import DevboxError, admin, config, costs

INTERVAL_MINUTES = 15
SCHEDULE = f"*/{INTERVAL_MINUTES} * * * *"
MARKER = "# devbox-autostop"
# A check is overdue when two scheduled runs in a row are missing.
STALE_AFTER = timedelta(minutes=2 * INTERVAL_MINUTES + 5)
RUNS_FILE = "idle-check.json"
INSTALL_FILE = "autostop.json"
LOG_FILE = "idle-check.log"
BASE_PATH = ["/usr/local/bin", "/usr/bin", "/bin"]
TOOLS = ("hcloud", "ssh", "ssh-keygen", "rsync", "git")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# Crontab


def read_crontab() -> str | None:
    """The user's crontab; "" when there is none, None when it can't be read."""
    proc = admin._run(["crontab", "-l"])
    if proc is None:
        return None
    if proc.returncode == 0:
        return proc.stdout
    if "no crontab" in (proc.stderr or "").lower():
        return ""
    return None


def write_crontab(text: str) -> None:
    try:
        proc = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DevboxError(f"cannot write the crontab: {e}")
    if proc.returncode != 0:
        raise DevboxError(f"cannot write the crontab: {proc.stderr.strip() or proc.stdout.strip()}")


def parse_entries(crontab: str) -> list[dict]:
    """Entries added by `autostop enable`: marker line + the cron line below it."""
    lines = crontab.splitlines()
    entries = []
    for i, line in enumerate(lines):
        if not line.startswith(MARKER + " "):
            continue
        try:
            fields = dict(part.split("=", 1) for part in shlex.split(line[len(MARKER):]) if "=" in part)
        except ValueError:
            continue
        if "config" not in fields or "state" not in fields:
            continue
        cron_line = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].lstrip().startswith("#") else None
        entries.append({
            "config_dir": fields["config"], "state_dir": fields["state"],
            "marker": line, "line": cron_line,
        })
    return entries


def without_entry(crontab: str, config_dir: str, state_dir: str) -> str:
    """The crontab minus our marker and cron line for these homes; everything else untouched."""
    lines = crontab.splitlines()
    mine = {(e["marker"], e["line"]) for e in parse_entries(crontab)
            if e["config_dir"] == config_dir and e["state_dir"] == state_dir}
    drop: set[int] = set()
    for i, line in enumerate(lines):
        for marker, cron_line in mine:
            if line == marker:
                drop.add(i)
                if cron_line is not None and i + 1 < len(lines) and lines[i + 1] == cron_line:
                    drop.add(i + 1)
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return "\n".join(kept) + ("\n" if kept else "")


def unmanaged(crontab: str | None) -> str:
    """The crontab without the entries `autostop enable` added."""
    if not crontab:
        return ""
    managed = {e["marker"] for e in parse_entries(crontab)} | {e["line"] for e in parse_entries(crontab)}
    return "\n".join(line for line in crontab.splitlines() if line not in managed)


def manual_schedule(crontab: str | None) -> bool:
    """An idle-check cron entry or systemd timer not added by `autostop enable`."""
    return admin.idle_scheduler(unmanaged(crontab))["status"] == admin.OK


# This config


def homes() -> tuple[str, str]:
    return str(config.config_dir()), str(config.state_dir())


def cron_path() -> str:
    dirs = []
    for tool in TOOLS:
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    seen: list[str] = []
    for d in dirs + BASE_PATH:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def job_command(extra: tuple[str, ...] = ()) -> str:
    """The shell command cron runs (without output redirection)."""
    env = {
        "XDG_CONFIG_HOME": str(config.config_dir().parent),
        "XDG_STATE_HOME": str(config.state_dir().parent),
        "PATH": cron_path(),
    }
    assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"{assignments} {shlex.join([sys.executable, '-m', 'devbox', 'idle-check', *extra])}"


def build_entry() -> tuple[str, str]:
    config_dir, state_dir = homes()
    log = Path(state_dir) / LOG_FILE
    marker = f"{MARKER} config={shlex.quote(config_dir)} state={shlex.quote(state_dir)}"
    line = f"{SCHEDULE} {job_command()} >> {shlex.quote(str(log))} 2>&1"
    # cron turns % into a newline; refuse instead of installing a broken entry.
    if "%" in line or "\n" in line or "%" in marker:
        raise DevboxError("a path used by the cron entry contains '%' or a newline, which cron can't run")
    return marker, line


def controller_of(config_dir: str, state_dir: str) -> str | None:
    """The controller id a config/state home uses, without creating one."""
    try:
        cfg = config.load(Path(config_dir) / "config.toml")
    except DevboxError:
        return None
    if cfg.controller_id:
        return cfg.controller_id
    try:
        return (Path(state_dir) / "controller-id").read_text().strip() or None
    except OSError:
        return None


def credentials(cfg) -> dict:
    """Whether a job without the shell's environment can get a Hetzner token."""
    if cfg.hcloud_token_file is None:
        return admin.check(
            "credentials", admin.FAIL,
            "no hcloud_token_file in the config; scheduled jobs don't see HCLOUD_TOKEN from your shell",
            f"put the token alone in a file (chmod 600) and set hcloud_token_file in {config.config_dir() / 'config.toml'}",
        )
    try:
        token = cfg.hcloud_token()
    except DevboxError as e:
        return admin.check("credentials", admin.FAIL, str(e), "check hcloud_token_file in the config")
    if not token:
        return admin.check("credentials", admin.FAIL, f"{cfg.hcloud_token_file} is empty",
                           "write the Hetzner API token to that file")
    if cfg.hcloud_token_file.stat().st_mode & 0o077:
        return admin.check("credentials", admin.WARN, f"{cfg.hcloud_token_file} is readable by other users",
                           f"chmod 600 {cfg.hcloud_token_file}")
    return admin.check("credentials", admin.OK, f"token file {cfg.hcloud_token_file}")


# Run records


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def record_run(state_dir: Path, error: str | None, now: datetime | None = None) -> None:
    now = now or now_utc()
    runs = read_json(state_dir / RUNS_FILE)
    runs["last_run"] = iso(now)
    runs["last_result"] = "ok" if error is None else "failed"
    if error is None:
        runs["last_success"] = iso(now)
    else:
        runs["last_failure"] = iso(now)
        runs["last_error"] = error
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (RUNS_FILE + ".tmp")
    tmp.write_text(json.dumps(runs, indent=2) + "\n")
    os.replace(tmp, state_dir / RUNS_FILE)


def last_failed(runs: dict) -> bool:
    return runs.get("last_result") == "failed"


def ago(dt: datetime, now: datetime) -> str:
    return f"{dt.astimezone().strftime('%Y-%m-%d %H:%M')} ({costs.duration((now - dt).total_seconds())} ago)"


# Health


def enforcement(cfg, now: datetime | None = None) -> dict:
    """Is idle shutdown actually enforced for this config? (check dict, as in doctor)"""
    now = now or now_utc()
    crontab = read_crontab()
    config_dir, state_dir = homes()
    runs = read_json(Path(state_dir) / RUNS_FILE)
    entry = next((e for e in parse_entries(crontab or "")
                  if e["config_dir"] == config_dir and e["state_dir"] == state_dir and e["line"]), None)

    if entry is None:
        manual = admin.idle_scheduler(unmanaged(crontab))
        if manual["status"] != admin.OK:
            return manual
        if last_failed(runs):
            return admin.check(
                "idle shutdown", admin.WARN,
                f"{manual['detail']}, but the last idle-check failed at "
                f"{ago(parse_iso(runs['last_failure']), now)}: {runs.get('last_error')}",
                f"see {Path(state_dir) / LOG_FILE} or run `devbox idle-check --dry-run`",
            )
        return manual

    creds = credentials(cfg)
    if creds["status"] == admin.FAIL:
        return admin.check("idle shutdown", admin.FAIL, f"scheduled, but {creds['detail']}", creds["fix"])
    if last_failed(runs):
        return admin.check(
            "idle shutdown", admin.FAIL,
            f"scheduled, but the last check failed at {ago(parse_iso(runs['last_failure']), now)}: "
            f"{runs.get('last_error')}",
            f"see {Path(state_dir) / LOG_FILE}",
        )
    success = parse_iso(runs.get("last_success"))
    if success and now - success <= STALE_AFTER:
        return admin.check("idle shutdown", admin.OK, f"scheduled (autostop), last check {ago(success, now)}")
    installed = parse_iso(read_json(Path(state_dir) / INSTALL_FILE).get("installed_at"))
    if success is None and installed and now - installed <= STALE_AFTER:
        return admin.check("idle shutdown", admin.OK, f"scheduled (autostop) {ago(installed, now)}, first check pending")
    since = f"since {ago(success, now)}" if success else "yet"
    return admin.check(
        "idle shutdown", admin.FAIL,
        f"scheduled, but no successful check {since}; cron may not be running",
        f"check the cron service and {Path(state_dir) / LOG_FILE}",
    )


def next_check(after: datetime) -> datetime:
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while t.minute % INTERVAL_MINUTES:
        t += timedelta(minutes=1)
    return t


def coverage(crontab: str | None) -> list[dict]:
    config_dir, state_dir = homes()
    return [
        {"config_dir": e["config_dir"], "state_dir": e["state_dir"],
         "controller": controller_of(e["config_dir"], e["state_dir"]),
         "this_config": e["config_dir"] == config_dir and e["state_dir"] == state_dir}
        for e in parse_entries(crontab or "") if e["line"]
    ]


# Enable / disable


def verify(timeout: float = 120) -> dict:
    """Run the scheduled command once (dry run) with an environment like cron's."""
    env = {"HOME": str(Path.home()), "LOGNAME": os.environ.get("LOGNAME", ""), "USER": os.environ.get("USER", ""),
           "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"}
    try:
        proc = subprocess.run(["/bin/sh", "-c", job_command(("--dry-run", "--json"))], env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the test run did not finish within {int(timeout)}s"}
    if proc.returncode == 0:
        return {"ok": True}
    out = proc.stdout.strip()
    try:
        error = json.loads(out)["error"]
    except (ValueError, KeyError, TypeError):
        error = (proc.stderr.strip() or out or f"exit {proc.returncode}").splitlines()[-1]
    return {"ok": False, "error": error}


def enable(cfg, yes: bool) -> dict:
    if shutil.which("hcloud") is None:
        raise DevboxError("hcloud CLI not found; install it before scheduling idle-check")
    creds = credentials(cfg)
    if creds["status"] == admin.FAIL:
        raise DevboxError(f"{creds['detail']}; {creds['fix']}", code="no_token_file")
    crontab = read_crontab()
    if crontab is None:
        raise DevboxError("cannot read the crontab; is cron installed (`crontab -l`)?")

    config_dir, state_dir = homes()
    marker, line = build_entry()
    existing = next((e for e in parse_entries(crontab)
                     if e["config_dir"] == config_dir and e["state_dir"] == state_dir), None)
    warnings = []
    if creds["status"] == admin.WARN:
        warnings.append(f"{creds['detail']}: {creds['fix']}")
    if cfg.idle_action == "down":
        warnings.append("idle_action is 'down': idle servers are deleted with their data, not paused. "
                        "Set idle_action = \"pause\" in the config to keep a snapshot instead")
    if config.parse_duration(cfg.idle_timeout) is None:
        warnings.append("idle_timeout is off; only projects with their own --idle-timeout are checked")
    if manual_schedule(crontab):
        warnings.append("another idle-check schedule (not managed by autostop) is installed; runs never "
                        "overlap, but you can remove it once this one works")

    plan = {
        "config_dir": config_dir, "state_dir": state_dir, "controller": cfg.get_controller_id(),
        "schedule": SCHEDULE, "marker": marker, "entry": line, "log": str(Path(state_dir) / LOG_FILE),
        "idle_timeout": cfg.idle_timeout, "idle_action": cfg.idle_action,
        "already_enabled": bool(existing and existing["line"] == line),
        "replaces": existing["line"] if existing and existing["line"] != line else None,
        "warnings": warnings, "verified": None, "installed": False,
    }
    if not yes:
        return plan

    check = verify()
    plan["verified"] = check
    if not check["ok"]:
        raise DevboxError(
            f"the scheduled command failed in a cron-like environment, so nothing was installed: {check['error']}",
            code="verify_failed", details={"entry": line},
        )
    if not plan["already_enabled"]:
        base = without_entry(crontab, config_dir, state_dir)
        if base and not base.endswith("\n"):
            base += "\n"
        write_crontab(f"{base}{marker}\n{line}\n")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir) / INSTALL_FILE).write_text(
        json.dumps({"installed_at": iso(now_utc()), "entry": line}, indent=2) + "\n"
    )
    plan["installed"] = True
    return plan


def disable(cfg, yes: bool) -> dict:
    crontab = read_crontab()
    if crontab is None:
        raise DevboxError("cannot read the crontab; is cron installed (`crontab -l`)?")
    config_dir, state_dir = homes()
    existing = next((e for e in parse_entries(crontab)
                     if e["config_dir"] == config_dir and e["state_dir"] == state_dir), None)
    result = {"config_dir": config_dir, "state_dir": state_dir,
              "entry": existing["line"] if existing else None, "removed": False,
              "manual_schedule": manual_schedule(crontab)}
    if existing is None or not yes:
        return result
    write_crontab(without_entry(crontab, config_dir, state_dir))
    (Path(state_dir) / INSTALL_FILE).unlink(missing_ok=True)
    result["removed"] = True
    return result
