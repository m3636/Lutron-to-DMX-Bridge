"""Abstract driver interface every Lutron backend implements."""

from __future__ import annotations

import abc
from typing import Awaitable, Callable, Union

from ..metrics import DriverMetrics
from ..models import Area, Keypad, Schedule, SystemInventory, Zone

# Called with (zone_id, level_percent) whenever the system reports a level change.
LevelCallback = Callable[[str, float], Union[Awaitable[None], None]]


class DriverError(Exception):
    pass


class LutronDriver(abc.ABC):
    """One connection to one Lutron system. All methods are asyncio coroutines.

    Levels are always 0-100 percent floats. IDs are strings (RA2 integration
    IDs, LEAP hrefs/ids, or simulator ids).
    """

    #: short name shown in the GUI, e.g. "RadioRA 2"
    system_name: str = "Lutron"

    def __init__(self) -> None:
        self._listeners: list[LevelCallback] = []
        self.metrics = DriverMetrics()

    # -- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the connection and load the inventory."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    def inflight(self) -> int:
        """Commands sent that the system has not acknowledged yet (0 if unknown)."""
        return 0

    def link_status(self) -> dict:
        """Counters for the GUI's status area: rates, echo latency, reconnects."""
        return self.metrics.snapshot(self.inflight())

    # -- inventory -------------------------------------------------------
    @abc.abstractmethod
    def inventory(self) -> SystemInventory:
        """Current cached inventory (zones carry their last known level)."""

    def areas(self) -> list[Area]:
        return self.inventory().areas

    def zones(self) -> list[Zone]:
        return self.inventory().zones

    def keypads(self) -> list[Keypad]:
        return self.inventory().keypads

    def schedules(self) -> list[Schedule]:
        return self.inventory().schedules

    def zone(self, zone_id: str) -> Zone | None:
        return next((z for z in self.zones() if z.id == zone_id), None)

    # -- control ---------------------------------------------------------
    @abc.abstractmethod
    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        """Set a zone to `level` percent, optionally over `fade` seconds."""

    async def set_levels(self, levels: dict[str, float], fade: float | None = None) -> None:
        """Set many zones as close to simultaneously as the system allows.

        Drivers may override with a batched implementation.
        """
        import asyncio

        await asyncio.gather(*(self.set_level(z, lvl, fade) for z, lvl in levels.items()))

    @abc.abstractmethod
    async def refresh_levels(self) -> None:
        """Query the system for every zone's current level."""

    @abc.abstractmethod
    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        """Enable or disable (never delete) a schedule / timeclock event."""

    async def refresh_schedules(self, ids: list[str] | None = None) -> set[str]:
        """Re-read schedule enabled states from the system.

        Returns the ids whose state was freshly confirmed by the system. The default
        has nothing to query, so every cached state counts as current.
        """
        wanted = ids if ids is not None else [s.id for s in self.schedules()]
        known = {s.id for s in self.schedules()}
        return {i for i in wanted if i in known}

    # -- backup ----------------------------------------------------------
    @abc.abstractmethod
    async def backup(self) -> dict:
        """Return a JSON-serialisable snapshot of everything we can read."""

    # -- events ----------------------------------------------------------
    def add_listener(self, cb: LevelCallback) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: LevelCallback) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    async def _notify(self, zone_id: str, level: float) -> None:
        import inspect

        z = self.zone(zone_id)
        if z is not None:
            z.level = level
        for cb in list(self._listeners):
            res = cb(zone_id, level)
            if inspect.isawaitable(res):
                await res
