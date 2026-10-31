"""Cost estimates for devbox servers and snapshots.

Billing model (an estimate of how Hetzner bills):
- A server's lifetime is split into calendar months (UTC). In each month it
  costs `started hours × hourly price`, capped at the monthly price.
  Snapshots are counted the same way, with an hourly price of
  `size × price per GB per month / 730`.
- Prices include VAT. Primary IPv4 addresses and traffic are not included.

Allocation policy for reports:
- Each month's cost (and billed hours) is computed once, over the whole time
  the item ran in that month, and then spread evenly over that running time.
- A period (`--since/--until/--month/--days`) or a day/month row gets the share
  of each month's cost that falls inside it.
- So every breakdown adds up to its total, and adjacent periods add up to the
  combined period. Billed hours in a partial period can therefore be
  fractional.
"""

import math
import re
from datetime import date, datetime, timedelta, timezone

from devbox import DevboxError

CURRENCY = "EUR"
HOURS_PER_MONTH = 730
MIN_YEAR = 2000
MAX_YEAR = 9998
MAX_DAYS = 36500
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def server_item(row) -> dict:
    return {**dict(row), "kind": "server"}


def snapshot_item(row) -> dict:
    monthly = None
    if row["size_gb"] is not None and row["price_per_gb_month"] is not None:
        monthly = row["size_gb"] * row["price_per_gb_month"]
    return {
        "id": f"snapshot-{row['id']}",
        "kind": "snapshot",
        "project": row["project"],
        "hetzner_id": row["hetzner_image_id"],
        "server_type": "snapshot",
        "location": "-",
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
        "price_hourly": monthly / HOURS_PER_MONTH if monthly is not None else None,
        "price_monthly": monthly,
    }


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(dt: datetime) -> datetime:
    start = month_start(dt)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def billed_hours(start: datetime, end: datetime) -> int:
    """Started hours between start and end."""
    if end <= start:
        return 0
    return math.ceil((end - start).total_seconds() / 3600)


def month_segments(
    start: datetime, end: datetime, price_hourly: float | None, price_monthly: float | None
) -> list[tuple[datetime, datetime, int, float | None]]:
    """(segment start, segment end, billed hours, cost) for each calendar month of [start, end)."""
    segments = []
    cursor = start
    while cursor < end:
        seg_end = min(end, next_month(cursor))
        hours = billed_hours(cursor, seg_end)
        cost = None
        if price_hourly is not None:
            cost = hours * price_hourly
            if price_monthly is not None:
                cost = min(cost, price_monthly)
        segments.append((cursor, seg_end, hours, cost))
        cursor = seg_end
    return segments


def cost_between(
    start: datetime, end: datetime, price_hourly: float | None, price_monthly: float | None
) -> float | None:
    """Billed cost for running from `start` to `end`, capped per calendar month."""
    if price_hourly is None:
        return None
    return sum(cost for _, _, _, cost in month_segments(start, end, price_hourly, price_monthly))


def lifetime(item, now: datetime) -> tuple[datetime, datetime]:
    start = parse_ts(item["created_at"])
    end = parse_ts(item["deleted_at"]) if item["deleted_at"] else now
    return start, max(start, end)


def server_window(item, since: datetime | None, until: datetime | None, now: datetime) -> tuple[datetime, datetime] | None:
    """The part of an item's lifetime inside [since, until), or None."""
    start, end = lifetime(item, now)
    if since and since > start:
        start = since
    if until and until < end:
        end = until
    return (start, end) if end > start else None


