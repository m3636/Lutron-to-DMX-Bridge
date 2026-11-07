"""DMX engine: receives one universe and drives Lutron zones from it.

Each enabled `DmxFixtureMapping` occupies `len(params)` consecutive channels
starting at `address`. Supported params:

* ``intensity`` — 0-255 -> 0-100 %
* ``on_off``    — >=128 on, <128 off. When on, the fixture's ``intensity``
  param is used if it has one, otherwise 100 %.
* ``fade``      — 0-255 -> 0.0-25.5 s fade applied to this fixture's commands.

Unknown params still consume a channel (so addressing stays predictable) but
are ignored and reported in ``status()["warnings"]``.

Sending policy (Lutron systems handle a few commands per second per zone,
not 44 Hz DMX):

* Zones are only sent when their target differs from the last value *we*
  sent by at least DEADBAND percent (a full 0 or 100 always goes through).
* Each zone is rate limited to ``config.dmx.max_rate_hz``.
* All zones together are limited to ``config.dmx.max_commands_per_s`` (a token
  bucket). A RadioRA 2 repeater works through its telnet commands one after
  another, so 80 zones x 5 Hz = 400 commands/s only builds a queue inside the
  repeater and the lights then change one after another, seconds late. When
  more zones are due than the budget allows, the ones sent longest ago go
  first so every zone is served in turn. The bucket holds at least one command
  per mapped zone, so a whole-house cue still goes out at once.
* A zone whose DMX value is gliding (a run of small changes: a fade or ramp
  from the console) is sent with a fade time, so the dimmer keeps moving
  between commands instead of stepping. The fade is the time until the zone's
  next command is due (gliding zones / budget) and the level is extrapolated
  to where the ramp will be by then, so zones stay in step even though each
  one gets only a few commands per second. A small first move waits RAMP_HOLD
  to see whether it starts a ramp. A jump (a cue snapping to a new level) is
  sent with no fade, and a ``fade`` channel from the console always wins.
* Changes made elsewhere (keypads, the GUI) are not fought: we only resend
  when the DMX value itself changes (latest takes precedence).
* Zones becoming ready in the same tick with the same fade go out as one
  ``driver.set_levels`` call.
* The command budget above is a ceiling, not a promise: ``dmx.protect`` (on by
  default) lowers it while the repeater's round-trip latency is high, caps the
  commands written but not yet echoed, stops sending when the repeater is seconds
  behind, and pauses the output on a DMX storm. See ``protect.py``. A paused
  engine keeps tracking the DMX input but writes nothing to the Lutron system.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import DmxFixtureMapping
from ..metrics import LinkLoad, RateWindow
from ..models import ZoneType
from . import DMX_SLOTS
from .protect import REVERSAL_MIN_SWING, Protector

if TYPE_CHECKING:  # pragma: no cover
    from ..controller import Controller

log = logging.getLogger(__name__)

SUPPORTED_PARAMS = ("intensity", "on_off", "fade")
DEADBAND = 0.5  # percent
SETTLE_TIME = 0.3  # s a sub-deadband change must be stable before it's sent
TICK = 0.05  # s, trailing-edge flush period
FPS_WINDOW = 1.0  # s

# Ramp smoothing: a target that changes by <= RAMP_STEP percent at a time,
# RAMP_MIN_RUN times in a row with gaps <= RAMP_GAP, is treated as a ramp.
RAMP_STEP = 5.0  # percent per DMX frame
RAMP_GAP = 0.5  # s
RAMP_MIN_RUN = 3
RESEND_FRACTION = 0.7  # a gliding zone is not sent again before this share of its fade has passed...
TRACK_ERROR = 6.0  # ...unless the light would be this many percent away from the DMX value
RAMP_HOLD = 0.25  # s a small first move waits, to see whether it is the start of a ramp
SMOOTH_MIN_FADE = 0.2  # s
SMOOTH_MAX_FADE = 5.0  # s
MAX_LEAD = 50.0  # percent a command may run ahead of the DMX value

SOURCE_LABELS = {
    "artnet": "Art-Net",
    "sacn": "sACN (E1.31)",
    "usb": "USB (Enttec DMX USB Pro)",
}


# ---------------------------------------------------------------------------
# Mapping helpers (pure)
# ---------------------------------------------------------------------------
def auto_assign(zone_ids: list[str], params: list[str] | None = None, start: int = 1) -> list[DmxFixtureMapping]:
    """Sequentially patch zones: each fixture gets len(params) consecutive channels."""
    params = list(params or ["intensity"])
    if not params:
        raise ValueError("params must not be empty")
    width = len(params)
    last = start + width * len(zone_ids) - 1
    if start < 1 or (zone_ids and last > DMX_SLOTS):
        raise ValueError(
            f"{len(zone_ids)} fixtures x {width} channels from {start} needs channels "
            f"{start}-{last}, beyond 512"
        )
    return [
        DmxFixtureMapping(zone_id=z, address=start + i * width, params=list(params))
        for i, z in enumerate(zone_ids)
    ]


def validate_mappings(mappings: list[DmxFixtureMapping]) -> list[str]:
    """Human-readable problems with the patch (empty list = all good)."""
    warnings: list[str] = []
    owner: dict[int, str] = {}
    zones_seen: dict[str, int] = {}
    for m in mappings:
        if not m.enabled:
            continue
        label = f"zone {m.zone_id} @ {m.address}"
        if not m.params:
            warnings.append(f"{label}: no params")
            continue
        end = m.address + len(m.params) - 1
        if m.address < 1 or end > DMX_SLOTS:
            warnings.append(f"{label}: channels {m.address}-{end} outside 1-512")
        for p in m.params:
            if p not in SUPPORTED_PARAMS:
                warnings.append(f"{label}: unknown param {p!r} (ignored)")
        for ch in range(max(m.address, 1), min(end, DMX_SLOTS) + 1):
            if ch in owner and owner[ch] != label:
                warnings.append(f"channel {ch} overlaps: {owner[ch]} and {label}")
            owner.setdefault(ch, label)
        zones_seen[m.zone_id] = zones_seen.get(m.zone_id, 0) + 1
    for z, n in zones_seen.items():
        if n > 1:
            warnings.append(f"zone {z} is mapped {n} times; the last mapping wins")
    return warnings


def fixture_target(m: DmxFixtureMapping, frame: bytes) -> tuple[float | None, float | None]:
    """(level %, fade s) for one mapping from a 512-byte frame.

    Level is None when the mapping has no level-producing param or its
    channels are out of range.
    """
    values: dict[str, int] = {}
    for i, p in enumerate(m.params):
        ch = m.address + i
        if 1 <= ch <= DMX_SLOTS and p not in values:
            values[p] = frame[ch - 1]
    fade = values["fade"] / 10.0 if "fade" in values else None
    intensity = values["intensity"] * 100.0 / 255.0 if "intensity" in values else None
    if "on_off" in values:
        if values["on_off"] >= 128:
            level = intensity if intensity is not None else 100.0
        else:
            level = 0.0
    else:
        level = intensity
    if level is not None:
        level = round(level, 2)
    return level, fade


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
@dataclass
class _ZoneState:
    sent: float | None  # last level we sent (or the zone's level at start)
    sent_target: float | None = None  # the DMX value that command was computed from
    target: float | None = None
    fade: float | None = None
    target_changed_at: float = 0.0
    last_send: float = -1e9
    smooth: bool = True  # False for on/off, shade and fan outputs: they ignore ramps
    ramp_run: int = 0  # consecutive small target changes
    run_started_at: float = 0.0
    run_start_level: float = 0.0
    # What the last command should make the light do: from plan_x0 at plan_t0
    # to plan_x1 at plan_t0 + plan_fade (plan_fade 0 = jumped there).
    plan_t0: float = 0.0
    plan_x0: float = 0.0
    plan_x1: float | None = None
    plan_fade: float = 0.0
    # Direction-reversal tracking for storm detection (see DmxEngine._track_reversal).
    rev_anchor: float | None = None
    rev_dir: int = 0

    def light_at(self, now: float) -> float | None:
        """Where our last command should have the light at `now`."""
        if self.plan_x1 is None:
            return self.sent
        if self.plan_fade > 0 and now < self.plan_t0 + self.plan_fade:
            frac = max(0.0, (now - self.plan_t0) / self.plan_fade)
            return self.plan_x0 + (self.plan_x1 - self.plan_x0) * frac
        return self.plan_x1


class DmxEngine:
    def __init__(self, controller: "Controller", *, port: int | None = None,
                 receiver_factory: Any = None) -> None:
        """`port` overrides the UDP port (tests); `receiver_factory(engine)` lets
        tests inject a receiver."""
        self.controller = controller
        self._port_override = port
        self._receiver_factory = receiver_factory
        self.receiver: Any = None
        self.running = False
        self.error: str | None = None
        self.packets = 0
        self.commands_sent = 0
        self.last_packet_time: float | None = None  # epoch seconds
        self._last_packet_mono: float | None = None
        self._frame = bytes(DMX_SLOTS)
        self._frame_times: deque[float] = deque()
        self._zones: dict[str, _ZoneState] = {}
        self._tokens = 0.0
        self._tokens_at: float | None = None
        #: zones that were due at the last flush but did not fit in the command budget
        self.held_back = 0
        self.commands = RateWindow()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.warnings: list[str] = []
        self._clock = time.monotonic
        self.protector = Protector()

    # -- config shortcuts ------------------------------------------------
    @property
    def config(self):
        return self.controller.config.dmx

    @property
    def last_frame(self) -> bytes:
        return self._frame

    def _min_interval(self) -> float:
        rate = self.config.max_rate_hz or 0
        return 1.0 / rate if rate > 0 else 0.0

    def _protecting(self) -> bool:
        return bool(getattr(self.config, "protect", True))

    def _budget(self) -> float:
        """Commands per second across all zones right now (0 = unlimited).

        The configured limit, scaled down by the protector while the repeater is slow.
        """
        configured = float(getattr(self.config, "max_commands_per_s", 0) or 0)
        if self._protecting():
            return self.protector.effective_rate(configured)
        return configured

    def _active_mappings(self) -> list[DmxFixtureMapping]:
        return [m for m in self.config.mappings if m.enabled]

    # -- lifecycle -------------------------------------------------------
    def _make_receiver(self) -> Any:
        if self._receiver_factory is not None:
            return self._receiver_factory(self)
        cfg = self.config
        src = cfg.source
        if src == "artnet":
            from .artnet import ARTNET_PORT, ArtNetReceiver

            return ArtNetReceiver(cfg.universe, self.on_frame, cfg.bind_ip,
                                  self._port_override if self._port_override is not None else ARTNET_PORT)
        if src == "sacn":
            from .sacn import SACN_PORT, SacnReceiver

            return SacnReceiver(cfg.universe, self.on_frame, cfg.bind_ip,
                                self._port_override if self._port_override is not None else SACN_PORT)
        if src == "usb":
            from .usb import UsbProReceiver

            return UsbProReceiver(cfg.serial_port, self.on_frame)
        raise ValueError(f"Unknown DMX source {src!r} (expected artnet, sacn or usb)")

    async def start(self) -> None:
        self.warnings = validate_mappings(self.config.mappings)
        self._init_zone_states()
        self.error = None
        try:
            self.receiver = self._make_receiver()
            await self.receiver.start()
        except Exception as exc:
            self.error = str(exc)
            self.receiver = None
            raise
        self.running = True
        self._task = asyncio.create_task(self._sender_loop(), name="dmx-sender")

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self.receiver is not None:
            try:
                await self.receiver.stop()
            finally:
                self.receiver = None

    def _init_zone_states(self) -> None:
        driver = self.controller.driver
        self._zones = {}
        for m in self._active_mappings():
            z = None
            try:
                z = driver.zone(m.zone_id)
            except Exception:
                pass
            self._zones[m.zone_id] = _ZoneState(
                sent=z.level if z is not None else None,
                smooth=z is None or getattr(z, "type", ZoneType.DIMMER) is ZoneType.DIMMER)

    # -- frame handling (sync; called from the receiver) -----------------
    def on_frame(self, frame: bytes) -> None:
        self.process_frame(frame, self._clock())
        self._wake.set()

    def process_frame(self, frame: bytes, now: float) -> None:
        self.packets += 1
        self.last_packet_time = time.time()
        self._last_packet_mono = now
        self._frame = frame
        self.protector.note_frame(now)
        self._frame_times.append(now)
        while self._frame_times and now - self._frame_times[0] > FPS_WINDOW:
            self._frame_times.popleft()

        targets: dict[str, tuple[float, float | None]] = {}
        for m in self._active_mappings():
            level, fade = fixture_target(m, frame)
            if level is not None:
                targets[m.zone_id] = (level, fade)  # later mappings win
        for zone_id, (level, fade) in targets.items():
            st = self._zones.get(zone_id)
            if st is None:  # mapping added at runtime
                st = self._zones[zone_id] = _ZoneState(sent=None)
            if st.target != level:
                if (st.target is not None and abs(level - st.target) <= RAMP_STEP
                        and now - st.target_changed_at <= RAMP_GAP):
                    st.ramp_run += 1
                else:
                    st.ramp_run = 0
                    st.run_started_at = now
                    st.run_start_level = level
                self._track_reversal(st, level, now)
                st.target = level
                st.target_changed_at = now
            st.fade = fade

    def _track_reversal(self, st: _ZoneState, level: float, now: float) -> None:
        """Count a light turning around after a real swing: ramps and snaps have none, noise and strobes many."""
        if st.rev_anchor is None:
            st.rev_anchor = level
            return
        move = level - st.rev_anchor
        if st.rev_dir == 0:
            if abs(move) >= REVERSAL_MIN_SWING:
                st.rev_dir, st.rev_anchor = (1 if move > 0 else -1), level
        elif move * st.rev_dir > 0:
            st.rev_anchor = level  # still going the same way
        elif abs(move) >= REVERSAL_MIN_SWING:
            st.rev_dir, st.rev_anchor = -st.rev_dir, level
            self.protector.note_reversal(now)

    # -- sending ---------------------------------------------------------
    def _due(self, now: float) -> dict[str, tuple[float, float | None]]:
        interval = self._min_interval()
        due: dict[str, tuple[float, float | None]] = {}
        for zone_id, st in self._zones.items():
            if st.target is None:
                continue
            if st.sent is not None:
                if abs(st.target - st.sent) < 1e-6:
                    continue
                ref = st.sent if st.sent_target is None else st.sent_target
                if (abs(st.target - ref) < RAMP_STEP
                        and now - st.run_started_at < RAMP_HOLD - 1e-9):
                    continue  # maybe the first step of a ramp: wait for the next few frames
                settled = now - st.target_changed_at >= SETTLE_TIME - 1e-9
                if (not settled and st.smooth and st.ramp_run >= RAMP_MIN_RUN and st.plan_fade > 0
                        and now < st.plan_t0 + st.plan_fade
                        and now - st.last_send < RESEND_FRACTION * st.plan_fade
                        and abs(st.target - (st.light_at(now) or 0.0)) <= TRACK_ERROR):
                    continue  # the light is still gliding where we sent it
                big_enough = (abs(st.target - ref) >= DEADBAND or st.target in (0.0, 100.0)
                              or settled)
                if not big_enough:
                    continue
            if now - st.last_send < interval - 1e-9:
                continue
            due[zone_id] = (st.target, st.fade)
        return due

    def _within_budget(self, due: dict[str, tuple[float, float | None]], now: float,
                       headroom: int | None = None) -> dict[str, tuple[float, float | None]]:
        """Trim `due` to the global command budget, longest-waiting zones first.

        `headroom` (protection on) is how many more commands may be written before the
        in-flight cap is reached, so even a burst cannot put more than the cap into the repeater.
        """
        rate = self._budget()
        allowed = len(due)
        if rate > 0:
            cap = max(rate, float(len(self._zones)))
            if self._protecting() and self.protector.state != "ok":
                cap = max(rate, 1.0)  # the repeater is struggling: no whole-house burst credit
            if self._tokens_at is None:
                self._tokens = cap
            else:
                self._tokens = min(cap, self._tokens + max(0.0, now - self._tokens_at) * rate)
            self._tokens_at = now
            allowed = int(self._tokens + 1e-9)
        if headroom is not None:
            allowed = min(allowed, headroom)
        if allowed >= len(due):
            if rate > 0:
                self._tokens -= len(due)
            return due
        oldest = sorted(due, key=lambda z: self._zones[z].last_send)[:allowed]
        if rate > 0:
            self._tokens -= len(oldest)
        return {z: due[z] for z in oldest}

    def _glide_time(self, now: float) -> float:
        """Seconds until a gliding zone can expect its next command."""
        active = sum(1 for st in self._zones.values()
                     if st.smooth and st.ramp_run >= RAMP_MIN_RUN and now - st.target_changed_at <= RAMP_GAP)
        rate = self._budget()
        wait = max(self._min_interval(), active / rate if rate > 0 else 0.0)
        return round(min(SMOOTH_MAX_FADE, max(SMOOTH_MIN_FADE, wait)), 1)

    def _shape(self, zone_id: str, target: float, fade: float | None,
               now: float, glide: float) -> tuple[float, float | None]:
        """(level, fade) to send for a zone whose DMX value is `target`.

        The console's own fade channel wins. Otherwise a gliding zone gets a
        fade of `glide` seconds and a level extrapolated to the end of that
        fade, so the light is where the ramp is going to be when its next
        command lands.
        """
        if fade is not None:
            return target, fade
        st = self._zones[zone_id]
        if not st.smooth or st.ramp_run < RAMP_MIN_RUN:
            return target, None
        level = target
        if st.sent_target is not None and st.last_send >= st.run_started_at:
            ref_t, ref_level = st.last_send, st.sent_target
        else:
            ref_t, ref_level = st.run_started_at, st.run_start_level
        since = now - ref_t
        if since > 0:
            slope = (target - ref_level) / since
            level = min(100.0, max(0.0, target + max(-MAX_LEAD, min(MAX_LEAD, slope * glide))))
            if slope and level in (0.0, 100.0) and abs(level - target) < abs(slope) * glide:
                # The ramp reaches the end of the range inside this fade: arrive on time.
                glide = round(max(SMOOTH_MIN_FADE, abs(level - target) / abs(slope)), 1)
        return round(level, 2), glide

    async def flush(self, now: float | None = None) -> int:
        """Send every zone that is due. Returns the number of zones sent."""
        now = self._clock() if now is None else now
        driver = self.controller.driver
        load = self._load(driver) if self._protecting() else None
        if load is not None and self.protector.update(now, load, self.config):
            await self._release_backlog(driver, load)  # a storm just paused the output
        if self.protector.paused:
            self.held_back = 0
            return 0
        if not getattr(driver, "connected", True):
            self.protector.link_down()
            self.held_back = 0
            return 0
        due = self._due(now)
        if not due:
            self.held_back = 0
            return 0
        waiting = len(due)
        if load is not None and not self.protector.sending_allowed:
            self.held_back = waiting  # the repeater is seconds behind: let it catch up first
            return 0
        due = self._within_budget(due, now, self.protector.headroom(load, self.config) if load is not None else None)
        self.held_back = waiting - len(due)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("DMX: %d zones due, %d sent now, %d held back by the %.0f/s command budget",
                      waiting, len(due), waiting - len(due), self._budget())
        if not due:
            return 0
        glide = self._glide_time(now)
        # Group by fade so each group can be one batched driver call.
        groups: dict[float | None, dict[str, float]] = {}
        for zone_id, (level, fade) in due.items():
            cmd_level, cmd_fade = self._shape(zone_id, level, fade, now, glide)
            groups.setdefault(cmd_fade, {})[zone_id] = cmd_level
        sent = 0
        for fade, levels in groups.items():
            try:
                if len(levels) == 1:
                    (zone_id, level), = levels.items()
                    await driver.set_level(zone_id, level, fade)
                else:
                    await driver.set_levels(levels, fade)
            except Exception as exc:
                # Leave the zones dirty; they'll be retried at the rate limit.
                self.error = f"Send failed: {exc}"
                log.warning("DMX send failed: %s", exc)
                for zone_id in levels:
                    self._zones[zone_id].last_send = now
                continue
            for zone_id, level in levels.items():
                st = self._zones[zone_id]
                st.plan_x0 = st.light_at(now) if st.light_at(now) is not None else level
                st.plan_t0, st.plan_x1, st.plan_fade = now, level, fade or 0.0
                st.sent = level
                st.sent_target = due[zone_id][0]
                st.last_send = now
            self.commands_sent += len(levels)
            self.commands.add(len(levels), now)
            sent += len(levels)
        if sent and self.error and self.error.startswith("Send failed"):
            self.error = None
        return sent

    @staticmethod
    def _load(driver: Any) -> LinkLoad:
        try:
            load = driver.load()
        except Exception:  # a driver without the probe: assume the best
            return LinkLoad()
        return load if isinstance(load, LinkLoad) else LinkLoad()

    # -- pause switch ----------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.protector.paused

    async def pause(self, reason: str = "Paused by you") -> None:
        """Stop writing to the Lutron system. The DMX input keeps being tracked."""
        self.protector.pause(self._clock(), reason)
        driver = self.controller.driver
        await self._release_backlog(driver, self._load(driver))

    def resume(self) -> None:
        """Start writing again, from a low rate that ramps up while the repeater keeps up."""
        self.protector.resume(self._clock())
        self._tokens_at = None
        self._wake.set()

    async def _release_backlog(self, driver: Any, load: LinkLoad) -> None:
        """Pausing because the repeater is behind: drop the connection so what is still in the
        network buffers is discarded rather than delivered after the pause."""
        limit = float(getattr(self.config, "latency_limit_ms", 500.0) or 500.0)
        behind = (load.latency_ms or 0.0) > limit or load.inflight > 5
        reset = getattr(driver, "reset_connection", None)
        if behind and reset is not None:
            try:
                await reset()
            except Exception:
                log.exception("Could not reset the Lutron connection")

    async def _sender_loop(self) -> None:
        tick = min(TICK, self._min_interval() or TICK)
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the loop die
                log.exception("DMX flush failed")

    # -- GUI -------------------------------------------------------------
    def fps(self) -> float:
        now = self._clock()
        while self._frame_times and now - self._frame_times[0] > FPS_WINDOW:
            self._frame_times.popleft()
        return round(len(self._frame_times) / FPS_WINDOW, 1)

    def status(self) -> dict:
        cfg = self.config
        st: dict[str, Any] = {
            "source": cfg.source,
            "source_label": SOURCE_LABELS.get(cfg.source, cfg.source),
            "universe": cfg.universe,
            "running": self.running,
            "packets": self.packets,
            "last_packet_time": self.last_packet_time,
            "last_packet_age": (round(self._clock() - self._last_packet_mono, 2)
                                if self._last_packet_mono is not None else None),
            "fps": self.fps(),
            "error": self.error or getattr(self.receiver, "error", None),
            "warnings": list(self.warnings),
            "commands_sent": self.commands_sent,
            "commands_per_s": self.commands.rate(self._clock()),
            "held_back": self.held_back,
            "paused": self.paused,
            "protect": {**self.protector.status(self._clock()), "enabled": self._protecting(),
                        "latency_limit_ms": getattr(cfg, "latency_limit_ms", 500.0),
                        "max_inflight": getattr(cfg, "max_inflight", 40)},
            "max_rate_hz": cfg.max_rate_hz,
            "max_commands_per_s": cfg.max_commands_per_s,
            "mapped_zones": len(self._zones),
            "receiver": self.receiver.info() if self.receiver is not None and hasattr(self.receiver, "info") else {},
        }
        if cfg.source == "sacn":
            from .sacn import effective_universe

            st["effective_universe"] = effective_universe(cfg.universe)
        if cfg.source == "usb":
            from .usb import USB_SOURCE_NOTE

            st["note"] = USB_SOURCE_NOTE
        return st

    def channel_map(self) -> list[dict]:
        """One row per patched channel, sorted by channel, with live values."""
        rows: list[dict] = []
        owners: dict[int, int] = {}
        driver = self.controller.driver
        for m in self.config.mappings:
            try:
                z = driver.zone(m.zone_id)
            except Exception:
                z = None
            for i, p in enumerate(m.params):
                ch = m.address + i
                valid = 1 <= ch <= DMX_SLOTS
                if m.enabled and valid:
                    owners[ch] = owners.get(ch, 0) + 1
                rows.append({
                    "channel": ch,
                    "zone_id": m.zone_id,
                    "zone_name": z.name if z is not None else None,
                    "param": p,
                    "supported": p in SUPPORTED_PARAMS,
                    "enabled": m.enabled,
                    "value": self._frame[ch - 1] if valid else None,
                })
        for r in rows:
            r["conflict"] = r["enabled"] and owners.get(r["channel"], 0) > 1
        rows.sort(key=lambda r: (r["channel"], r["zone_id"]))
        return rows
