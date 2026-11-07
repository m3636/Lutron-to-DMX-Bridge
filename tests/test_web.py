"""Tests for the web API, using an in-test fake driver (no hardware, no simulator)."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from lutron_to_dmx_bridge.config import AppConfig, DriverConfig
from lutron_to_dmx_bridge.drivers.base import LutronDriver
from lutron_to_dmx_bridge.models import Area, Keypad, Schedule, SystemInventory, Zone, ZoneType


class FakeDriver(LutronDriver):
    system_name = "Fake System"

    def __init__(self, config: DriverConfig | None = None) -> None:
        super().__init__()
        self.config = config
        self._connected = False
        self._inv = SystemInventory(
            areas=[Area("a1", "Kitchen"), Area("a2", "Living <Room>")],
            zones=[
                Zone("z1", "Pendants", "a1", ZoneType.DIMMER, 0.0),
                Zone("z2", "Cans", "a1", ZoneType.DIMMER, 50.0),
                Zone("z3", "Lamp", "a2", ZoneType.SWITCH, 0.0),
            ],
            keypads=[Keypad("k1", "Entry", "a1", ["Bright", "Off"])],
            schedules=[Schedule("s1", "Sunset", True, "At sunset")],
        )
        self.calls: list[tuple] = []

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def inventory(self) -> SystemInventory:
        return self._inv

    async def set_level(self, zone_id, level, fade=None):
        self.calls.append(("set_level", zone_id, level, fade))
        await self._notify(zone_id, level)

    async def refresh_levels(self) -> None:
        pass

    async def set_schedule_enabled(self, schedule_id, enabled):
        for s in self._inv.schedules:
            if s.id == schedule_id:
                s.enabled = enabled

    async def backup(self) -> dict:
        return {"inventory": self._inv.to_dict()}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def ctx(home, monkeypatch):
    from lutron_to_dmx_bridge import controller as controller_mod
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.web.app import create_app

    # reconfigure_driver builds drivers through create_driver; keep it in-test.
    monkeypatch.setattr(controller_mod, "create_driver", lambda cfg: FakeDriver(cfg))

    cfg = AppConfig(driver=DriverConfig(kind="ra2", host="10.0.0.5", password="secret"))
    driver = FakeDriver(cfg.driver)
    ctl = Controller(cfg, driver=driver)
    app = create_app(ctl)
    with TestClient(app) as client:
        client.portal.call(driver.connect)  # the CLI normally starts the controller
        yield client, ctl, driver


def test_index_and_static(ctx):
    client, _, _ = ctx
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/logo.png").headers["content-type"] == "image/png"
    assert "static/logo.png" in client.get("/").text
    for chip in ("chip-lutron", "chip-dmx", "chip-out", "chip-alerts", "chip-server", "tab-status"):
        assert f'id="{chip}"' in r.text
    assert "pill-ws" not in r.text  # the old "Live" pill is gone
    assert "statusModel" in client.get("/static/app.js").text


def test_page_assets_are_cache_busted_and_revalidated(ctx):
    """An update must never run a cached old app.js against the new page (blank status chips in 0.3.0)."""
    import hashlib
    import re

    from lutron_to_dmx_bridge.web.app import STATIC_DIR

    client, _, _ = ctx
    page = client.get("/").text
    for name in ("app.js", "style.css"):
        m = re.search(rf'"static/{re.escape(name)}\?v=([0-9a-f]+)"', page)
        assert m, name
        assert m.group(1) == hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
        r = client.get(f"static/{name}?v={m.group(1)}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_devices_page_marks_dmx_controlled_zones(ctx):
    """The Devices page styles zones from GET /api/settings/dmx, so the frontend assets and that payload must agree."""
    client, _, _ = ctx
    assert "DMX Controlled" in client.get("/static/app.js").text
    assert ".zone.dmx" in client.get("/static/style.css").text
    maps = [{"zone_id": "z1", "address": 1, "params": ["intensity"], "enabled": True},
            {"zone_id": "z2", "address": 2, "params": ["intensity"], "enabled": False}]
    body = {"enabled": False, "source": "artnet", "universe": 0, "bind_ip": "0.0.0.0", "serial_port": "",
            "max_rate_hz": 4, "mappings": maps}
    assert client.put("/api/settings/dmx", json=body).status_code == 200
    got = client.get("/api/settings/dmx").json()["mappings"]
    assert [(m["zone_id"], m["enabled"]) for m in got] == [("z1", True), ("z2", False)]


def test_status(ctx):
    client, _, _ = ctx
    st = client.get("/api/status").json()
    assert st["system_name"] == "Fake System"
    assert st["connected"] is True
    assert st["driver"] == {"kind": "ra2", "host": "10.0.0.5"}
    assert st["dmx"]["enabled"] is False
    assert st["app"]["version"] and st["app"]["uptime_s"] >= 0 and st["app"]["clients"] == 0
    assert set(st["lutron"]) >= {"connected_for_s", "commands_per_s", "latency", "inflight", "reconnect_attempts"}
    assert set(st["alerts"]) == {"warnings", "errors", "recent"}


def test_alerts_are_counted_and_can_be_cleared(ctx):
    client, _, _ = ctx
    client.post("/api/alerts/clear")
    logging.getLogger("lutron_to_dmx_bridge.test_web").warning("repeater is slow")
    alerts = client.get("/api/status").json()["alerts"]
    assert alerts["warnings"] == 1 and alerts["recent"][0]["message"] == "repeater is slow"
    assert client.post("/api/alerts/clear").json() == {"ok": True}
    alerts = client.get("/api/status").json()["alerts"]
    assert alerts["warnings"] == 0 and alerts["recent"] == []


def test_inventory(ctx):
    client, _, _ = ctx
    inv = client.get("/api/inventory").json()
    assert [a["name"] for a in inv["areas"]] == ["Kitchen", "Living <Room>"]
    z2 = next(z for z in inv["zones"] if z["id"] == "z2")
    assert z2["level"] == 50.0 and z2["is_on"] is True and z2["type"] == "dimmer"
    assert inv["keypads"][0]["buttons"] == ["Bright", "Off"]
    assert inv["schedules"][0]["enabled"] is True


def test_zone_level_on_off_and_validation(ctx):
    client, _, driver = ctx
    r = client.post("/api/zones/z1/level", json={"level": 42.5, "fade": 2})
    assert r.status_code == 200
    assert driver.zone("z1").level == 42.5
    assert driver.calls[-1] == ("set_level", "z1", 42.5, 2)

    assert client.post("/api/zones/z1/off").status_code == 200
    assert driver.zone("z1").level == 0
    assert client.post("/api/zones/z1/on").status_code == 200
    assert driver.zone("z1").level == 100

    assert client.post("/api/zones/z1/level", json={"level": 101}).status_code == 422
    assert client.post("/api/zones/z1/level", json={"level": -1}).status_code == 422
    assert client.post("/api/zones/nope/level", json={"level": 10}).status_code == 404
    assert client.post("/api/zones/nope/on").status_code == 404


def test_area_level(ctx):
    client, _, driver = ctx
    r = client.post("/api/areas/a1/level", json={"level": 30})
    assert r.status_code == 200
    assert sorted(r.json()["zone_ids"]) == ["z1", "z2"]
    assert driver.zone("z1").level == 30 and driver.zone("z2").level == 30
    assert driver.zone("z3").level == 0
    assert client.post("/api/areas/zzz/level", json={"level": 30}).status_code == 404


def test_scene_crud_and_activate(ctx, home):
    client, ctl, driver = ctx
    r = client.post("/api/scenes", json={"name": "Evening", "levels": {"z1": 20, "z3": 100}, "fade": 1.5})
    assert r.status_code == 201
    scene = r.json()
    sid = scene["id"]
    assert scene["levels"] == {"z1": 20.0, "z3": 100.0}

    assert [s["id"] for s in client.get("/api/scenes").json()] == [sid]
    saved = json.loads((home / "config.json").read_text())
    assert saved["scenes"][0]["name"] == "Evening"

    r = client.put(f"/api/scenes/{sid}", json={"name": "Late", "levels": {"z2": 5}})
    assert r.status_code == 200 and r.json()["name"] == "Late"
    assert client.get(f"/api/scenes/{sid}").json()["levels"] == {"z2": 5.0}

    assert client.post(f"/api/scenes/{sid}/activate").status_code == 200
    assert driver.zone("z2").level == 5

    assert client.post("/api/scenes", json={"name": "Bad", "levels": {"z1": 150}}).status_code == 422
    assert client.put("/api/scenes/missing", json={"name": "x", "levels": {}}).status_code == 404
    assert client.post("/api/scenes/missing/activate").status_code == 404

    assert client.delete(f"/api/scenes/{sid}").status_code == 200
    assert client.delete(f"/api/scenes/{sid}").status_code == 404
    assert client.get("/api/scenes").json() == []


def test_scene_capture(ctx):
    client, _, driver = ctx
    client.post("/api/zones/z1/level", json={"level": 77})
    r = client.post("/api/scenes/capture", json={"name": "Now", "zone_ids": ["z1", "z2"]})
    assert r.status_code == 201
    assert r.json()["levels"] == {"z1": 77.0, "z2": 50.0}
    r = client.post("/api/scenes/capture", json={"name": "All"})
    assert set(r.json()["levels"]) == {"z1", "z2", "z3"}
    assert client.post("/api/scenes/capture", json={"name": "X", "zone_ids": ["nope"]}).status_code == 404


def test_schedule_toggle(ctx):
    client, _, driver = ctx
    r = client.post("/api/schedules/s1/enabled", json={"enabled": False})
    assert r.status_code == 200
    assert driver.schedules()[0].enabled is False
    assert client.get("/api/inventory").json()["schedules"][0]["enabled"] is False
    assert client.post("/api/schedules/nope/enabled", json={"enabled": True}).status_code == 404


def test_backup(ctx, home):
    client, _, _ = ctx
    r = client.get("/api/backup")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "lutron-backup-" in cd and cd.endswith('.json"')
    data = r.json()
    assert data["lutron"]["inventory"]["zones"][0]["id"] == "z1"
    assert data["app_config"]["driver"]["password"] == "********"
    saved = list((home / "backups").glob("lutron-backup-*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["lutron"] == data["lutron"]


def test_driver_settings_password_masking(ctx, home):
    client, ctl, _ = ctx
    d = client.get("/api/settings/driver").json()
    assert d["password"] == "********"
    assert "secret" not in json.dumps(d)

    # Placeholder sent back -> keep old password
    d["host"] = "10.0.0.9"
    r = client.put("/api/settings/driver", json=d)
    assert r.status_code == 200, r.text
    assert r.json()["password"] == "********"
    assert ctl.config.driver.password == "secret"
    assert ctl.config.driver.host == "10.0.0.9"
    assert isinstance(ctl.driver, FakeDriver) and ctl.driver.connected
    assert json.loads((home / "config.json").read_text())["driver"]["host"] == "10.0.0.9"

    # New password replaces it
    d["password"] = "newpw"
    client.put("/api/settings/driver", json=d)
    assert ctl.config.driver.password == "newpw"

    assert client.put("/api/settings/driver", json={**d, "kind": "bogus"}).status_code == 422
    assert client.put("/api/settings/driver", json={**d, "host": ""}).status_code == 422


def test_dmx_settings_and_helpers(ctx, home):
    client, ctl, _ = ctx
    body = {
        "enabled": False, "source": "sacn", "universe": 2, "bind_ip": "0.0.0.0", "serial_port": "",
        "max_rate_hz": 4, "mappings": [{"zone_id": "z1", "address": 10, "params": ["intensity", "on_off"]}],
    }
    r = client.put("/api/settings/dmx", json=body)
    assert r.status_code == 200, r.text
    assert ctl.config.dmx.source == "sacn" and ctl.config.dmx.mappings[0].address == 10
    assert client.get("/api/settings/dmx").json()["mappings"][0]["params"] == ["intensity", "on_off"]
    assert client.get("/api/settings/dmx").json()["max_commands_per_s"] == 20  # default
    r = client.put("/api/settings/dmx", json={**body, "max_commands_per_s": 8})
    assert r.status_code == 200 and ctl.config.dmx.max_commands_per_s == 8
    assert client.put("/api/settings/dmx", json={**body, "max_commands_per_s": -1}).status_code == 422

    mon = client.get("/api/dmx/monitor").json()
    assert mon["channel_map"]["10"] == {"zone_id": "z1", "param": "intensity"}
    assert mon["channel_map"]["11"]["param"] == "on_off"

    bad = {**body, "mappings": [{"zone_id": "z1", "address": 512, "params": ["intensity", "on_off"]}]}
    assert client.put("/api/settings/dmx", json=bad).status_code == 422
    bad = {**body, "mappings": [{"zone_id": "z1", "address": 1, "params": ["color"]}]}
    assert client.put("/api/settings/dmx", json=bad).status_code == 400
    assert client.put("/api/settings/dmx", json={**body, "source": "midi"}).status_code == 422

    r = client.post("/api/dmx/auto-assign", json={"zone_ids": ["z1", "z2"], "params": ["intensity"], "start": 5})
    assert r.status_code == 200
    maps = r.json()["mappings"]
    assert [(m["zone_id"], m["address"]) for m in maps] == [("z1", 5), ("z2", 6)]

    r = client.get("/api/dmx/serial-ports")
    assert r.status_code == 200 and isinstance(r.json()["ports"], list)


def test_websocket_level_push(ctx):
    client, ctl, driver = ctx
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["connected"] is True
        client.post("/api/zones/z2/level", json={"level": 64})
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "level":
                break
        assert msg == {"type": "level", "zone_id": "z2", "level": 64.0}


def test_websocket_status_is_not_starved_by_level_traffic(ctx, monkeypatch):
    from lutron_to_dmx_bridge.web import app as app_mod

    monkeypatch.setattr(app_mod, "STATUS_INTERVAL", 0.2)
    client, ctl, driver = ctx
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "status"
        stop = threading.Event()

        def flood():  # levels arrive far more often than the status interval
            while not stop.is_set():
                client.portal.call(driver._notify, "z2", 33.0)
                time.sleep(0.01)

        t = threading.Thread(target=flood)
        t.start()
        try:
            statuses = 0
            for _ in range(200):
                if ws.receive_json()["type"] == "status":
                    statuses += 1
                    if statuses == 2:
                        break
        finally:
            stop.set()
            t.join()
        assert statuses == 2


def test_websocket_follows_driver_swap(ctx):
    client, ctl, old_driver = ctx
    d = client.get("/api/settings/driver").json()
    client.put("/api/settings/driver", json=d)
    new_driver = ctl.driver
    assert new_driver is not old_driver
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        client.post("/api/zones/z1/level", json={"level": 12})
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "level":
                break
        assert msg["zone_id"] == "z1" and msg["level"] == 12.0
    assert not old_driver._listeners


def test_discover_endpoint_uses_injected_function(home, monkeypatch):
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.discovery.models import DiscoveredDevice
    from lutron_to_dmx_bridge.web.app import create_app

    seen = []

    async def fake_discover(*, scan):
        seen.append(scan)
        return [DiscoveredDevice(host="10.0.0.60", port=8081, kind="leap", method="mdns",
                                 system="RadioRA 3", name="lutron-abc", confidence=1.0)]

    cfg = AppConfig(driver=DriverConfig(kind="simulator"))
    with TestClient(create_app(Controller(cfg, driver=FakeDriver()), discover=fake_discover)) as client:
        r = client.post("/api/discover", json={"scan": True})
        assert r.status_code == 200
        dev = r.json()["devices"][0]
        assert dev["host"] == "10.0.0.60" and dev["name"] == "lutron-abc" and dev["kind"] == "leap"
        assert client.post("/api/discover").status_code == 200  # no body = auto
        assert seen == [True, "auto"]
        assert client.post("/api/discover", json={"scan": "sometimes"}).status_code == 422


def test_discover_endpoint_reports_failure(home):
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.web.app import create_app

    async def broken(*, scan):
        raise RuntimeError("Local Network access blocked")

    cfg = AppConfig(driver=DriverConfig(kind="simulator"))
    with TestClient(create_app(Controller(cfg, driver=FakeDriver()), discover=broken)) as client:
        r = client.post("/api/discover", json={})
        assert r.status_code == 502 and "blocked" in r.json()["detail"]


def test_driver_settings_keep_and_replace_hostname(ctx):
    client, ctl, _ = ctx
    body = {"kind": "leap", "host": "10.0.0.60", "hostname": "lutron-abc"}
    assert client.put("/api/settings/driver", json=body).status_code == 200
    assert ctl.config.driver.hostname == "lutron-abc"
    # same host, no hostname sent: remembered name is kept
    client.put("/api/settings/driver", json={"kind": "leap", "host": "10.0.0.60"})
    assert ctl.config.driver.hostname == "lutron-abc"
    # a different host without a name: stale name is dropped
    client.put("/api/settings/driver", json={"kind": "leap", "host": "10.0.0.99"})
    assert ctl.config.driver.hostname == ""


def test_pair_untrusted_cert_returns_409_then_opt_in_pairs(home, monkeypatch):
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.drivers import leap
    from lutron_to_dmx_bridge.drivers.leap_pairing import UntrustedControllerCert
    from lutron_to_dmx_bridge.web.app import create_app

    calls = []

    async def fake_pair(host, out_dir, trust_presented_cert=False):
        calls.append(trust_presented_cert)
        if not trust_presented_cert:
            raise UntrustedControllerCert(host, 8083, ["Lutron root CA: unable to get local issuer certificate"],
                                          "subject='X' issuer='Y'")
        return {"cert_dir": str(out_dir)}

    monkeypatch.setattr(leap, "pair", fake_pair)
    cfg = AppConfig(driver=DriverConfig(kind="simulator"))
    with TestClient(create_app(Controller(cfg, driver=FakeDriver()))) as client:
        r = client.post("/api/pair/leap", json={"host": "10.0.0.5"})
        assert r.status_code == 409 and "issuer='Y'" in r.json()["detail"]
        r = client.post("/api/pair/leap", json={"host": "10.0.0.5", "trust_presented_cert": True})
        assert r.status_code == 200 and r.json()["ok"]
    assert calls == [False, True]
