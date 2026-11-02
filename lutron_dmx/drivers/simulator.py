"""In-memory simulated Lutron system for offline development, demos and tests.

The inventory is seeded from ``sample_data/simulator_inventory.json`` (the
names mirror the real house) and uses RA2-style integration IDs so that
scenes / DMX mappings created against the simulator look like real ones.
Fades are simulated by stepping the level in a background task and calling
``_notify`` on every step, just like monitoring messages from real hardware.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
from pathlib import Path
from typing import Any

from ..models import Area, Keypad, Schedule, SystemInventory, Zone, ZoneType
from .base import DriverError, LutronDriver

#: Located next to this module so it also resolves inside a PyInstaller bundle
#: (as long as ``lutron_dmx/drivers/sample_data`` is added as data).
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


class SimulatorDriver(LutronDriver):
    system_name = "Simulator"

    #: pretend echo latency in ms (base, plus up to ECHO_JITTER_MS of jitter) so the status area has numbers to show
    ECHO_BASE_MS = 40.0
    ECHO_JITTER_MS = 20.0

    def __init__(self, config: Any = None, *, fade_step: float = FADE_STEP) -> None:
        super().__init__()
        self.config = config
        self.fade_step = fade_step
        self._jitter = random.Random(1)
        self._raw = _load_raw()
        self._inventory = inventory_from_dict(self._raw)
        self._connected = False
        self._fades: dict[str, asyncio.Task[None]] = {}

    # -- lifecycle -------------------------------------------------------
    async def connect(self) -> None:
        self._connected = True
        self.metrics.mark_connected()

    async def close(self) -> None:
        self._connected = False
        self.metrics.mark_disconnected()
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
        self.metrics.echo.add(self.ECHO_BASE_MS + self._jitter.random() * self.ECHO_JITTER_MS)
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
