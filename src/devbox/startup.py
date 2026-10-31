"""Bounded server startup: one deadline, progress lines and failure reporting for `up`."""

import re
import sys
import time
from typing import Callable, TextIO

from devbox import DevboxError

EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130

# Stages of `up`, in order. Used in progress, failures and the registry.
STAGES = ("prepare", "create", "power-on", "ssh", "cloud-init", "tools", "sync", "secrets")


class StageTimeout(DevboxError):
    def __init__(self, stage: str, message: str | None = None):
        super().__init__(message or f"startup time ran out during {stage}", code="startup_timeout", exit_code=EXIT_TIMEOUT)
        self.stage = stage


class StageFailed(DevboxError):
    def __init__(self, stage: str, message: str):
        super().__init__(message, code="startup_failed")
        self.stage = stage


class Deadline:
    """One monotonic deadline shared by every step of a startup."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit = seconds
        self.clock = clock
        self.start = clock()

    def elapsed(self) -> float:
        return self.clock() - self.start

    def remaining(self) -> float:
        return self.limit - self.elapsed()

    def check(self, stage: str) -> float:
        """Remaining seconds, or StageTimeout if there are none left."""
        left = self.remaining()
        if left <= 0:
            raise StageTimeout(stage)
        return left

    def bound(self, stage: str, cap: float) -> float:
        """A subprocess timeout: at most `cap`, never past the deadline."""
        return max(0.5, min(cap, self.check(stage)))


def clock_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def duration_label(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


class Progress:
    """Timestamped progress lines on stderr. Plain lines, no terminal tricks."""

    def __init__(self, deadline: Deadline, stream: TextIO | None = None, quiet: bool = False):
        self.deadline = deadline
        self.stream = stream or sys.stderr
        self.quiet = quiet
        self.stage = "prepare"
        self._last_wait_note = None
        self._last_wait_at = -1e9

    def step(self, stage: str, message: str) -> None:
        self.stage = stage
        self.say(message)

    def say(self, message: str) -> None:
        if not self.quiet:
            print(f"[{clock_label(self.deadline.elapsed())}] {message}", file=self.stream, flush=True)

    def waiting(self, message: str, every: float = 30.0) -> None:
        """Repeat a waiting message when it changes, or every `every` seconds."""
        now = self.deadline.elapsed()
        if message != self._last_wait_note or now - self._last_wait_at >= every:
            self._last_wait_note = message
            self._last_wait_at = now
            self.say(message)


SECRET_PATTERNS = [
    re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)(\S+)"),
    re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)"),
]


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Mask known secret values and common credential patterns."""
    for value in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(value, "***")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""), text)
    return text
