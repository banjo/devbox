"""Polling file watcher for `devbox sync --watch` (standard library only)."""

import os
import time
from pathlib import Path
from typing import Callable

from devbox.remote import git_files

Snapshot = dict[str, tuple[int, int]]


def snapshot(root: Path) -> Snapshot:
    """(mtime, size) of every file sync would send."""
    files = git_files(root)
    if files is None:
        files = [
            str(Path(dirpath, name).relative_to(root))
            for dirpath, dirnames, filenames in os.walk(root)
            if ".git" not in Path(dirpath).relative_to(root).parts
            for name in filenames
        ]
    result = {}
    for name in files:
        try:
            st = os.stat(root / name)
        except OSError:
            continue
        result[name] = (st.st_mtime_ns, st.st_size)
    return result


def changed(before: Snapshot, after: Snapshot) -> list[str]:
    names = set(before) | set(after)
    return sorted(n for n in names if before.get(n) != after.get(n))


def watch(
    take: Callable[[], Snapshot],
    on_change: Callable[[list[str]], None],
    interval: float = 1.0,
    settle: float = 0.3,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> None:
    """Call `on_change(paths)` whenever the snapshot changes and then stays put for `settle` seconds."""
    last = take()
    n = 0
    while rounds is None or n < rounds:
        n += 1
        sleep(interval)
        current = take()
        if current == last:
            continue
        # Wait until a burst of saves (formatters, git checkouts) is over.
        while True:
            sleep(settle)
            again = take()
            if again == current:
                break
            current = again
        on_change(changed(last, current))
        last = current
