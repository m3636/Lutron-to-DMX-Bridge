"""Ties the active driver, saved config, custom scenes and DMX engine together.

The web GUI and the DMX engine both talk to the Lutron system only through
this class, so they never need to know which driver is active.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from .config import AppConfig, DriverConfig, SceneConfig
from .drivers import LutronDriver, create_driver
from .schedule_sets import ScheduleSetManager

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: AppConfig, driver: LutronDriver | None = None) -> None:
        self.config = config
        self.driver: LutronDriver = driver or create_driver(config.driver)
        self.dmx = None  # set by start() when DMX is enabled; see lutron_to_dmx_bridge.dmx.engine
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self.schedule_sets = ScheduleSetManager(self)

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        try:
            await self.driver.connect()
            self.last_error = None
        except Exception as exc:  # keep the GUI usable so the user can fix settings
            log.exception("Driver connect failed")
            self.last_error = str(exc)
            await self._try_rediscover()
        await self.restart_dmx()

    async def _try_rediscover(self) -> None:
        """If the saved IP no longer answers, look the processor up again by its mDNS name."""
        cfg = self.config.driver
        if not (cfg.hostname and cfg.kind == "leap"):
            return
        from .discovery import rediscover_host

        new_host = await rediscover_host(cfg.hostname)
        if not new_host or new_host == cfg.host:
            return
        log.info("Lutron processor %s moved from %s to %s", cfg.hostname, cfg.host, new_host)
        cfg.host = new_host
        self.config.save()
        await self.driver.close()
        self.driver = create_driver(cfg)
        try:
            await self.driver.connect()
            self.last_error = None
        except Exception as exc:
            log.exception("Driver connect failed")
            self.last_error = str(exc)

    async def stop(self) -> None:
        if self.dmx is not None:
            await self.dmx.stop()
            self.dmx = None
        await self.driver.close()

    async def reconfigure_driver(self, driver_config: DriverConfig) -> None:
        async with self._lock:
            await self.driver.close()
            self.config.driver = driver_config
            self.config.save()
            self.driver = create_driver(driver_config)
            try:
                await self.driver.connect()
                self.last_error = None
            except Exception as exc:
                log.exception("Driver connect failed")
                self.last_error = str(exc)
            if self.dmx is not None:
                self.dmx.controller = self

    async def restart_dmx(self) -> None:
        from .dmx.engine import DmxEngine

        if self.dmx is not None:
            await self.dmx.stop()
            self.dmx = None
        if self.config.dmx.enabled:
            self.dmx = DmxEngine(self)
            try:
                await self.dmx.start()
            except Exception as exc:
                log.exception("DMX start failed")
                self.last_error = f"DMX: {exc}"

    def save(self) -> None:
        self.config.save()

    async def import_config(self, parsed, sections: list[str] | None = None, restore_active_set: bool = False):
        """Replace settings from a parsed export (see ``config_transfer``) and apply them to the running app."""
        from .config_transfer import import_into_config

        result = import_into_config(self.config, parsed, sections, restore_active_set)
        if result.driver_changed:
            await self.reconfigure_driver(self.config.driver)
        if result.dmx_changed or result.driver_changed:
            try:
                await self.restart_dmx()
            except Exception as exc:  # the settings are saved either way
                log.exception("DMX restart after import failed")
                result.warnings.append(f"DMX restart failed: {exc}")
        return result

    # -- direct control --------------------------------------------------
    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        level = max(0.0, min(100.0, float(level)))
        await self.driver.set_level(zone_id, level, fade)

    # -- custom scenes (stored by this app, not in Lutron) ---------------
    def scene(self, scene_id: str) -> SceneConfig | None:
        return next((s for s in self.config.scenes if s.id == scene_id), None)

    def save_scene(self, name: str, levels: dict[str, float], fade: float | None = None,
                   scene_id: str | None = None) -> SceneConfig:
        existing = self.scene(scene_id) if scene_id else None
        if existing:
            existing.name, existing.levels, existing.fade = name, dict(levels), fade
            scene = existing
        else:
            scene = SceneConfig(id=scene_id or uuid.uuid4().hex[:8], name=name,
                                levels=dict(levels), fade=fade)
            self.config.scenes.append(scene)
        self.save()
        return scene

    def capture_scene(self, name: str, zone_ids: list[str]) -> SceneConfig:
        levels = {z.id: z.level for z in self.driver.zones() if z.id in zone_ids}
        return self.save_scene(name, levels)

    def delete_scene(self, scene_id: str) -> bool:
        before = len(self.config.scenes)
        self.config.scenes = [s for s in self.config.scenes if s.id != scene_id]
        if len(self.config.scenes) != before:
            self.save()
            return True
        return False

    async def activate_scene(self, scene_id: str) -> None:
        scene = self.scene(scene_id)
        if scene is None:
            raise KeyError(scene_id)
        await self.driver.set_levels(scene.levels, scene.fade)

    # -- schedules -------------------------------------------------------
    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        await self.driver.set_schedule_enabled(schedule_id, enabled)
