import asyncio

import pytest

from lutron_dmx.config import DriverConfig
from lutron_dmx.drivers import DriverError, create_driver
from lutron_dmx.drivers.simulator import SimulatorDriver
from lutron_dmx.models import ZoneType


@pytest.fixture
async def sim():
    d = SimulatorDriver(DriverConfig(kind="simulator"), fade_step=0.01)
    await d.connect()
    yield d
    await d.close()


def test_create_driver_returns_simulator():
    d = create_driver(DriverConfig(kind="simulator"))
    assert isinstance(d, SimulatorDriver)
    assert d.system_name == "Simulator"


async def test_inventory_seeded_from_real_names(sim):
    inv = sim.inventory()
    names = {a.name for a in inv.areas}
    assert {"Utility Room", "Exterior", "Main Stairs", "Back Porch",
            "Living Room", "Office", "Sitting Room"} <= names
    living = next(a for a in inv.areas if a.name == "Living Room")
    living_zones = [z.name for z in inv.zones if z.area_id == living.id]
    assert len(living_zones) == 7 and "Living Fan Light" in living_zones
    for empty in ("Utility Room", "Back Porch", "Office"):
        aid = next(a.id for a in inv.areas if a.name == empty)
        assert not [z for z in inv.zones if z.area_id == aid]
    assert {k.name for k in inv.keypads} == {"Main Stairs Keypad", "Living Room Keypad", "Sitting Room Keypad"}
    # all zones on except Sitting Room Lights
    for z in inv.zones:
        if z.name == "Sitting Room Lights":
            assert z.level == 0
        else:
            assert z.level >= 75
    # ids look like RA2 integration ids
    assert all(z.id.isdigit() for z in inv.zones)
    assert len(inv.schedules) == 3
    assert sum(not s.enabled for s in inv.schedules) == 1


async def test_instances_do_not_share_state():
    a, b = SimulatorDriver(), SimulatorDriver()
    zid = a.zones()[0].id
    await a.set_level(zid, 12)
    assert a.zone(zid).level == 12
    assert b.zone(zid).level != 12
    await a.set_schedule_enabled(a.schedules()[0].id, False)
    assert b.schedules()[0].enabled is True


async def test_set_level_immediate_notifies(sim):
    seen = []
    sim.add_listener(lambda zid, lvl: seen.append((zid, lvl)))
    await sim.set_level("40", 33)
    assert seen == [("40", 33.0)]
    assert sim.zone("40").level == 33


async def test_async_listener_and_clamp(sim):
    seen = []

    async def cb(zid, lvl):
        seen.append(lvl)

    sim.add_listener(cb)
    await sim.set_level("40", 150)
    assert seen == [100.0]


async def test_switch_snaps(sim):
    z = next(z for z in sim.zones() if z.type is ZoneType.SWITCH)
    await sim.set_level(z.id, 30)
    assert z.level == 100
    await sim.set_level(z.id, 0)
    assert z.level == 0


async def test_fade_steps_levels(sim):
    seen = []
    sim.add_listener(lambda zid, lvl: seen.append(lvl))
    await sim.set_level("50", 100, fade=0.2)
    await sim.wait_fades()
    assert sim.zone("50").level == 100
    assert len(seen) >= 3
    assert seen == sorted(seen)
    assert 0 < seen[0] < 100


async def test_new_command_cancels_fade(sim):
    await sim.set_level("50", 100, fade=5)
    await asyncio.sleep(0.05)
    await sim.set_level("50", 10)
    await asyncio.sleep(0.05)
    assert sim.zone("50").level == 10
    assert not sim._fades


async def test_set_levels_and_unknown_zone(sim):
    await sim.set_levels({"40": 1, "41": 2})
    assert (sim.zone("40").level, sim.zone("41").level) == (1, 2)
    with pytest.raises(DriverError):
        await sim.set_levels({"40": 50, "nope": 1})
    assert sim.zone("40").level == 1  # validated before applying


async def test_schedule_toggle(sim):
    s = sim.schedules()[0]
    await sim.set_schedule_enabled(s.id, False)
    assert s.enabled is False
    with pytest.raises(DriverError):
        await sim.set_schedule_enabled("missing", True)


async def test_backup(sim):
    b = await sim.backup()
    assert b["system"] == "Simulator"
    assert len(b["inventory"]["zones"]) == len(sim.zones())
    assert b["levels"]["50"] == 0
    import json
    json.dumps(b)


async def test_close_disconnects(sim):
    assert sim.connected
    await sim.close()
    assert not sim.connected
