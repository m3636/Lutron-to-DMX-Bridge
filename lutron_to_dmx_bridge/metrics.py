"""Small counters behind the GUI's status area: rates, latencies, warnings.

Everything here is cheap, thread-safe enough for asyncio use, and has no
dependency on the rest of the app. Time is passed in as an argument so tests
can drive it without sleeping.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass

#: Seconds over which latency samples are summarised.
LATENCY_WINDOW = 60.0
#: Seconds of latency samples kept for the Status tab graph.
LATENCY_HISTORY = 120.0
#: How many warning/error lines are kept for the Status tab.
LOG_KEEP = 20

#: A latency sample older than this no longer says how busy the system is right now.
LOAD_STALE_S = 2.5

STARTED_AT = time.time()


def uptime_s() -> float:
    return max(0.0, time.time() - STARTED_AT)


@dataclass
class LinkLoad:
    """How far behind the Lutron system is, as far as the driver can tell (see ``dmx/protect.py``)."""

    #: round trip of the latest probe, or how long an unanswered probe has waited (None = unknown)
    latency_ms: float | None = None
    #: level commands written that the system has not echoed yet
    inflight: int = 0


class RateWindow:
    """Events per second over a sliding window, plus a running total."""

    def __init__(self, window: float = 1.0) -> None:
        self.window = window
        self.total = 0
        self._times: deque[float] = deque()

    def add(self, n: int = 1, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.total += n
        self._times.extend([now] * n)
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._times and now - self._times[0] > self.window:
            self._times.popleft()

    def rate(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._trim(now)
        return round(len(self._times) / self.window, 1)


class LatencyStats:
    """Recent latency samples in milliseconds: last, median and 95th percentile."""

    def __init__(self, window: float = LATENCY_WINDOW, keep: int = 400, history: float = LATENCY_HISTORY) -> None:
        self.window = window
        self.history_span = history
        self._samples: deque[tuple[float, float]] = deque(maxlen=keep)
        self.last: float | None = None
        self._last_at: float | None = None

    def add(self, ms: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last = ms
        self._last_at = now
        self._samples.append((now, ms))

    def fresh_last(self, max_age: float = LOAD_STALE_S, now: float | None = None) -> float | None:
        """The latest sample in ms, or None when it is older than `max_age` seconds."""
        now = time.monotonic() if now is None else now
        if self.last is None or self._last_at is None or now - self._last_at > max_age:
            return None
        return self.last

    def snapshot(self, now: float | None = None) -> dict:
        """`age_s` is how long ago the last sample arrived (None before the first).

        `history` is the samples of the last `history_span` seconds as `[age_s, ms]`
        pairs, oldest first, for the Status tab graph.
        """
        now = time.monotonic() if now is None else now
        last = None if self.last is None else round(self.last)
        age = None if self._last_at is None else round(max(0.0, now - self._last_at), 1)
        history = [[round(max(0.0, now - t), 1), round(ms, 1)]
                   for t, ms in self._samples if now - t <= self.history_span]
        vals = sorted(ms for t, ms in self._samples if now - t <= self.window)
        if not vals:
            return {"last": last, "age_s": age, "median": None, "p95": None, "count": 0, "history": history}
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        return {"last": last, "age_s": age, "median": round(statistics.median(vals)), "p95": round(p95),
                "count": len(vals), "history": history}


class DriverMetrics:
    """What a driver can tell the GUI about its link to the Lutron system."""

    def __init__(self) -> None:
        self.commands = RateWindow()
        self.latency = LatencyStats()  # round trip to the Lutron system, one sample per probe
        self.connected_since: float | None = None  # epoch seconds
        self.reconnect_attempts = 0  # failures in the current outage
        self.reconnects = 0  # outages recovered since start
        self.retry_at: float | None = None  # epoch seconds of the next attempt

    def mark_connected(self) -> None:
        if self.reconnect_attempts:
            self.reconnects += 1
        self.connected_since = time.time()
        self.reconnect_attempts = 0
        self.retry_at = None

    def mark_disconnected(self) -> None:
        self.connected_since = None

    def mark_retry(self, delay: float) -> None:
        self.reconnect_attempts += 1
        self.retry_at = time.time() + delay

    def snapshot(self, inflight: int = 0, now: float | None = None) -> dict:
        wall = time.time()
        return {
            "connected_for_s": None if self.connected_since is None else round(wall - self.connected_since),
            "reconnect_attempts": self.reconnect_attempts,
            "reconnects": self.reconnects,
            "retry_in_s": None if self.retry_at is None else max(0, round(self.retry_at - wall)),
            "commands_per_s": self.commands.rate(now),
            "commands_total": self.commands.total,
            "latency": self.latency.snapshot(now),
            "inflight": inflight,
        }


@dataclass
class LogLine:
    time: float
    level: str  # "W" or "E"
    logger: str
    message: str

    def to_dict(self) -> dict:
        return {"time": self.time, "level": self.level, "logger": self.logger, "message": self.message}


class LogCounter(logging.Handler):
    """Counts warnings and errors and remembers the latest few.

    Counts can be cleared from the GUI, so they mean "since you last looked".
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0
        self.lines: deque[LogLine] = deque(maxlen=LOG_KEEP)
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string must not break logging
            msg = str(record.msg)
        is_error = record.levelno >= logging.ERROR
        with self._guard:
            if is_error:
                self.errors += 1
            else:
                self.warnings += 1
            self.lines.append(LogLine(record.created, "E" if is_error else "W", record.name, msg))

    def clear(self) -> None:
        with self._guard:
            self.warnings = self.errors = 0
            self.lines.clear()

    def snapshot(self) -> dict:
        with self._guard:
            return {"warnings": self.warnings, "errors": self.errors,
                    "recent": [ln.to_dict() for ln in reversed(self.lines)]}


#: One counter for the process, attached to the root logger by ``install_log_counter``.
LOG_COUNTER = LogCounter()


def install_log_counter() -> LogCounter:
    root = logging.getLogger()
    if LOG_COUNTER not in root.handlers:
        root.addHandler(LOG_COUNTER)
    return LOG_COUNTER
