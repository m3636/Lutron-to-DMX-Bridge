"""In-memory simulated Lutron system for offline development, demos and tests.

The inventory is seeded from ``sample_data/simulator_inventory.json`` (the
names mirror the real house) and uses RA2-style integration IDs so that
scenes / DMX mappings created against the simulator look like real ones.
Fades are simulated by stepping the level in a background task and calling
``_notify`` on every step, just like monitoring messages from real hardware.

Optionally the simulator models a slow repeater: with ``service_ms`` (or the
``LUTRON_DMX_SIM_SERVICE_MS`` environment variable) every command and every latency
probe waits its turn in a first-in-first-out queue and the repeater takes that long
over each one, like a real Main Repeater working through its telnet commands. A
light changes level when its command is *executed*, not when it was sent, so a
backlog keeps moving lights after whoever sent it has gone. Used to test the DMX
protection (``dmx/protect.py``) without hardware; fades are ignored in this mode.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..metrics import LOAD_STALE_S, LinkLoad
from ..models import Area, Keypad, Schedule, SystemInventory, Zone, ZoneType
from .base import DriverError, LutronDriver

#: Located next to this module so it also resolves inside a PyInstaller bundle
#: (as long as ``lutron_to_dmx_bridge/drivers/sample_data`` is added as data).
SAMPLE_DATA_DIR = Path(__file__).resolve().parent / "sample_data"
INVENTORY_FILE = SAMPLE_DATA_DIR / "simulator_inventory.json"

#: Interval between simulated fade steps (seconds) -> 20 updates per second.
FADE_STEP = 0.05

# Parsed once; each driver instance gets its own deep copy so state never leaks.
_RAW_CACHE: dict[str, Any] | None = None


def _load_raw() -> dict[str, Any]:
    global _RAW_CACHE
    if _RAW_CACHE is None:
        _RAW_CACHE = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
    return copy.deepcopy(_RAW_CACHE)


def inventory_from_dict(raw: dict[str, Any]) -> SystemInventory:
    """Build a SystemInventory from the dict shape produced by ``to_dict``."""
    return SystemInventory(
        areas=[Area(id=str(a["id"]), name=a["name"]) for a in raw.get("areas", [])],
        zones=[
            Zone(
                id=str(z["id"]),
                name=z["name"],
                area_id=z.get("area_id"),
                type=ZoneType(z.get("type", "dimmer")),
                level=float(z.get("level", 0.0)),
            )
            for z in raw.get("zones", [])
        ],
        keypads=[
            Keypad(
                id=str(k["id"]),
                name=k["name"],
                area_id=k.get("area_id"),
                buttons=list(k.get("buttons", [])),
            )
            for k in raw.get("keypads", [])
        ],
        schedules=[
            Schedule(
                id=str(s["id"]),
                name=s["name"],
                enabled=bool(s.get("enabled", True)),
                description=s.get("description", ""),
                timeclock_id=s.get("timeclock_id"),
            )
            for s in raw.get("schedules", [])
        ],
    )


@dataclass
class _Job:
    """One command or probe in the modeled repeater's queue."""

    kind: str  # "out" or "probe"
    zone_id: str
    level: float
    fade: float | None
    enqueued: float
    finishes: float


