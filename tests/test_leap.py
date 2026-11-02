"""LEAP driver unit tests - only pure helpers and offline paths (no hardware)."""

from types import SimpleNamespace

import pytest

from lutron_to_dmx_bridge.config import DriverConfig
from lutron_to_dmx_bridge.drivers import DriverError, create_driver
from lutron_to_dmx_bridge.drivers import leap
from lutron_to_dmx_bridge.drivers.leap import (LeapDriver, build_inventory, cert_paths, device_level,
                                     fan_speed_to_level, leap_type_to_zone_type,
                                     level_to_fan_speed, parse_timeclock_events)
from lutron_to_dmx_bridge.models import ZoneType


@pytest.mark.parametrize("t,zt", [
    ("WallDimmer", ZoneType.DIMMER), ("Dimmed", ZoneType.DIMMER),
    ("Switched", ZoneType.SWITCH), ("WallSwitch", ZoneType.SWITCH),
    ("Shade", ZoneType.SHADE), ("SerenaRollerShade", ZoneType.SHADE),
    ("FanSpeed", ZoneType.FAN), ("CasetaFanSpeedController", ZoneType.FAN),
    ("KeypadLED", None), ("RadioRa3Processor", None), ("SmartBridge", None), (None, None),
    ("SomeFutureZoneType", ZoneType.DIMMER),
])
def test_type_mapping(t, zt):
    assert leap_type_to_zone_type(t) is zt


def test_fan_mapping():
    assert level_to_fan_speed(0) == "Off"
    assert level_to_fan_speed(1) == "Low"
    assert level_to_fan_speed(50) == "Medium"
    assert level_to_fan_speed(70) == "MediumHigh"
    assert level_to_fan_speed(100) == "High"
    assert fan_speed_to_level("MediumHigh") == 75
    assert fan_speed_to_level(None) == 0


def test_device_level():
    assert device_level({"type": "Dimmed", "current_state": 42}) == 42
    assert device_level({"type": "Dimmed", "current_state": -1}) == 0
    assert device_level({"type": "FanSpeed", "current_state": -1, "fan_speed": "High"}) == 100


def test_build_inventory_ra3_shape():
    areas = {
        "1": {"id": "1", "name": "Home", "parent_id": None},
        "2": {"id": "2", "name": "Living Room", "parent_id": "1"},
        "3": {"id": "3", "name": "Office", "parent_id": "1"},
    }
    devices = {
        "1": {"device_id": "1", "zone": "1", "type": "RadioRa3Processor", "area": "1",
              "current_state": -1, "name": "Home Processor"},
        "101": {"device_id": "101", "zone": "101", "type": "Dimmed", "area": "2",
                "current_state": 80, "name": "Living Room_Living East Recessed",
                "device_name": "Living East Recessed", "button_groups": None},
        "102": {"device_id": "102", "zone": "102", "type": "FanSpeed", "area": "2",
                "current_state": -1, "fan_speed": "Medium", "device_name": "Fan"},
        "200": {"device_id": "200", "zone": None, "type": "SunnataKeypad", "area": "2",
                "button_groups": ["300"], "device_name": "Living Room Keypad"},
        "201": {"device_id": "201", "zone": None, "type": "KeypadLED", "button_groups": None},
    }
    buttons = {
        "b2": {"parent_device": "200", "button_number": 2, "button_name": "Relax"},
        "b1": {"parent_device": "200", "button_number": 1, "button_name": "Bright"},
    }
    inv = build_inventory(devices, areas, buttons)
    assert {z.id for z in inv.zones} == {"101", "102"}
    east = next(z for z in inv.zones if z.id == "101")
    assert east.name == "Living East Recessed" and east.level == 80 and east.area_id == "2"
    assert next(z for z in inv.zones if z.id == "102").level == 50
    assert inv.keypads[0].buttons == ["Bright", "Relax"]
    # root container area skipped, empty leaf kept
    assert [a.name for a in inv.areas] == ["Living Room", "Office"]


def test_parse_timeclock_events():
    events = [
        {"href": "/timeclockevent/11", "Name": "Sunset Exterior On",
         "Parent": {"href": "/timeclock/5"}, "EnabledState": "Enabled"},
        {"href": "/timeclockevent/12", "Name": "Midnight All Off",
         "Parent": {"href": "/timeclock/5"}, "EnabledState": "Disabled"},
    ]
    out = parse_timeclock_events(events, [{"href": "/timeclock/5", "Name": "Home"}])
    assert [(s.id, s.enabled, s.timeclock_id) for s in out] == [("11", True, "5"), ("12", False, "5")]
    assert out[0].description == "Home"


def test_cert_paths(tmp_path):
    k, c, ca = cert_paths(tmp_path)
    assert (k.name, c.name, ca.name) == ("caseta.key", "caseta.crt", "caseta-bridge.crt")


def test_create_driver_leap():
    assert isinstance(create_driver(DriverConfig(kind="leap")), LeapDriver)


async def test_connect_requires_pairing(tmp_path):
    d = LeapDriver(DriverConfig(kind="leap", host="192.0.2.1", leap_cert_dir=str(tmp_path)))
    with pytest.raises(DriverError, match="paired"):
        await d.connect()
    assert not d.connected


async def test_schedules_unsupported_raises(tmp_path):
    d = LeapDriver(DriverConfig(kind="leap", host="x", leap_cert_dir=str(tmp_path)))
    assert d.schedules() == []
    with pytest.raises(DriverError, match="not supported"):
        await d.set_schedule_enabled("1", True)


async def test_pair_writes_files(tmp_path, monkeypatch):
    async def fake_pair(host, **kw):
        return {"key": "KEY", "cert": "CERT", "ca": "CA", "version": "1.0"}

    monkeypatch.setattr(leap, "async_pair", fake_pair)
    res = await leap.pair("192.0.2.1", tmp_path / "certs")
    k, c, ca = cert_paths(tmp_path / "certs")
    assert (k.read_text(), c.read_text(), ca.read_text()) == ("KEY", "CERT", "CA")
    assert res["keyfile"] == str(k) and res["version"] == "1.0"


class FakeBridge:
    """Minimal stand-in for Smartbridge to exercise set_level dispatch."""

    def __init__(self):
        self.devices = {
            "101": {"zone": "101", "type": "Dimmed", "current_state": 0, "area": "2"},
            "102": {"zone": "102", "type": "FanSpeed", "current_state": -1, "fan_speed": "Off"},
        }
        self.calls = []
        self.areas, self.buttons, self.scenes, self.occupancy_groups = {}, {}, {}, {}

    async def set_value(self, device_id, value=None, fade_time=None):
        self.calls.append(("value", device_id, value, fade_time))

    async def set_fan(self, device_id, value):
        self.calls.append(("fan", device_id, value))

    def is_connected(self):
        return True

    async def close(self):
        pass


async def test_set_level_dispatch():
    d = LeapDriver(DriverConfig(kind="leap"))
    bridge = FakeBridge()
    d._bridge = bridge
    d._inventory = build_inventory(bridge.devices, {"2": {"name": "A"}}, {})
    await d.set_levels({"101": 33.6, "102": 70}, fade=2)
    assert ("fan", "102", "MediumHigh") in bridge.calls
    val = next(c for c in bridge.calls if c[0] == "value")
    assert val[1:3] == ("101", 34) and val[3].total_seconds() == 2
    with pytest.raises(DriverError):
        await d.set_level("999", 1)
    b = await d.backup()
    assert b["raw"]["devices"]["101"]["type"] == "Dimmed"
