"""FastAPI application: JSON API, WebSocket live updates and the static SPA.

The app never starts or stops the controller itself (the CLI owns that
lifecycle); it only talks to the Lutron system through ``Controller``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import inspect
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from .. import config_transfer
from ..config import DMX_PARAMS, DMX_SOURCES, DRIVER_KINDS, DmxConfig, DmxFixtureMapping, DriverConfig, data_dir
from ..metrics import STARTED_AT, install_log_counter, uptime_s
from ..schedule_sets import ScheduleSetError
from ..updater import UpdateError, Updater
from .guard import HostOriginGuard

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PASSWORD_PLACEHOLDER = "********"
STATUS_INTERVAL = 1.0  # seconds between status pushes, whatever else is flowing


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class LevelBody(BaseModel):
    level: float = Field(ge=0, le=100)
    fade: float | None = Field(default=None, ge=0, le=3600)


class AreaLevelBody(BaseModel):
    level: float = Field(ge=0, le=100)
    fade: float | None = Field(default=None, ge=0, le=3600)


class SceneBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    levels: dict[str, float] = Field(default_factory=dict)
    fade: float | None = Field(default=None, ge=0, le=3600)


class CaptureBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    #: empty / omitted = every zone
    zone_ids: list[str] = Field(default_factory=list)


class EnabledBody(BaseModel):
    enabled: bool


class ScheduleSetBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    #: schedule ids to disable while the set is active
    disable: list[str] = Field(default_factory=list)
    #: schedule ids to enable while the set is active (optional)
    enable: list[str] = Field(default_factory=list)


class DriverSettingsBody(BaseModel):
    kind: str
    host: str = ""
    hostname: str = ""
    username: str = ""
    password: str = ""
    port: int | None = Field(default=None, ge=1, le=65535)
    integration_report: str = ""
    leap_cert_dir: str = ""


class DiscoverBody(BaseModel):
    #: "auto" = subnet scan only if mDNS finds nothing; true = always; false = never
    scan: bool | str = "auto"


class MappingBody(BaseModel):
    zone_id: str = Field(min_length=1)
    address: int = Field(ge=1, le=512)
    params: list[str] = Field(default_factory=lambda: ["intensity"], min_length=1, max_length=8)
    enabled: bool = True


class DmxSettingsBody(BaseModel):
    enabled: bool = False
    source: str = "artnet"
    universe: int = Field(default=0, ge=0, le=32767)
    bind_ip: str = "0.0.0.0"
    serial_port: str = ""
    max_rate_hz: float = Field(default=5.0, gt=0, le=50)
    max_commands_per_s: float = Field(default=20.0, ge=0, le=1000)
    mappings: list[MappingBody] = Field(default_factory=list)


class AutoAssignBody(BaseModel):
    zone_ids: list[str] = Field(default_factory=list)
    params: list[str] = Field(default_factory=lambda: ["intensity"], min_length=1, max_length=8)
    start: int = Field(default=1, ge=1, le=512)


class UpdateSettingsBody(BaseModel):
    check: bool = True
    token: str = Field(default="", max_length=500)


class UpdateApplyBody(BaseModel):
    version: str = Field(min_length=1, max_length=50)
    #: the user has seen the warnings (schedule set active, DMX running) and wants to go ahead
    confirm: bool = False


class UpdateDismissBody(BaseModel):
    version: str = Field(min_length=1, max_length=50)


class ConfigImportBody(BaseModel):
    #: the parsed JSON of an export file
    document: dict[str, Any]
    #: section ids to import; omitted = the defaults the file has
    sections: list[str] | None = None
    #: also bring back the saved prior schedule states of a set that was active when the file was exported
    restore_active_set: bool = False
    #: required to actually import (the preview does not need it)
    confirm: bool = False


class PairBody(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    trust_presented_cert: bool = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _jsonable(obj: Any) -> Any:
    """Best-effort conversion of driver/engine return values to JSON types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if hasattr(obj, "to_dict"):
        with contextlib.suppress(Exception):
            return _jsonable(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return list(bytes(obj))
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "value") and isinstance(getattr(obj, "value"), (str, int)):
        return obj.value  # enums
    with contextlib.suppress(TypeError):
        return list(obj)  # array.array, numpy-ish
    return str(obj)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _mask_driver(cfg: DriverConfig) -> dict:
    d = dataclasses.asdict(cfg)
    d["password"] = PASSWORD_PLACEHOLDER if cfg.password else ""
    return d


