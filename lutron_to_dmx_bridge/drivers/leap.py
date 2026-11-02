"""LEAP driver for RadioRA 3, RA2 Select and Caseta, via ``pylutron_caseta``.

LEAP is Lutron's TLS/JSON protocol (port 8081). The client authenticates with
a certificate obtained once by *pairing* (pressing the button on the bridge /
processor); see :func:`pair`. The three PEM files are stored in
``DriverConfig.leap_cert_dir`` under fixed names.

Written against pylutron_caseta 0.29. Zone control and status use the
library's public API (``set_value``, ``set_fan``, ``add_subscriber``).
Schedules (timeclock events) are NOT exposed by pylutron_caseta; they are read
best-effort through its private ``_request`` method from ``/timeclock`` and
``/timeclockevent``. Those LEAP endpoints and the ``EnabledState`` field are
based on community reverse-engineering of RA3 and are UNVERIFIED on hardware.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from pylutron_caseta import _LEAP_DEVICE_TYPES, FAN_HIGH, FAN_LOW, FAN_MEDIUM, \
    FAN_MEDIUM_HIGH, FAN_OFF
from pylutron_caseta.smartbridge import Smartbridge

from ..models import Area, Keypad, Schedule, SystemInventory, Zone, ZoneType
from .base import DriverError, LutronDriver
from .leap_pairing import UntrustedControllerCert, async_pair

log = logging.getLogger(__name__)

KEY_FILE = "caseta.key"
CERT_FILE = "caseta.crt"
CA_FILE = "caseta-bridge.crt"

CONNECT_TIMEOUT = 30.0

# -- pure mapping helpers (unit-tested) -------------------------------------
_TYPE_MAP: dict[str, ZoneType] = {}
for _t in _LEAP_DEVICE_TYPES.get("light", []):
    _TYPE_MAP[_t] = ZoneType.DIMMER
for _t in _LEAP_DEVICE_TYPES.get("switch", []):
    _TYPE_MAP[_t] = ZoneType.SWITCH
for _t in _LEAP_DEVICE_TYPES.get("cover", []):
    _TYPE_MAP[_t] = ZoneType.SHADE
for _t in _LEAP_DEVICE_TYPES.get("fan", []):
    _TYPE_MAP[_t] = ZoneType.FAN
# Keypad LEDs are modelled as "switch" devices by pylutron_caseta; not loads.
_TYPE_MAP.pop("KeypadLED", None)

# Fan speeds in ascending order, with the level each represents.
_FAN_SPEEDS: list[tuple[str, float]] = [
    (FAN_OFF, 0.0), (FAN_LOW, 25.0), (FAN_MEDIUM, 50.0),
    (FAN_MEDIUM_HIGH, 75.0), (FAN_HIGH, 100.0),
]


def leap_type_to_zone_type(device_type: str | None) -> ZoneType | None:
    """Map a pylutron_caseta device ``type`` to a ZoneType, or None if not a load."""
    if not device_type or device_type == "KeypadLED":
        return None
    if device_type in _TYPE_MAP:
        return _TYPE_MAP[device_type]
    low = device_type.lower()
    if any(w in low for w in ("processor", "bridge", "repeater")):
        return None
    if "shade" in low or "blind" in low or "drape" in low:
        return ZoneType.SHADE
    if "fan" in low:
        return ZoneType.FAN
    if "switch" in low:
        return ZoneType.SWITCH
    return ZoneType.DIMMER  # unknown zone types accept GoToLevel


def level_to_fan_speed(level: float) -> str:
    """Nearest fan speed name for a 0-100 level (>0 is never Off)."""
    if level <= 0:
        return FAN_OFF
    return min(_FAN_SPEEDS[1:], key=lambda s: abs(s[1] - level))[0]


def fan_speed_to_level(speed: str | None) -> float:
    return dict(_FAN_SPEEDS).get(speed or FAN_OFF, 0.0)


def device_level(device: dict) -> float:
    """Current 0-100 level of a pylutron_caseta device dict (unknown -> 0)."""
    if leap_type_to_zone_type(device.get("type")) is ZoneType.FAN:
        return fan_speed_to_level(device.get("fan_speed"))
    lvl = device.get("current_state", -1)
    try:
        lvl = float(lvl)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, lvl))


def cert_paths(cert_dir: str | Path) -> tuple[Path, Path, Path]:
    """(keyfile, certfile, ca_certs) inside the configured cert directory."""
    d = Path(cert_dir).expanduser()
    return d / KEY_FILE, d / CERT_FILE, d / CA_FILE


def id_from_href(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


def build_inventory(devices: dict[str, dict], areas: dict[str, dict],
                    buttons: dict[str, dict]) -> SystemInventory:
    """Build a SystemInventory from pylutron_caseta's devices/areas/buttons dicts."""
    inv = SystemInventory()
    used_areas: set[str] = set()

    for dev_id, dev in devices.items():
        if dev.get("zone") is not None:
            ztype = leap_type_to_zone_type(dev.get("type"))
            if ztype is None:
                continue
            area = dev.get("area")
            inv.zones.append(Zone(
                id=str(dev_id),
                name=dev.get("device_name") or dev.get("name") or f"Zone {dev_id}",
                area_id=str(area) if area is not None else None,
                type=ztype,
                level=device_level(dev),
            ))
            if area is not None:
                used_areas.add(str(area))
        elif dev.get("button_groups"):
            # Keypad / Pico: read-only, list button names in button order.
            btns = sorted(
                (b for b in buttons.values() if str(b.get("parent_device")) == str(dev_id)),
                key=lambda b: b.get("button_number") or 0,
            )
            labels = [b.get("button_name") or f"Button {b.get('button_number')}" for b in btns]
            area = dev.get("area")
            inv.keypads.append(Keypad(
                id=str(dev_id),
                name=dev.get("device_name") or dev.get("name") or f"Keypad {dev_id}",
                area_id=str(area) if area is not None else None,
                buttons=labels,
            ))
            if area is not None:
                used_areas.add(str(area))

    parents = {str(a.get("parent_id")) for a in areas.values() if a.get("parent_id")}
    for aid, a in areas.items():
        aid = str(aid)
        # Skip pure container areas (the root / floors) that hold nothing directly.
        if aid in used_areas or aid not in parents:
            inv.areas.append(Area(id=aid, name=a.get("name", aid)))
    return inv