def server_usage(item, since=None, until=None, now: datetime | None = None) -> dict:
    """Running time, billed hours and cost of an item allocated to [since, until).

    Without since/until this is the item's whole billed cost, with whole
    billed hours.
    """
    now = now or datetime.now(timezone.utc)
    start, end = lifetime(item, now)
    seconds = 0.0
    hours = 0.0
    cost = 0.0 if item["price_hourly"] is not None else None
    for seg_start, seg_end, seg_hours, seg_cost in month_segments(
        start, end, item["price_hourly"], item["price_monthly"]
    ):
        lo = max(seg_start, since) if since else seg_start
        hi = min(seg_end, until) if until else seg_end
        if hi <= lo:
            continue
        overlap = (hi - lo).total_seconds()
        share = overlap / (seg_end - seg_start).total_seconds()
        seconds += overlap
        hours += seg_hours * share
        if cost is not None:
            cost += seg_cost * share
    whole = since is None and until is None
    return {
        "seconds": int(round(seconds)),
        "billed_hours": int(round(hours)) if whole else hours,
        "cost": cost,
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


def summarize(items, since=None, until=None, by: str = "project", now: datetime | None = None) -> dict:
    """Total cost and a breakdown for items that ran inside [since, until)."""
    if by not in GROUPINGS:
        raise ValueError(by)
    now = now or datetime.now(timezone.utc)
    groups: dict[str, dict] = {}
    total = 0.0
    snapshot_total = 0.0
    total_seconds = 0
    total_hours = 0.0
    unknown = 0
    counted = {"server": set(), "snapshot": set()}

    def add(key: str, kind: str, usage: dict) -> None:
        g = groups.setdefault(key, {
            "key": key, "cost": 0.0, "seconds": 0, "billed_hours": 0.0,
            "servers": 0, "snapshots": 0, "snapshot_cost": 0.0, "unknown_price": 0,
        })
        g["servers" if kind == "server" else "snapshots"] += 1
        if kind == "server":
            g["seconds"] += usage["seconds"]
            g["billed_hours"] += usage["billed_hours"]
        if usage["cost"] is None:
            g["unknown_price"] += 1
        else:
            g["cost"] += usage["cost"]
            if kind == "snapshot":
                g["snapshot_cost"] += usage["cost"]

    for item in items:
        kind = item.get("kind", "server") if isinstance(item, dict) else "server"
        window = server_window(item, since, until, now)
        if window is None:
            continue
        usage = server_usage(item, since, until, now)
        if kind == "server":
            total_seconds += usage["seconds"]
            total_hours += usage["billed_hours"]
        if usage["cost"] is None:
            unknown += 1
        else:
            total += usage["cost"]
            if kind == "snapshot":
                snapshot_total += usage["cost"]
        counted[kind].add(item["id"])

        if by in ("month", "day"):
            start, end = window
            for key, b_start, b_end in buckets(start, end, by):
                add(key, kind, server_usage(item, max(b_start, start), min(b_end, end), now))
        elif by == "project":
            add(item["project"], kind, usage)
        elif by == "type":
            add("snapshot" if kind == "snapshot" else f"{item['server_type']} {item['location']}", kind, usage)
        else:
            add(f"{item['project']}#{'snapshot-' if kind == 'snapshot' else ''}{item['hetzner_id']}", kind, usage)

    return {
        "currency": CURRENCY,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "by": by,
        "total": round(total, 6),
        "snapshot_total": round(snapshot_total, 6),
        "seconds": total_seconds,
        "billed_hours": round(total_hours, 2),
        "servers": len(counted["server"]),
        "snapshots": len(counted["snapshot"]),
        "unknown_price": unknown,
        "groups": [
            {
                **g,
                "cost": round(g["cost"], 6),
                "snapshot_cost": round(g["snapshot_cost"], 6),
                "billed_hours": round(g["billed_hours"], 2),
            }
            for g in sorted(groups.values(), key=lambda g: g["key"])
        ],
    }


# Parsing report periods. All errors are DevboxError so `--json` stays valid.


def _check_year(year: int, value: str) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise DevboxError(f"'{value}' is out of range: use a year between {MIN_YEAR} and {MAX_YEAR}")


def parse_day(value: str) -> datetime:
    if not DAY_RE.match(value):
        raise DevboxError(f"invalid date '{value}': use YYYY-MM-DD")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise DevboxError(f"invalid date '{value}': that day does not exist")
    _check_year(day.year, value)
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


def parse_month(value: str) -> tuple[datetime, datetime]:
    if not MONTH_RE.match(value):
        raise DevboxError(f"invalid month '{value}': use YYYY-MM, e.g. 2026-10")
    try:
        start = datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise DevboxError(f"invalid month '{value}': use YYYY-MM, e.g. 2026-10")
    _check_year(start.year, value)
    return start, next_month(start)


def parse_days(value: str) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise DevboxError(f"invalid --days '{value}': use a whole number of days")
    if not 1 <= days <= MAX_DAYS:
        raise DevboxError(f"--days must be between 1 and {MAX_DAYS}")
    return days


def resolve_period(
    month: str | None, days: str | None, since: str | None, until: str | None, now: datetime
) -> tuple[datetime | None, datetime | None]:
    """[since, until) for a report; None means unbounded on that side."""
    start = end = None
    if month:
        start, end = parse_month(month)
    if days is not None:
        start = now - timedelta(days=parse_days(days))
    if since:
        start = parse_day(since)
    if until:
        end = parse_day(until)
    if start and end and end <= start:
        raise DevboxError(
            f"the period is empty or reversed: until ({end.date()}) must be after since ({start.date()})"
        )
    return start, end


def money(value: float | None) -> str:
    return "€?" if value is None else f"€{value:.2f}"


def hours(value: float) -> str:
    return f"{value:g}" if float(value).is_integer() else f"{value:.2f}"


def duration(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"