def _validate_params(params: list[str]) -> list[str]:
    bad = [p for p in params if p not in DMX_PARAMS]
    if bad:
        raise HTTPException(400, f"Unsupported DMX parameter(s): {', '.join(bad)}")
    return list(params)


def _fallback_auto_assign(zone_ids: list[str], params: list[str], start: int = 1) -> list[DmxFixtureMapping]:
    out, addr = [], start
    for zid in zone_ids:
        if addr + len(params) - 1 > 512:
            break
        out.append(DmxFixtureMapping(zone_id=zid, address=addr, params=list(params)))
        addr += len(params)
    return out


def _channel_map_from_config(dmx_cfg: DmxConfig) -> dict[str, dict]:
    """channel (1-512, as str) -> {zone_id, param} for enabled mappings."""
    cmap: dict[str, dict] = {}
    for m in dmx_cfg.mappings:
        if not m.enabled:
            continue
        for i, p in enumerate(m.params):
            ch = m.address + i
            if 1 <= ch <= 512:
                cmap.setdefault(str(ch), {"zone_id": m.zone_id, "param": p})
    return cmap


class NoCacheStaticFiles(StaticFiles):
    """Static files the browser must revalidate (a cheap 304 on localhost) instead of guessing they are still fresh."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


#: Assets the page links to; each gets a ?v=<content hash> so an update is never mixed with a cached old copy.
VERSIONED_ASSETS = ("static/style.css", "static/app.js")


def render_index() -> str:
    """index.html with cache-busting asset URLs.

    Without this, a browser that had cached app.js/style.css (no Cache-Control,
    so it guesses freshness from Last-Modified) keeps running the old script
    against the new page after an update, and the new parts stay blank.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in VERSIONED_ASSETS:
        digest = hashlib.sha256((STATIC_DIR / asset.split("/", 1)[1]).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"{asset}"', f'"{asset}?v={digest}"')
    return html


class LiveHub:
    """Fans driver level changes out to every connected WebSocket.

    Tracks which driver instance the listener is registered on so that a
    driver swap (``Controller.reconfigure_driver``) is picked up.
    """

    def __init__(self, controller) -> None:
        self.controller = controller
        self.clients: set[asyncio.Queue] = set()
        self._driver = None

    def ensure_listener(self) -> None:
        drv = self.controller.driver
        if drv is self._driver:
            return
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.remove_listener(self._on_level)
        drv.add_listener(self._on_level)
        self._driver = drv

    def detach(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.remove_listener(self._on_level)
        self._driver = None

    def _on_level(self, zone_id: str, level: float) -> None:
        msg = {"type": "level", "zone_id": zone_id, "level": level}
        for q in list(self.clients):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    def broadcast(self, msg: dict) -> None:
        for q in list(self.clients):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app(controller, discover=None, updater: Updater | None = None, bind_host: str | None = None) -> FastAPI:
    """``updater`` is the release checker; without one (tests, self-check) the app never touches the network.

    ``bind_host`` is the address the server listens on (default: the configured ``web_host``); see ``guard``.
    """
    hub = LiveHub(controller)
    log_counter = install_log_counter()
    index_html = render_index()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        with contextlib.suppress(Exception):
            hub.ensure_listener()
        update_task = asyncio.create_task(updater.run()) if updater is not None else None
        yield
        if update_task is not None:
            update_task.cancel()
            with contextlib.suppress(BaseException):
                await update_task
        hub.detach()

    app = FastAPI(title="Lutron To DMX Bridge", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(HostOriginGuard, bind_host=bind_host or controller.config.web_host)
    app.state.controller = controller
    app.state.hub = hub
    app.state.updater = updater

    # -- shared lookups ----------------------------------------------------
    def drv():
        return controller.driver

    def get_zone(zone_id: str):
        z = drv().zone(zone_id)
        if z is None:
            raise HTTPException(404, f"Unknown zone {zone_id!r}")
        return z

    def get_scene(scene_id: str):
        s = controller.scene(scene_id)
        if s is None:
            raise HTTPException(404, f"Unknown scene {scene_id!r}")
        return s

    async def driver_call(coro):
        try:
            return await coro
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(404, f"Not found: {exc}") from exc
        except Exception as exc:
            log.exception("Driver call failed")
            raise HTTPException(502, f"Lutron system error: {exc}") from exc

    async def dmx_status() -> dict | None:
        engine = getattr(controller, "dmx", None)
        cfg = controller.config.dmx
        base = {"enabled": cfg.enabled, "source": cfg.source, "universe": cfg.universe,
                "running": engine is not None}
        if engine is not None:
            with contextlib.suppress(Exception):
                st = await _maybe_await(engine.status())
                if isinstance(st, dict):
                    base.update(_jsonable(st))
                else:
                    base["detail"] = _jsonable(st)
        return base

    def update_warnings() -> list[str]:
        """Things an update would interrupt; the user has to confirm these."""
        out: list[str] = []
        active = None
        with contextlib.suppress(Exception):
            active = controller.schedule_sets.status()
        if active:
            out.append(f"The schedule set \"{active['set_name']}\" is active. It stays active through the "
                       "restart and can be restored afterwards, but the app is unavailable for a short time.")
        engine = getattr(controller, "dmx", None)
        if engine is not None:
            with contextlib.suppress(Exception):
                st = engine.status()
                age = st.get("last_packet_age")
                if st.get("running") and age is not None and age < 5:
                    out.append("DMX input is live. It pauses for a few seconds while the app restarts; "
                               "the lights keep their last levels.")
        return out

    def update_state() -> dict:
        if updater is None:
            return {"auto_check": False, "available": False, "disabled": True}
        state = updater.public_state()
        state["warnings"] = update_warnings() if state["available"] else []
        return state

    async def status_payload() -> dict:
        d = drv()
        try:
            connected = bool(d.connected)
        except Exception:
            connected = False
        link: dict = {}
        with contextlib.suppress(Exception):
            link = d.link_status()
        queue: dict = {}
        with contextlib.suppress(Exception):
            queue = controller.queue_status()
        return {
            "system_name": getattr(d, "system_name", "Lutron"),
            "connected": connected,
            "last_error": controller.last_error,
            "driver": {"kind": controller.config.driver.kind, "host": controller.config.driver.host},
            "dmx": await dmx_status(),
            "schedule_set": controller.schedule_sets.status(),
            "lutron": link,
            "queue": queue,
            "alerts": log_counter.snapshot(),
            "update": update_state(),
            "app": {"version": __version__, "started_at": STARTED_AT, "uptime_s": round(uptime_s()),
                    "clients": len(hub.clients)},
        }

    # -- status / inventory ------------------------------------------------
    @app.get("/api/status")
    async def api_status():
        return await status_payload()

    @app.post("/api/alerts/clear")
    async def api_alerts_clear():
        log_counter.clear()
        return {"ok": True}

    @app.post("/api/queue/clear")
    async def api_queue_clear():
        """Drop the level commands waiting inside the app; see Controller.clear_queue."""
        result = await driver_call(controller.clear_queue())
        return {"ok": True, **result, "status": await status_payload()}

    @app.get("/api/inventory")
    async def api_inventory():
        try:
            inv = drv().inventory().to_dict()
        except Exception as exc:
            log.exception("inventory failed")
            inv = {"areas": [], "zones": [], "keypads": [], "schedules": [], "error": str(exc)}
        return inv

    # -- zones / areas -----------------------------------------------------
    @app.post("/api/zones/{zone_id}/level")
    async def api_zone_level(zone_id: str, body: LevelBody):
        get_zone(zone_id)
        await driver_call(controller.set_level(zone_id, body.level, body.fade))
        return {"ok": True, "zone_id": zone_id, "level": body.level}

    @app.post("/api/zones/{zone_id}/on")
    async def api_zone_on(zone_id: str):
        get_zone(zone_id)
        await driver_call(controller.set_level(zone_id, 100.0))
        return {"ok": True, "zone_id": zone_id, "level": 100.0}

    @app.post("/api/zones/{zone_id}/off")
    async def api_zone_off(zone_id: str):
        get_zone(zone_id)
        await driver_call(controller.set_level(zone_id, 0.0))
        return {"ok": True, "zone_id": zone_id, "level": 0.0}

    @app.post("/api/areas/{area_id}/level")
    async def api_area_level(area_id: str, body: AreaLevelBody):
        if not any(a.id == area_id for a in drv().areas()):
            raise HTTPException(404, f"Unknown area {area_id!r}")
        zone_ids = [z.id for z in drv().zones() if z.area_id == area_id]
        if zone_ids:
            await driver_call(drv().set_levels({z: body.level for z in zone_ids}, body.fade))
        return {"ok": True, "area_id": area_id, "zone_ids": zone_ids, "level": body.level}

    # -- scenes ------------------------------------------------------------
    def scene_dict(s) -> dict:
        return dataclasses.asdict(s)

    def check_levels(levels: dict[str, float]) -> dict[str, float]:
        for zid, lvl in levels.items():
            if not 0 <= lvl <= 100:
                raise HTTPException(422, f"Level for zone {zid!r} must be 0-100")
        return {k: float(v) for k, v in levels.items()}

    @app.get("/api/scenes")
    async def api_scenes():
        return [scene_dict(s) for s in controller.config.scenes]

    @app.post("/api/scenes", status_code=201)
    async def api_scene_create(body: SceneBody):
        s = controller.save_scene(body.name.strip(), check_levels(body.levels), body.fade)
        return scene_dict(s)

    @app.post("/api/scenes/capture", status_code=201)
    async def api_scene_capture(body: CaptureBody):
        zone_ids = body.zone_ids or [z.id for z in drv().zones()]
        known = {z.id for z in drv().zones()}
        unknown = [z for z in zone_ids if z not in known]
        if unknown:
            raise HTTPException(404, f"Unknown zone(s): {', '.join(unknown)}")
        s = controller.capture_scene(body.name.strip(), zone_ids)
        return scene_dict(s)

    @app.get("/api/scenes/{scene_id}")
    async def api_scene_get(scene_id: str):
        return scene_dict(get_scene(scene_id))

    @app.put("/api/scenes/{scene_id}")
    async def api_scene_update(scene_id: str, body: SceneBody):
        get_scene(scene_id)
        s = controller.save_scene(body.name.strip(), check_levels(body.levels), body.fade, scene_id=scene_id)
        return scene_dict(s)

    @app.delete("/api/scenes/{scene_id}")
    async def api_scene_delete(scene_id: str):
        if not controller.delete_scene(scene_id):
            raise HTTPException(404, f"Unknown scene {scene_id!r}")
        return {"ok": True}

    @app.post("/api/scenes/{scene_id}/activate")
    async def api_scene_activate(scene_id: str):
        get_scene(scene_id)
        await driver_call(controller.activate_scene(scene_id))
        return {"ok": True}

    # -- schedules ---------------------------------------------------------
    @app.post("/api/schedules/{schedule_id}/enabled")
    async def api_schedule_enabled(schedule_id: str, body: EnabledBody):
        if not any(s.id == schedule_id for s in drv().schedules()):
            raise HTTPException(404, f"Unknown schedule {schedule_id!r}")
        await driver_call(controller.set_schedule_enabled(schedule_id, body.enabled))
        return {"ok": True, "schedule_id": schedule_id, "enabled": body.enabled}

    # -- schedule sets (e.g. "Performance mode") ---------------------------
    sets = controller.schedule_sets

    async def set_call(coro):
        try:
            return await coro
        except ScheduleSetError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, f"Unknown schedule set {exc}") from exc
        except Exception as exc:
            log.exception("Schedule set call failed")
            raise HTTPException(502, f"Lutron system error: {exc}") from exc

    def set_dict(s) -> dict:
        live = {x.id: x for x in drv().schedules()}

        def refs(items):
            return [{"id": r.id, "name": live[r.id].name if r.id in live else r.name,
                     "missing": r.id not in live} for r in items]

        d = {"id": s.id, "name": s.name, "disable": refs(s.disable), "enable": refs(s.enable)}
        d["active"] = bool(sets.active and sets.active.set_id == s.id)
        return d

    def sync_set(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except ScheduleSetError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, f"Unknown schedule set {exc}") from exc

    @app.get("/api/schedule-sets")
    async def api_schedule_sets():
        return {"sets": [set_dict(s) for s in sets.sets], "active": sets.status()}

    @app.post("/api/schedule-sets", status_code=201)
    async def api_schedule_set_create(body: ScheduleSetBody):
        return set_dict(sync_set(sets.save, body.name, body.disable, body.enable))

    @app.put("/api/schedule-sets/{set_id}")
    async def api_schedule_set_update(set_id: str, body: ScheduleSetBody):
        return set_dict(sync_set(sets.save, body.name, body.disable, body.enable, set_id=set_id))

    @app.delete("/api/schedule-sets/{set_id}")
    async def api_schedule_set_delete(set_id: str):
        sync_set(sets.delete, set_id)
        return {"ok": True}

    @app.post("/api/schedule-sets/active/deactivate")
    async def api_schedule_set_deactivate():
        result = await set_call(sets.deactivate())
        hub.broadcast({"type": "inventory_changed"})
        return result

    @app.post("/api/schedule-sets/active/forget")
    async def api_schedule_set_forget():
        sync_set(sets.forget_active)
        return {"ok": True}

    @app.post("/api/schedule-sets/{set_id}/activate")
    async def api_schedule_set_activate(set_id: str):
        sync_set(sets.get, set_id)
        result = await set_call(sets.activate(set_id))
        hub.broadcast({"type": "inventory_changed"})
        return result

    # -- backup ------------------------------------------------------------
    @app.get("/api/backup")
    async def api_backup():
        lutron = await driver_call(drv().backup())
        now = datetime.now()
        cfg = controller.config.to_dict()
        cfg["driver"] = _mask_driver(controller.config.driver)
        cfg["updates"]["token"] = PASSWORD_PLACEHOLDER if controller.config.updates.token else ""
        payload = {
            "format": "lutron-to-dmx-bridge-backup",
            "version": 1,
            "created": now.isoformat(timespec="seconds"),
            "system_name": getattr(drv(), "system_name", "Lutron"),
            "driver_kind": controller.config.driver.kind,
            "lutron": _jsonable(lutron),
            "app_config": cfg,
        }
        filename = f"lutron-backup-{now:%Y%m%d-%H%M%S}.json"
        body = json.dumps(payload, indent=2)
        try:
            bdir = data_dir() / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            (bdir / filename).write_text(body, encoding="utf-8")
        except OSError:
            log.exception("Could not save backup copy")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # -- settings export / import ------------------------------------------
    def parse_import(body: ConfigImportBody):
        try:
            return config_transfer.parse_document(body.document)
        except config_transfer.ConfigImportError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/config/export")
    async def api_config_export(credentials: bool = False):
        doc = config_transfer.build_export(controller.config, include_credentials=credentials)
        return Response(
            content=json.dumps(doc, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{config_transfer.export_filename()}"',
                     "Cache-Control": "no-store"},
        )

    @app.post("/api/config/import/preview")
    async def api_config_import_preview(body: ConfigImportBody):
        parsed = parse_import(body)
        try:
            chosen = config_transfer.select_sections(parsed, body.sections)
        except config_transfer.ConfigImportError:
            chosen = []  # e.g. a file with only the web address: nothing selected by default
        cfg = controller.config
        zones: set[str] | None = None
        scheds: set[str] | None = None
        names: dict[str, str] = {}
        with contextlib.suppress(Exception):
            if drv().connected:
                zones = {z.id for z in drv().zones()}
                names = {z.id: z.name for z in drv().zones()}
                scheds = {s.id for s in drv().schedules()}
        if "driver" in parsed.sections:
            nd = config_transfer.effective_driver(cfg, parsed)
            if (nd.kind, nd.host) != (cfg.driver.kind, cfg.driver.host):
                zones = scheds = None  # a different Lutron system is about to be connected
        _, adoptable = config_transfer.active_set_status(cfg, parsed, parsed.sections)
        return {
            "file": parsed.info(),
            "sections": [p.to_dict() for p in config_transfer.describe_changes(cfg, parsed, parsed.sections, names)],
            "selected": chosen,
            "warnings": [{"section": sec, "text": text} for sec, text in
                         config_transfer.import_warnings(cfg, parsed, parsed.sections, zones, scheds)],
            "can_restore_active_set": adoptable,
        }

    @app.post("/api/config/import")
    async def api_config_import(body: ConfigImportBody):
        if not body.confirm:
            raise HTTPException(422, "Importing replaces the chosen settings; send confirm: true after reviewing "
                                     "the preview.")
        parsed = parse_import(body)
        try:
            result = await controller.import_config(parsed, body.sections, body.restore_active_set)
        except config_transfer.ConfigImportError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        hub.ensure_listener()
        hub.broadcast({"type": "inventory_changed"})
        return {**result.to_dict(), "status": await status_payload()}

    # -- driver settings ---------------------------------------------------
    @app.get("/api/settings/driver")
    async def api_driver_get():
        return _mask_driver(controller.config.driver)

    @app.put("/api/settings/driver")
    async def api_driver_put(body: DriverSettingsBody):
        if body.kind not in DRIVER_KINDS:
            raise HTTPException(422, f"kind must be one of {', '.join(DRIVER_KINDS)}")
        if body.kind in ("ra2", "leap") and not body.host.strip():
            raise HTTPException(422, "Host / IP address is required for this system type")
        old = controller.config.driver
        password = old.password if body.password == PASSWORD_PLACEHOLDER else body.password
        new = DriverConfig(
            kind=body.kind,
            host=body.host.strip(),
            hostname=body.hostname.strip() or (old.hostname if body.host.strip() == old.host else ""),
            username=body.username,
            password=password,
            port=body.port,
            integration_report=body.integration_report.strip(),
            leap_cert_dir=body.leap_cert_dir.strip(),
        )
        try:
            await controller.reconfigure_driver(new)
        except Exception as exc:  # e.g. unknown kind / driver module missing
            log.exception("reconfigure failed")
            controller.last_error = str(exc)
            raise HTTPException(500, f"Could not apply driver settings: {exc}") from exc
        hub.ensure_listener()
        hub.broadcast({"type": "inventory_changed"})
        result = _mask_driver(controller.config.driver)
        result["status"] = await status_payload()
        return result

    # -- DMX ---------------------------------------------------------------
    @app.get("/api/settings/dmx")
    async def api_dmx_get():
        return dataclasses.asdict(controller.config.dmx)

    @app.put("/api/settings/dmx")
    async def api_dmx_put(body: DmxSettingsBody):
        if body.source not in DMX_SOURCES:
            raise HTTPException(422, f"source must be one of {', '.join(DMX_SOURCES)}")
        mappings = []
        for m in body.mappings:
            params = _validate_params(m.params)
            if m.address + len(params) - 1 > 512:
                raise HTTPException(422, f"Mapping for zone {m.zone_id!r} runs past channel 512")
            mappings.append(DmxFixtureMapping(zone_id=m.zone_id, address=m.address,
                                              params=params, enabled=m.enabled))
        controller.config.dmx = DmxConfig(
            enabled=body.enabled, source=body.source, universe=body.universe,
            bind_ip=body.bind_ip.strip() or "0.0.0.0", serial_port=body.serial_port.strip(),
            max_rate_hz=body.max_rate_hz, max_commands_per_s=body.max_commands_per_s,
            mappings=mappings,
        )
        controller.save()
        warning = None
        try:
            await controller.restart_dmx()
        except ImportError as exc:
            warning = f"DMX engine not available: {exc}"
        except Exception as exc:
            log.exception("DMX restart failed")
            warning = f"DMX restart failed: {exc}"
        result = dataclasses.asdict(controller.config.dmx)
        result["warning"] = warning
        result["status"] = await dmx_status()
        return result

    @app.post("/api/dmx/auto-assign")
    async def api_dmx_auto_assign(body: AutoAssignBody):
        params = _validate_params(body.params)
        zone_ids = body.zone_ids or [z.id for z in drv().zones()]
        try:
            from ..dmx.engine import auto_assign
        except ImportError:
            auto_assign = _fallback_auto_assign
        try:
            result = auto_assign(zone_ids, params, start=body.start)
        except Exception as exc:
            raise HTTPException(400, f"Auto-assign failed: {exc}") from exc
        return {"mappings": _jsonable(result)}

    @app.get("/api/dmx/monitor")
    async def api_dmx_monitor():
        engine = getattr(controller, "dmx", None)
        frame: list[int] = []
        engine_map = None
        if engine is not None:
            with contextlib.suppress(Exception):
                lf = getattr(engine, "last_frame", None)
                if callable(lf):
                    lf = lf()
                if lf is not None:
                    frame = [int(v) for v in bytes(lf)[:512]] if isinstance(lf, (bytes, bytearray, memoryview)) \
                        else [int(v) for v in list(lf)[:512]]
            with contextlib.suppress(Exception):
                engine_map = _jsonable(engine.channel_map())
        return {
            "running": engine is not None,
            "status": await dmx_status(),
            "frame": frame,
            "channel_map": _channel_map_from_config(controller.config.dmx),
            "engine_channel_map": engine_map,
        }

    @app.get("/api/dmx/serial-ports")
    async def api_serial_ports():
        try:
            from ..dmx.usb import list_serial_ports
        except ImportError as exc:
            return {"ports": [], "error": f"USB DMX support unavailable: {exc}"}
        try:
            raw = await _maybe_await(list_serial_ports())
        except Exception as exc:
            return {"ports": [], "error": str(exc)}
        ports = []
        for p in raw or []:
            if isinstance(p, str):
                ports.append({"device": p, "description": ""})
            elif isinstance(p, dict):
                dev = p.get("device") or p.get("port") or p.get("name") or ""
                entry = _jsonable(p)
                entry.update({"device": str(dev), "description": str(p.get("description") or "")})
                ports.append(entry)
            elif isinstance(p, (list, tuple)) and p:
                ports.append({"device": str(p[0]), "description": str(p[1]) if len(p) > 1 else ""})
            else:
                ports.append({"device": str(getattr(p, "device", p)),
                              "description": str(getattr(p, "description", ""))})
        return {"ports": ports}

    # -- controller discovery ------------------------------------------------
    @app.post("/api/discover")
    async def api_discover(body: DiscoverBody | None = None):
        scan = (body or DiscoverBody()).scan
        if scan not in (True, False, "auto"):
            raise HTTPException(422, 'scan must be true, false or "auto"')
        if discover is not None:
            run = discover
        else:
            from ..discovery import discover as run
        try:
            devices = await run(scan=scan)
        except Exception as exc:
            log.warning("discovery failed: %s", exc)
            raise HTTPException(502, str(exc)) from exc
        return {"devices": [d.to_dict() for d in devices]}

    # -- LEAP pairing ------------------------------------------------------
    @app.post("/api/pair/leap")
    async def api_pair_leap(body: PairBody):
        host = body.host.strip()
        try:
            from ..drivers.leap import pair
            from ..drivers.leap_pairing import UntrustedControllerCert
        except ImportError as exc:
            raise HTTPException(501, f"LEAP support unavailable: {exc}") from exc
        safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        out_dir = data_dir() / "leap" / safe
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await pair(host, out_dir, trust_presented_cert=body.trust_presented_cert)
        except UntrustedControllerCert as exc:
            log.warning("LEAP pairing certificate not trusted: %s", exc)
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            log.exception("LEAP pairing failed")
            raise HTTPException(502, f"Pairing failed: {exc}") from exc
        return {"ok": True, "host": host, "cert_dir": str(out_dir), "result": _jsonable(result)}

    # -- updates -----------------------------------------------------------
    def need_updater() -> Updater:
        if updater is None:
            raise HTTPException(404, "Updates are not available in this mode.")
        return updater

    def masked_updates() -> dict:
        u = controller.config.updates
        return {"check": u.check, "token": PASSWORD_PLACEHOLDER if u.token else ""}

    @app.get("/api/update")
    async def api_update():
        need_updater()
        return update_state()

    @app.post("/api/update/check")
    async def api_update_check():
        u = need_updater()
        await u.check()
        return update_state()

    @app.post("/api/update/dismiss")
    async def api_update_dismiss(body: UpdateDismissBody):
        need_updater().dismiss(body.version)
        return update_state()

    @app.post("/api/update/apply")
    async def api_update_apply(body: UpdateApplyBody):
        u = need_updater()
        try:
            res = await u.apply(body.version, update_warnings(), body.confirm)
        except UpdateError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**res, "update": update_state()}

    @app.get("/api/settings/updates")
    async def api_updates_get():
        need_updater()
        return masked_updates()

    @app.put("/api/settings/updates")
    async def api_updates_put(body: UpdateSettingsBody):
        u = need_updater()
        cfg = controller.config.updates
        was_off, old_token = not cfg.check, cfg.token
        cfg.check = body.check
        if body.token != PASSWORD_PLACEHOLDER:
            cfg.token = body.token.strip()
        controller.config.save()
        if (cfg.check and was_off) or cfg.token != old_token:
            await u.check()
        return {**masked_updates(), "update": update_state()}

    # -- WebSocket ---------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        hub.ensure_listener()
        hub.clients.add(q)

        async def reader():
            while True:
                await ws.receive_text()  # we ignore client messages (pings)

        async def writer():
            loop = asyncio.get_running_loop()
            await ws.send_json({"type": "status", **(await status_payload())})
            next_status = loop.time() + STATUS_INTERVAL
            while True:
                # Status goes out on a timer: level changes arriving all the time
                # (a DMX ramp) must not starve it.
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=max(0.0, next_status - loop.time()))
                except asyncio.TimeoutError:
                    msg = None
                if msg is None or loop.time() >= next_status:
                    hub.ensure_listener()  # pick up a driver swapped elsewhere
                    await ws.send_json({"type": "status", **(await status_payload())})
                    next_status = loop.time() + STATUS_INTERVAL
                if msg is not None:
                    await ws.send_json(msg)

        tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                    log.debug("websocket task ended: %r", exc)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
            hub.clients.discard(q)
            with contextlib.suppress(Exception):
                await ws.close()

    # -- static SPA --------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app