def parse_timeclock_events(events: list[dict], timeclocks: list[dict] | None = None
                           ) -> list[Schedule]:
    """Map LEAP ``TimeclockEvents`` JSON to Schedules (field names unverified)."""
    tc_names = {id_from_href(t["href"]): t.get("Name", "") for t in (timeclocks or [])
                if "href" in t}
    out: list[Schedule] = []
    for ev in events:
        if "href" not in ev:
            continue
        parent = (ev.get("Parent") or ev.get("Timeclock") or {}).get("href")
        tc_id = id_from_href(parent) if parent else None
        state = ev.get("EnabledState", ev.get("IsEnabled", True))
        enabled = state if isinstance(state, bool) else str(state).lower() == "enabled"
        out.append(Schedule(
            id=id_from_href(ev["href"]),
            name=ev.get("Name", id_from_href(ev["href"])),
            enabled=enabled,
            description=tc_names.get(tc_id or "", ""),
            timeclock_id=tc_id,
        ))
    return out


def _json_safe(obj: Any) -> Any:
    """Round-trip through JSON, stringifying anything exotic (ColorMode etc.)."""
    return json.loads(json.dumps(obj, default=str))


# -- pairing ----------------------------------------------------------------
async def pair(host: str, out_dir: str | Path, trust_presented_cert: bool = False) -> dict:
    """Pair with a LEAP bridge/processor and write the certificates to `out_dir`.

    The user must press the pairing button on the bridge (or, on RA3, the
    processor) shortly before / during this call. Returns the written paths.

    Raises :class:`UntrustedControllerCert` (a DriverError) when the controller's pairing
    certificate matches no Lutron CA; retry with ``trust_presented_cert=True`` only once the
    user has confirmed it is their controller.
    """
    try:
        data = await async_pair(host, trust_presented_cert=trust_presented_cert)
    except UntrustedControllerCert:
        raise
    except DriverError as exc:
        raise DriverError(f"Pairing with {host} failed: {exc}") from exc
    except Exception as exc:
        raise DriverError(f"Pairing with {host} failed: {exc or type(exc).__name__}") from exc
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    key, cert, ca = cert_paths(out)
    key.write_text(data["key"], encoding="ascii")
    cert.write_text(data["cert"], encoding="ascii")
    ca.write_text(data["ca"], encoding="ascii")
    return {
        "cert_dir": str(out),
        "keyfile": str(key),
        "certfile": str(cert),
        "ca_certs": str(ca),
        "version": data.get("version"),
    }


