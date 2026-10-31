"""Cost estimates for devbox servers.

Hetzner bills cloud servers per started hour, capped at the monthly price per
calendar month. Prices are the gross prices from `hcloud server-type list` at
the time the server was created. Only the server itself is counted: primary
IPv4 addresses, traffic overage and snapshots are not included.
"""

import math
from datetime import date, datetime, timedelta, timezone

CURRENCY = "EUR"


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(dt: datetime) -> datetime:
    start = month_start(dt)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def billed_hours(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    return math.ceil((end - start).total_seconds() / 3600)


def cost_between(
    start: datetime, end: datetime, price_hourly: float | None, price_monthly: float | None
) -> float | None:
    """Estimated cost for running from `start` to `end`, split by calendar month."""
    if price_hourly is None:
        return None
    total = 0.0
    cursor = start
    while cursor < end:
        segment_end = min(end, next_month(cursor))
        cost = billed_hours(cursor, segment_end) * price_hourly
        if price_monthly is not None:
            cost = min(cost, price_monthly)
        total += cost
        cursor = segment_end
    return total


def server_window(server, since: datetime | None, until: datetime | None, now: datetime) -> tuple[datetime, datetime] | None:
    """The part of a server's lifetime inside [since, until), or None."""
    start = parse_ts(server["created_at"])
    end = parse_ts(server["deleted_at"]) if server["deleted_at"] else now
    if since and since > start:
        start = since
    if until and until < end:
        end = until
    return (start, end) if end > start else None


def server_usage(server, since=None, until=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    window = server_window(server, since, until, now)
    if window is None:
        return {"seconds": 0, "billed_hours": 0, "cost": 0.0 if server["price_hourly"] is not None else None}
    start, end = window
    return {
        "seconds": int((end - start).total_seconds()),
        "billed_hours": billed_hours(start, end),
        "cost": cost_between(start, end, server["price_hourly"], server["price_monthly"]),
    }


def buckets(start: datetime, end: datetime, by: str):
    """Yield (key, bucket_start, bucket_end) covering [start, end) for day/month grouping."""
    if by == "month":
        cursor = month_start(start)
        while cursor < end:
            nxt = next_month(cursor)
            yield cursor.strftime("%Y-%m"), cursor, nxt
            cursor = nxt
    elif by == "day":
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor < end:
            nxt = cursor + timedelta(days=1)
            yield cursor.strftime("%Y-%m-%d"), cursor, nxt
            cursor = nxt
    else:
        raise ValueError(by)


GROUPINGS = ("project", "month", "day", "type", "server")


def summarize(servers, since=None, until=None, by: str = "project", now: datetime | None = None) -> dict:
    """Total cost and a breakdown for servers that ran inside [since, until)."""
    now = now or datetime.now(timezone.utc)
    groups: dict[str, dict] = {}
    total = 0.0
    unknown = 0
    total_seconds = 0
    counted = set()

    def add(key: str, usage: dict) -> None:
        g = groups.setdefault(key, {"key": key, "cost": 0.0, "seconds": 0, "billed_hours": 0, "servers": 0, "unknown_price": 0})
        g["seconds"] += usage["seconds"]
        g["billed_hours"] += usage["billed_hours"]
        g["servers"] += 1
        if usage["cost"] is None:
            g["unknown_price"] += 1
        else:
            g["cost"] += usage["cost"]

    for s in servers:
        window = server_window(s, since, until, now)
        if window is None:
            continue
        usage = server_usage(s, since, until, now)
        total_seconds += usage["seconds"]
        if usage["cost"] is None:
            unknown += 1
        else:
            total += usage["cost"]
        counted.add(s["id"])

        if by in ("month", "day"):
            start, end = window
            for key, b_start, b_end in buckets(start, end, by):
                add(key, server_usage(s, max(b_start, start), min(b_end, end), now))
        elif by == "project":
            add(s["project"], usage)
        elif by == "type":
            add(f"{s['server_type']} {s['location']}", usage)
        elif by == "server":
            add(f"{s['project']}#{s['hetzner_id']}", usage)
        else:
            raise ValueError(by)

    return {
        "currency": CURRENCY,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "by": by,
        "total": round(total, 4),
        "seconds": total_seconds,
        "servers": len(counted),
        "unknown_price": unknown,
        "groups": [
            {**g, "cost": round(g["cost"], 4)} for g in sorted(groups.values(), key=lambda g: g["key"])
        ],
    }


def parse_day(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=timezone.utc)


def parse_month(value: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
    return start, next_month(start)


def money(value: float | None) -> str:
    return "€?" if value is None else f"€{value:.2f}"


def duration(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"
