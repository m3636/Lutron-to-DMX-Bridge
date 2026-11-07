"""Keeps a DMX source from burying the Lutron repeater in commands.

Why this exists: a RadioRA 2 Main Repeater executes telnet commands one after
another and holds the ones it has accepted but not yet carried out. If we send
faster than it can work, the backlog lives *inside the repeater*: closing this
app does not recall it, and the lights keep moving for as long as the backlog
takes to drain. A fixed commands-per-second limit is not enough, because nobody
knows what number the repeater can actually take (it differs between systems
and between moments: radio traffic, other keypads, a second repeater).

So the engine uses feedback, the way TCP does:

* **Latency** is the signal. The RA2 driver times a ``?SYSTEM,1`` query every
  half second while commands are flowing. The repeater answers in order, so the
  round trip includes the work queued ahead of it. A probe that has not been
  answered yet counts for as long as it has waited, so the signal keeps rising
  when the repeater stalls instead of going quiet.
* **AIMD rate control.** The allowed commands per second grows by a fixed
  amount per second while the repeater is quick (additive increase) and is
  halved when it is slow (multiplicative decrease), never more than once per
  queue-drain time so the lag in the signal does not make it overshoot.
* **A window** (``max_inflight``) caps the commands written but not yet echoed
  back. It bounds the backlog we can create between two latency samples, which
  the rate limit alone cannot (a whole-house cue is one burst).
* **A gate** closes when the repeater is seconds behind: nothing is sent until
  it catches up. Targets keep updating inside the app (the engine holds only the
  latest value per light, so nothing piles up here) and go out afterwards.
* **Storm detection** pauses DMX output, and raises an alert, when the source is
  flooding (frames per second far beyond DMX512's 44), when it makes lights
  reverse direction faster than any real show does (noise, a feedback loop), or
  when the repeater has not recovered although we slowed to the minimum rate.
  A pause that was tripped automatically stays until someone resumes it.
* **A manual pause** does the same on demand.

Everything here takes the time as an argument so tests can run it in virtual
time. Nothing touches the network.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any

from ..metrics import LinkLoad

log = logging.getLogger(__name__)

#: lowest rate the controller backs off to (commands per second, all lights together)
MIN_RATE = 2.0
#: rate used right after a resume or a reconnect, before the controller has measured anything
RESUME_RATE = 5.0
#: commands/s added per second while the repeater is quick
INCREASE_PER_S = 4.0
#: how far one slow reading cuts the rate
DECREASE_FACTOR = 0.5
#: smallest in-flight window, so a very slow repeater still gets a few commands at a time
MIN_WINDOW = 4
#: ceiling when the configured limit is 0 ("no limit"): adaptive control still needs one
UNLIMITED_CEILING = 100.0
#: the gate closes at GATE_FACTOR x the latency limit and reopens below GATE_REOPEN_FACTOR x
GATE_FACTOR = 6.0
GATE_REOPEN_FACTOR = 2.0
#: "quick" means under this share of the latency limit
QUICK_FACTOR = 0.5

#: more DMX frames per second than this, for STORM_SUSTAIN seconds, is a flood (DMX512 tops out at 44)
STORM_FPS = 200.0
#: direction reversals per second across all lights, for STORM_REVERSAL_SUSTAIN seconds
STORM_REVERSALS = 300.0
#: a light must swing by this many percent before turning back counts as a reversal
REVERSAL_MIN_SWING = 10.0
STORM_SUSTAIN = 2.0
STORM_REVERSAL_SUSTAIN = 3.0
#: congested at the minimum rate for this long means the repeater is not recovering
STALL_TRIP_S = 20.0
RATE_WINDOW = 1.0


class Protector:
    """Adaptive rate, in-flight window, gate, storm detection and the pause switch."""

    def __init__(self) -> None:
        self.rate: float | None = None  # current ceiling in commands/s; None until first measured
        self.paused = False
        self.auto = False  # the pause was tripped by a storm, not asked for
        self.reason = ""
        self.paused_at: float | None = None
        self.gate_closed = False
        self.congested = False
        self.latency_ms: float | None = None
        self.decreases = 0
        self.trips = 0
        self._last_decrease = -1e9
        self._last_update: float | None = None
        self._ceiling = 0.0
        self._episode = False
        self._frames: deque[float] = deque()
        self._reversals: deque[float] = deque()
        self._flood_since: float | None = None
        self._reversal_since: float | None = None
        self._stall_since: float | None = None

    # -- inputs ----------------------------------------------------------
    def note_frame(self, now: float) -> None:
        self._frames.append(now)
        self._trim(self._frames, now)

    def note_reversal(self, now: float) -> None:
        self._reversals.append(now)
        self._trim(self._reversals, now)

    @staticmethod
    def _trim(q: deque[float], now: float) -> None:
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()

    def link_down(self) -> None:
        """The connection dropped: start the next one slowly."""
        self.rate = RESUME_RATE if self.rate is None else min(self.rate, RESUME_RATE)

    # -- the controller --------------------------------------------------
    def update(self, now: float, load: LinkLoad, cfg: Any) -> str | None:
        """Take one reading. Returns a reason when a storm has just paused the output."""
        configured = float(getattr(cfg, "max_commands_per_s", 0) or 0)
        self._ceiling = configured if configured > 0 else UNLIMITED_CEILING
        limit = float(getattr(cfg, "latency_limit_ms", 500.0) or 500.0)
        cap = int(getattr(cfg, "max_inflight", 40) or 0)
        dt = 0.0 if self._last_update is None else max(0.0, min(1.0, now - self._last_update))
        self._last_update = now
        self.latency_ms = load.latency_ms
        lat = load.latency_ms

        if self.rate is None:
            self.rate = self._ceiling
        self.rate = min(self.rate, self._ceiling)

        if lat is not None:
            if not self.gate_closed and lat >= GATE_FACTOR * limit:
                self.gate_closed = True
            elif self.gate_closed and lat < GATE_REOPEN_FACTOR * limit:
                self.gate_closed = False
        self.congested = self.gate_closed or (lat is not None and lat > limit) or (cap > 0 and load.inflight >= cap)
        win = self.window(cfg)
        quick = (lat is None or lat < QUICK_FACTOR * limit) and not (win is not None and load.inflight >= win * 0.75)

        if self.congested:
            # one cut per queue-drain time: the latency we read is still about the old rate
            wait = max(1.0, min(5.0, (lat or 0.0) / 1000.0))
            if now - self._last_decrease >= wait:
                self.rate = max(MIN_RATE, self.rate * DECREASE_FACTOR)
                self._last_decrease = now
                self.decreases += 1
        elif quick:
            self.rate = min(self._ceiling, self.rate + INCREASE_PER_S * dt)

        backed_off = self.congested or self.rate < self._ceiling - 1e-6
        if backed_off and not self._episode:
            self._episode = True
            log.warning("The Lutron repeater is falling behind (%s, %d commands not yet echoed); "
                        "slowing DMX output to %.0f commands/s",
                        "no answer yet" if lat is None else f"{lat:.0f} ms round trip", load.inflight, self.rate)
        elif not backed_off and self._episode:
            self._episode = False
            log.info("The Lutron repeater has caught up; DMX output is back to full speed")

        return self._check_storm(now)

    def _check_storm(self, now: float) -> str | None:
        if self.paused:
            self._flood_since = self._reversal_since = self._stall_since = None
            return None
        self._trim(self._frames, now)
        self._trim(self._reversals, now)
        fps = len(self._frames) / RATE_WINDOW
        revs = len(self._reversals) / RATE_WINDOW

        self._flood_since = self._sustained(self._flood_since, fps > STORM_FPS, now)
        self._reversal_since = self._sustained(self._reversal_since, revs > STORM_REVERSALS, now)
        self._stall_since = self._sustained(self._stall_since,
                                            self.congested and (self.rate or 0) <= MIN_RATE + 1e-6, now)
        reason = None
        if self._flood_since is not None and now - self._flood_since >= STORM_SUSTAIN:
            reason = (f"DMX storm: {fps:.0f} frames per second is far beyond what DMX512 allows "
                      f"({STORM_FPS:.0f}/s). Something is flooding the input")
        elif self._reversal_since is not None and now - self._reversal_since >= STORM_REVERSAL_SUSTAIN:
            reason = (f"DMX storm: lights are being told to reverse direction {revs:.0f} times per second. "
                      "A strobe, a feedback loop or a noisy source cannot be followed by dimmers")
        elif self._stall_since is not None and now - self._stall_since >= STALL_TRIP_S:
            reason = ("The Lutron repeater has not recovered after DMX output slowed to the minimum "
                      f"({MIN_RATE:.0f} commands/s) for {STALL_TRIP_S:.0f} s")
        if reason:
            self.trips += 1
            self.pause(now, reason, auto=True)
        return reason

    @staticmethod
    def _sustained(since: float | None, condition: bool, now: float) -> float | None:
        if not condition:
            return None
        return now if since is None else since

    # -- decisions -------------------------------------------------------
    def effective_rate(self, configured: float) -> float:
        """Commands per second the engine may send now (the configured limit, scaled down by feedback)."""
        ceiling = configured if configured > 0 else UNLIMITED_CEILING
        return min(ceiling, ceiling if self.rate is None else self.rate)

    @property
    def sending_allowed(self) -> bool:
        return not self.paused and not self.gate_closed

    def window(self, cfg: Any) -> int | None:
        """Most level commands that may be in flight now (None = no cap).

        Little's law: a queue of N commands at R commands/s is N/R seconds of delay, so holding
        the delay to the latency limit means holding N to rate x limit. The window therefore
        follows the adaptive rate, with `max_inflight` as the hard ceiling.
        """
        cap = int(getattr(cfg, "max_inflight", 40) or 0)
        if cap <= 0:
            return None
        limit = float(getattr(cfg, "latency_limit_ms", 500.0) or 500.0) / 1000.0
        rate = self._ceiling if self.rate is None else self.rate
        return min(cap, max(MIN_WINDOW, math.ceil(rate * limit)))

    def headroom(self, load: LinkLoad, cfg: Any) -> int | None:
        """How many more commands may be written now without passing the window (None = no cap)."""
        win = self.window(cfg)
        return None if win is None else max(0, win - load.inflight)

    # -- pause switch ----------------------------------------------------
    def pause(self, now: float, reason: str = "Paused by you", auto: bool = False) -> None:
        if self.paused:
            return
        self.paused, self.auto, self.reason, self.paused_at = True, auto, reason, now
        if auto:
            log.error("DMX output paused automatically. %s. Resume it from the header once it is safe.", reason)
        else:
            log.warning("DMX output paused: %s", reason)

    def resume(self, now: float) -> None:
        if not self.paused:
            return
        self.paused = self.auto = False
        self.reason, self.paused_at = "", None
        self.rate = RESUME_RATE  # slow start: the whole current DMX state goes out, so ease into it
        self._last_decrease = now
        self._frames.clear()
        self._reversals.clear()
        self._flood_since = self._reversal_since = self._stall_since = None
        log.info("DMX output resumed; ramping up from %.0f commands/s", RESUME_RATE)

    # -- GUI -------------------------------------------------------------
    @property
    def state(self) -> str:
        if self.paused:
            return "tripped" if self.auto else "paused"
        if self.gate_closed:
            return "hold"
        if self.congested or (self.rate is not None and self.rate < self._ceiling - 1e-6):
            return "backoff"
        return "ok"

    def status(self, now: float) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "paused": self.paused,
            "auto": self.auto,
            "paused_for_s": None if self.paused_at is None else round(max(0.0, now - self.paused_at)),
            "rate": None if self.rate is None else round(self.rate, 1),
            "ceiling": round(self._ceiling, 1),
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms),
            "gate_closed": self.gate_closed,
            "slowdowns": self.decreases,
            "trips": self.trips,
        }