# -- driver ----------------------------------------------------------------
class LeapDriver(LutronDriver):
    system_name = "Lutron RA3 / RA2 Select"

    def __init__(self, config: Any) -> None:
        super().__init__()
        self.config = config
        self._bridge: Smartbridge | None = None
        self._inventory = SystemInventory()
        self._raw_timeclocks: dict[str, Any] = {}
        self._schedules_supported = False
        self._pending: set[asyncio.Task[None]] = set()

    # -- lifecycle -------------------------------------------------------
    def _cert_dir(self) -> Path:
        if self.config.leap_cert_dir:
            return Path(self.config.leap_cert_dir).expanduser()
        from ..config import data_dir

        return data_dir() / "leap"

    async def connect(self) -> None:
        if not self.config.host:
            raise DriverError("No LEAP bridge/processor host configured")
        key, cert, ca = cert_paths(self._cert_dir())
        missing = [p.name for p in (key, cert, ca) if not p.exists()]
        if missing:
            raise DriverError(f"Not paired yet - missing {', '.join(missing)} in {key.parent}")

        kwargs: dict[str, Any] = {}
        if self.config.port:
            kwargs["port"] = int(self.config.port)
        bridge = Smartbridge.create_tls(self.config.host, str(key), str(cert), str(ca), **kwargs)
        try:
            await asyncio.wait_for(bridge.connect(), CONNECT_TIMEOUT)
        except Exception as exc:
            await bridge.close()
            raise DriverError(f"LEAP connection to {self.config.host} failed: {exc}") from exc
        self._bridge = bridge
        self._inventory = build_inventory(bridge.devices, bridge.areas, bridge.buttons)

        # pylutron_caseta keeps device dicts updated and calls one subscriber
        # per device id; it reconnects on its own after drops.
        for z in self._inventory.zones:
            bridge.add_subscriber(z.id, lambda zid=z.id: self._on_device_update(zid))

        await self._load_schedules()
        self.metrics.mark_connected()

    async def close(self) -> None:
        bridge, self._bridge = self._bridge, None
        self.metrics.mark_disconnected()
        if bridge is not None:
            await bridge.close()
        for t in list(self._pending):
            t.cancel()

    @property
    def connected(self) -> bool:
        return self._bridge is not None and self._bridge.is_connected()

    def inventory(self) -> SystemInventory:
        return self._inventory

    def _require_bridge(self) -> Smartbridge:
        if self._bridge is None:
            raise DriverError("Not connected to the Lutron bridge/processor")
        return self._bridge

    def _on_device_update(self, zone_id: str) -> None:
        # Called synchronously from pylutron_caseta's monitor task.
        if self._bridge is None:
            return
        dev = self._bridge.devices.get(zone_id)
        if dev is None:
            return
        task = asyncio.get_running_loop().create_task(self._notify(zone_id, device_level(dev)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # -- control ---------------------------------------------------------
    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        bridge = self._require_bridge()
        dev = bridge.devices.get(zone_id)
        zone = self.zone(zone_id)
        if dev is None or zone is None:
            raise DriverError(f"Unknown zone {zone_id!r}")
        level = max(0.0, min(100.0, float(level)))
        self.metrics.commands.add()
        try:
            if zone.type is ZoneType.FAN:
                await bridge.set_fan(zone_id, level_to_fan_speed(level))
            else:
                fade_td = timedelta(seconds=fade) if fade and fade > 0 else None
                # LEAP levels are integers 0-100.
                await bridge.set_value(zone_id, value=int(round(level)), fade_time=fade_td)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"LEAP set level failed for {zone.name}: {exc}") from exc

    async def set_levels(self, levels: dict[str, float], fade: float | None = None) -> None:
        # LEAP requests are tagged and pipelined, so gather sends them together.
        await asyncio.gather(*(self.set_level(z, lvl, fade) for z, lvl in levels.items()))

    async def refresh_levels(self) -> None:
        bridge = self._require_bridge()
        for z in list(self._inventory.zones):
            dev = bridge.devices.get(z.id) or {}
            zone_href = dev.get("zone")
            if zone_href is None:
                continue
            try:
                resp = await bridge._request("ReadRequest", f"/zone/{zone_href}/status")
            except Exception as exc:
                log.warning("LEAP status read failed for %s: %s", z.name, exc)
                continue
            status = (resp.Body or {}).get("ZoneStatus", {})
            if z.type is ZoneType.FAN:
                lvl = fan_speed_to_level(status.get("FanSpeed"))
            else:
                raw = status.get("Level", -1)
                if raw is None or raw < 0:
                    continue
                lvl = float(raw)
            await self._notify(z.id, lvl)

    # -- schedules (best effort, unverified) -----------------------------
    async def _raw_request(self, communique: str, url: str, body: dict | None = None) -> Any:
        bridge = self._require_bridge()
        request = getattr(bridge, "_request", None)
        if request is None:
            raise DriverError("Schedules are not supported by this firmware yet")
        return await request(communique, url, body)

    async def _load_schedules(self) -> None:
        self._schedules_supported = False
        self._inventory.schedules = []
        try:
            ev_resp = await self._raw_request("ReadRequest", "/timeclockevent")
        except Exception as exc:
            log.info("LEAP timeclock events unavailable: %s", exc)
            return
        timeclocks: list[dict] = []
        try:
            tc_resp = await self._raw_request("ReadRequest", "/timeclock")
            timeclocks = (tc_resp.Body or {}).get("Timeclocks", [])
        except Exception:
            pass
        events = (ev_resp.Body or {}).get("TimeclockEvents", [])
        self._raw_timeclocks = {"timeclocks": timeclocks, "events": events}
        self._inventory.schedules = parse_timeclock_events(events, timeclocks)
        self._schedules_supported = True

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        if not self._schedules_supported:
            raise DriverError("Schedules are not supported by this firmware yet")
        sched = next((s for s in self._inventory.schedules if s.id == schedule_id), None)
        if sched is None:
            raise DriverError(f"Unknown schedule {schedule_id!r}")
        # UNVERIFIED: request shape guessed from the LEAP read-model.
        body = {"TimeclockEvent": {"EnabledState": "Enabled" if enabled else "Disabled"}}
        try:
            await self._raw_request("UpdateRequest", f"/timeclockevent/{schedule_id}", body)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"Could not change schedule (unverified LEAP API): {exc}") from exc
        sched.enabled = bool(enabled)

    # -- backup ----------------------------------------------------------
    async def backup(self) -> dict:
        """Inventory plus the raw LEAP dumps. Not a restorable project backup
        (RA3 projects live in Lutron Designer)."""
        bridge = self._bridge
        raw: dict[str, Any] = {}
        if bridge is not None:
            raw = {
                "devices": bridge.devices,
                "areas": bridge.areas,
                "buttons": bridge.buttons,
                "scenes": bridge.scenes,
                "occupancy_groups": bridge.occupancy_groups,
            }
        raw["timeclocks"] = self._raw_timeclocks
        return {
            "system": self.system_name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": self.config.host,
            "inventory": self._inventory.to_dict(),
            "levels": {z.id: z.level for z in self._inventory.zones},
            "raw": _json_safe(raw),
        }
