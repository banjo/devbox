"""Billing-hour aware idle shutdown (`idle_timeout = "hour-end"` and `--at-hour-end`).

Hetzner bills every started hour from the moment a server is created, until it
is deleted. Once an hour has started it is paid, so an idle server can stay
until shortly before the next hour starts at no extra cost.

A stop is planned in a window before each billing boundary:

    boundary - interval - action time - margin  ...  boundary - action time - margin

The window is one check interval long, so exactly one scheduled check falls
inside it, and it ends when the action (snapshot + delete for pause) would no
longer finish, with the margin to spare, before the boundary. A server that misses its window (busy, or no check ran)
is not stopped in the new hour, which is already paid; it gets the window
before the next boundary instead.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

HOUR = timedelta(hours=1)
HOUR_END = "hour-end"
TIMES_FILE = "action-times.json"
# Used until devbox has timed a real pause or down on this machine.
DEFAULT_ACTION_SECONDS = {"pause": 300, "down": 60}
KEEP_TIMES = 10
RECENT_TIMES = 5


def next_boundary(created: datetime, now: datetime) -> datetime:
    """The start of the next billing hour: the end of the hour that `now` is in."""
    if now < created:
        return created + HOUR
    return created + HOUR * (int((now - created) / HOUR) + 1)


def window(boundary: datetime, interval: timedelta, action_time: timedelta,
           margin: timedelta) -> tuple[datetime, datetime]:
    end = boundary - action_time - margin
    return end - interval, end


def planned_stop(created: datetime, ready: datetime, now: datetime, interval: timedelta,
                 action_time: timedelta, margin: timedelta) -> datetime:
    """When the server should be stopped: the first moment, not before `ready` or `now`,
    that falls inside a stop window."""
    t = max(ready, now)
    boundary = next_boundary(created, t)
    start, end = window(boundary, interval, action_time, margin)
    if t < start:
        return start
    if t <= end:
        return t
    return window(boundary + HOUR, interval, action_time, margin)[0]


def missed_boundary(created: datetime, ready: datetime, now: datetime,
                    action_time: timedelta, margin: timedelta) -> datetime | None:
    """The boundary that just passed while the server could have been stopped before it."""
    boundary = next_boundary(created, now) - HOUR
    if boundary <= created:
        return None
    return boundary if ready <= boundary - action_time - margin else None


# How long pause and down take on this machine


def _read(state_dir: Path) -> dict:
    try:
        data = json.loads((state_dir / TIMES_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_action_time(state_dir: Path, action: str, seconds: float) -> None:
    data = _read(state_dir)
    times = [t for t in data.get(action, []) if isinstance(t, (int, float))]
    data[action] = (times + [round(seconds, 1)])[-KEEP_TIMES:]
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (TIMES_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, state_dir / TIMES_FILE)


def action_time(state_dir: Path, action: str) -> timedelta:
    """The longest of the recent runs of `action`, or a default before there are any."""
    times = [t for t in _read(state_dir).get(action, []) if isinstance(t, (int, float))]
    seconds = max(times[-RECENT_TIMES:]) if times else DEFAULT_ACTION_SECONDS[action]
    return timedelta(seconds=seconds)