class SimulatorDriver(LutronDriver):
    system_name = "Simulator"

    #: pretend round-trip latency in ms (base, plus up to LATENCY_JITTER_MS of jitter, one sample every
    #: LATENCY_INTERVAL seconds like the real driver's probe) so the status area has numbers to show
    LATENCY_BASE_MS = 40.0
    LATENCY_JITTER_MS = 20.0
    LATENCY_INTERVAL = 2.0

    #: probe spacing in queue mode while commands are flowing / when idle (like the RA2 driver)
    PROBE_FAST = 0.5
    PROBE_IDLE = 2.0
    #: queue mode: commands beyond this many in the queue count as still on the wire, and
    #: reset_connection() discards them (a guess at how much sits in network buffers)
    WIRE_AFTER = 32

    def __init__(self, config: Any = None, *, fade_step: float = FADE_STEP,
                 service_ms: float | None = None, clock: Any = None) -> None:
        """`service_ms`: model a repeater that takes this long per command (0 = instant).
        `clock`: a time source for the model; give one and drive it by calling `settle(now)`."""
        super().__init__()
        self.config = config
        self.fade_step = fade_step
        if service_ms is None:
            try:
                service_ms = float(os.environ.get("LUTRON_DMX_SIM_SERVICE_MS", "0") or 0)
            except ValueError:
                service_ms = 0.0
        self.service = max(0.0, service_ms) / 1000.0
        self._manual_clock = clock is not None
        self._clock = clock or time.monotonic
        self._jobs: deque[_Job] = deque()
        self._busy_until = 0.0  # when the repeater finishes the last job it was given
        self._last_probe = -1e9
        self._last_out = -1e9
        self._model_task: asyncio.Task[None] | None = None
        self.executed = 0  # commands the modeled repeater has carried out
        self.reset_dropped = 0  # commands discarded by reset_connection()
        self._jitter = random.Random(1)
        self._raw = _load_raw()
        self._inventory = inventory_from_dict(self._raw)
        self._connected = False
        self._fades: dict[str, asyncio.Task[None]] = {}
        self._probe_task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------
    async def connect(self) -> None:
        self._connected = True
        self.metrics.mark_connected()
        if self.service and not self._manual_clock:
            if self._model_task is None or self._model_task.done():
                self._model_task = asyncio.create_task(self._model_loop())
        elif not self.service and (self._probe_task is None or self._probe_task.done()):
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def _model_loop(self) -> None:
        while True:
            await self.settle(self._clock())
            await asyncio.sleep(0.01)

    # -- modeled repeater queue ------------------------------------------
    def _enqueue(self, kind: str, zone_id: str = "", level: float = 0.0, fade: float | None = None) -> None:
        now = self._clock()
        start = max(self._busy_until, now)
        self._busy_until = start + self.service
        self._jobs.append(_Job(kind, zone_id, level, fade, now, self._busy_until))
        if kind == "out":
            self._last_out = now

    async def settle(self, now: float) -> None:
        """Let the modeled repeater work through everything it can finish by `now`.

        Also sends the latency probes the real driver would send. Times in the model are exact
        (a job finishes when the queue ahead of it says so), whatever moment this is called.
        """
        if not self.service:
            return
        while self._jobs and self._jobs[0].finishes <= now + 1e-9:
            job = self._jobs.popleft()
            if job.kind == "probe":
                self.metrics.latency.add((job.finishes - job.enqueued) * 1000.0, job.finishes)
            else:
                self.executed += 1
                await self._apply(self._require_zone(job.zone_id), job.level,
                                  None if self._manual_clock else job.fade)
        busy = now - self._last_out < 1.0 or bool(self._jobs)
        waiting = sum(1 for j in self._jobs if j.kind == "probe")
        if (self._connected and waiting < 8
                and now - self._last_probe >= (self.PROBE_FAST if busy else self.PROBE_IDLE)):
            self._last_probe = now
            self._enqueue("probe")

    def queue_depth(self) -> int:
        """Level commands the modeled repeater has accepted but not carried out yet."""
        return sum(1 for j in self._jobs if j.kind == "out")

    def inflight(self) -> int:
        return self.queue_depth()

    def load(self) -> LinkLoad:
        if not self.service:
            return super().load()
        now = self._clock()
        lat = self.metrics.latency.fresh_last(LOAD_STALE_S, now)
        probe = next((j for j in self._jobs if j.kind == "probe"), None)
        if probe is not None:
            waited = (now - probe.enqueued) * 1000.0
            lat = waited if lat is None else max(lat, waited)
        return LinkLoad(lat, self.queue_depth())

    async def reset_connection(self) -> bool:
        if not self.service:
            return False
        keep = [j for i, j in enumerate(self._jobs) if i < self.WIRE_AFTER]
        self.reset_dropped += sum(1 for j in list(self._jobs)[self.WIRE_AFTER:] if j.kind == "out")
        self._jobs = deque(keep)
        self._busy_until = keep[-1].finishes if keep else 0.0
        return True

    async def _probe_loop(self) -> None:
        while True:
            self.metrics.latency.add(self.LATENCY_BASE_MS + self._jitter.random() * self.LATENCY_JITTER_MS)
            await asyncio.sleep(self.LATENCY_INTERVAL)

    async def close(self) -> None:
        self._connected = False
        self.metrics.mark_disconnected()
        probe, self._probe_task = self._probe_task, None
        model, self._model_task = self._model_task, None
        for t in (probe, model):
            if t is not None:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
        tasks = list(self._fades.values())
        self._fades.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def connected(self) -> bool:
        return self._connected

    def inventory(self) -> SystemInventory:
        return self._inventory

    # -- control ---------------------------------------------------------
    def _require_zone(self, zone_id: str) -> Zone:
        z = self.zone(zone_id)
        if z is None:
            raise DriverError(f"Unknown zone {zone_id!r}")
        return z

    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        zone = self._require_zone(zone_id)
        self.metrics.commands.add()
        if self.service:
            self._enqueue("out", zone_id, level, fade)  # the lights follow when the repeater gets to it
            return
        await self._apply(zone, level, fade)

    async def _apply(self, zone: Zone, level: float, fade: float | None) -> None:
        zone_id = zone.id
        target = max(0.0, min(100.0, float(level)))
        if zone.type is ZoneType.SWITCH:
            target = 100.0 if target > 0 else 0.0

        # A new command always supersedes a fade in progress, like real dimmers.
        old = self._fades.pop(zone_id, None)
        if old is not None:
            old.cancel()

        if not fade or fade <= 0 or zone.type is ZoneType.SWITCH:
            await self._notify(zone_id, target)
            return
        task = asyncio.create_task(self._run_fade(zone, target, float(fade)))
        self._fades[zone_id] = task

    async def set_levels(self, levels: dict[str, float], fade: float | None = None) -> None:
        # Validate first so a bad id doesn't leave the scene half-applied.
        for zid in levels:
            self._require_zone(zid)
        for zid, lvl in levels.items():
            await self.set_level(zid, lvl, fade)

    async def _run_fade(self, zone: Zone, target: float, fade: float) -> None:
        start_level = zone.level
        start = time.monotonic()
        try:
            while True:
                frac = min(1.0, (time.monotonic() - start) / fade)
                level = round(start_level + (target - start_level) * frac, 2)
                if level != zone.level:
                    await self._notify(zone.id, level)
                if frac >= 1.0:
                    break
                await asyncio.sleep(self.fade_step)
        finally:
            if self._fades.get(zone.id) is asyncio.current_task():
                del self._fades[zone.id]

    async def wait_fades(self) -> None:
        """Wait until all running fades have finished (handy in tests)."""
        while self._fades:
            await asyncio.gather(*list(self._fades.values()), return_exceptions=True)

    async def refresh_levels(self) -> None:
        # Nothing to query; re-announce current levels so listeners resync.
        for z in list(self._inventory.zones):
            await self._notify(z.id, z.level)

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        sched = next((s for s in self._inventory.schedules if s.id == schedule_id), None)
        if sched is None:
            raise DriverError(f"Unknown schedule {schedule_id!r}")
        sched.enabled = bool(enabled)

    # -- backup ----------------------------------------------------------
    async def backup(self) -> dict:
        return {
            "system": self.system_name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": INVENTORY_FILE.name,
            "inventory": self._inventory.to_dict(),
            "levels": {z.id: z.level for z in self._inventory.zones},
        }
