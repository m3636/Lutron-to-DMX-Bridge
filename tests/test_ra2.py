import asyncio
import time
import json

import pytest

from lutron_dmx.config import DriverConfig
from lutron_dmx.drivers import DriverError, create_driver
from lutron_dmx.drivers.ra2 import (RA2Driver, format_fade, format_level,
                                    output_type_to_zone_type, parse_db_xml)
from lutron_dmx.drivers.simulator import SAMPLE_DATA_DIR
from lutron_dmx.models import ZoneType

SAMPLE = SAMPLE_DATA_DIR / "ra2_sample_dbxml.xml"


# -- pure helpers ---------------------------------------------------------------
@pytest.mark.parametrize("secs,expected", [
    (0, "0"), (2, "2"), (2.0, "2"), (0.25, "0.25"), (1.5, "1.5"), (59, "59"),
    (60, "1:00"), (90, "1:30"), (125.4, "2:05"), (3600, "1:00:00"), (3725, "1:02:05"),
])
def test_format_fade(secs, expected):
    assert format_fade(secs) == expected


def test_format_level():
    assert format_level(75) == "75"
    assert format_level(33.333) == "33.33"
    assert format_level(0) == "0"
    assert format_level(120) == "100"


@pytest.mark.parametrize("t,zt", [
    ("INC", ZoneType.DIMMER), ("MLV", ZoneType.DIMMER), ("ELV", ZoneType.DIMMER),
    ("AUTO_DETECT", ZoneType.DIMMER), ("NON_DIM", ZoneType.SWITCH),
    ("NON_DIM_INC", ZoneType.SWITCH), ("RELAY_LIGHTING", ZoneType.SWITCH),
    ("SYSTEM_SHADE", ZoneType.SHADE), ("CEILING_FAN_TYPE", ZoneType.FAN),
    ("SOMETHING_NEW", ZoneType.DIMMER),
])
def test_output_type_mapping(t, zt):
    assert output_type_to_zone_type(t) is zt


def test_parse_db_xml_sample():
    inv = parse_db_xml(SAMPLE.read_text(encoding="utf-8"))
    area_names = [a.name for a in inv.areas]
    assert "Home" not in area_names  # pure container skipped
    assert "Utility Room" in area_names  # empty leaf kept
    assert [a.id for a in inv.areas if a.name == "Utility Room"] == ["2"]
    assert "114a Living Porch" in area_names  # nested area walked
    zones = {z.id: z for z in inv.zones}
    assert zones["30"].name == "Stair Pendants"
    assert zones["21"].type is ZoneType.SWITCH
    assert zones["47"].type is ZoneType.FAN
    assert zones["48"].type is ZoneType.SHADE
    porch = next(a for a in inv.areas if a.name == "114a Living Porch")
    assert zones["46"].area_id == porch.id
    stairs = next(a for a in inv.areas if a.name == "Main Stairs")
    assert zones["31"].area_id == stairs.id == "4"

    kps = {k.id: k for k in inv.keypads}
    assert set(kps) == {"60", "61", "62"}  # repeater + motion sensor excluded
    assert kps["60"].buttons == ["Stairs On", "Night"]
    assert kps["61"].buttons == ["Entertain", "Button 2"]  # empty engraving falls back
    assert kps["62"].name == "Living Pico"

    assert [s.id for s in inv.schedules] == ["70:1", "70:2", "70:3"]
    assert inv.schedules[2].name == "Morning Step Lights Off"
    assert all(s.timeclock_id == "70" for s in inv.schedules)


def test_parse_db_xml_bytes_with_bom():
    inv = parse_db_xml(b"\xef\xbb\xbf" + SAMPLE.read_bytes())
    assert len(inv.zones) == 12


# -- fake RA2 main repeater -------------------------------------------------
class FakeRepeater:
    def __init__(self, username="lutron", password="integration"):
        self.username, self.password = username, password
        self.commands: list[str] = []
        self.levels: dict[str, float] = {}
        self.timeclock: dict[tuple[str, str], int] = {}
        self.writers: list[asyncio.StreamWriter] = []
        self.logins = 0
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    def drop_clients(self):
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def wait_for(self, pred, timeout=2.0):
        async def loop():
            while not pred():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(loop(), timeout)

    async def handle(self, reader, writer):
        self.writers.append(writer)
        try:
            writer.write(b"login: ")
            user = (await reader.readline()).decode().strip()
            writer.write(b"password: ")
            pw = (await reader.readline()).decode().strip()
            if (user, pw) != (self.username, self.password):
                writer.write(b"bad login\r\nlogin: ")
                await writer.drain()
                await reader.readline()
                return
            self.logins += 1
            writer.write(b"\r\nGNET> ")
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                cmd = raw.decode().strip()
                self.commands.append(cmd)
                writer.write(self.reply(cmd).encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def reply(self, cmd: str) -> str:
        p = cmd.split(",")
        if p[0] == "#OUTPUT":
            self.levels[p[1]] = float(p[3])
            return f"GNET> ~OUTPUT,{p[1]},1,{float(p[3]):.2f}\r\n"
        if p[0] == "?OUTPUT":
            return f"GNET> ~OUTPUT,{p[1]},1,{self.levels.get(p[1], 0.0):.2f}\r\n"
        if p[0] == "#TIMECLOCK":
            self.timeclock[(p[1], p[3])] = int(p[4])
            return "GNET> \r\n"
        if p[0] == "?TIMECLOCK":
            return f"GNET> ~TIMECLOCK,{p[1]},6,{p[3]},{self.timeclock.get((p[1], p[3]), 1)}\r\n"
        if p[0] == "?SYSTEM":
            return "GNET> ~SYSTEM,1,12:00:00\r\n"
        return "GNET> \r\n"


@pytest.fixture
async def repeater():
    r = FakeRepeater()
    r.levels = {"40": 75.0, "50": 0.0}
    r.timeclock[("70", "3")] = 2
    await r.start()
    yield r
    await r.stop()


def make_config(port, **kw):
    return DriverConfig(kind="ra2", host="127.0.0.1", port=port,
                        integration_report=str(SAMPLE), **kw)


@pytest.fixture
async def driver(repeater):
    d = RA2Driver(make_config(repeater.port), connect_timeout=2)
    await d.connect()
    yield d
    await d.close()


def test_create_driver_ra2():
    assert isinstance(create_driver(DriverConfig(kind="ra2")), RA2Driver)


async def test_connect_login_monitoring_and_initial_query(driver, repeater):
    assert driver.connected
    await repeater.wait_for(lambda: "?TIMECLOCK,70,6,3" in repeater.commands)
    assert repeater.commands[0] == "#MONITORING,5,1"
    assert "?OUTPUT,40,1" in repeater.commands
    await repeater.wait_for(lambda: driver.zone("40").level == 75.0)
    await repeater.wait_for(lambda: driver.schedules()[2].enabled is False)
    assert driver.schedules()[0].enabled is True


async def test_set_level_and_monitoring_notify(driver, repeater):
    seen = []
    driver.add_listener(lambda zid, lvl: seen.append((zid, lvl)))
    await driver.set_level("50", 42.5, fade=90)
    await repeater.wait_for(lambda: ("50", 42.5) in seen)
    assert "#OUTPUT,50,1,42.5,1:30" in repeater.commands
    assert driver.zone("50").level == 42.5


async def test_set_levels_back_to_back(driver, repeater):
    await driver.set_levels({"40": 10, "45": 20, "46": 0}, fade=0.25)
    await repeater.wait_for(lambda: driver.zone("46").level == 0 and driver.zone("45").level == 20)
    for c in ("#OUTPUT,40,1,10,0.25", "#OUTPUT,45,1,20,0.25", "#OUTPUT,46,1,0,0.25"):
        assert c in repeater.commands


async def test_unsolicited_monitoring_line(driver, repeater):
    seen = []
    driver.add_listener(lambda zid, lvl: seen.append((zid, lvl)))
    repeater.writers[-1].write(b"~OUTPUT,31,1,66.00\r\nGNET> ")
    await repeater.wait_for(lambda: ("31", 66.0) in seen)


async def test_schedule_enable_disable(driver, repeater):
    await driver.set_schedule_enabled("70:1", False)
    await repeater.wait_for(lambda: repeater.timeclock.get(("70", "1")) == 2)
    assert "#TIMECLOCK,70,6,1,2" in repeater.commands
    # State is optimistic then confirmed by the ~TIMECLOCK reply to our follow-up query;
    # a late reply to an earlier connect-time query can briefly flip it, so wait to settle.
    await repeater.wait_for(lambda: driver.schedules()[0].enabled is False)
    await driver.set_schedule_enabled("70:3", True)
    await repeater.wait_for(lambda: "#TIMECLOCK,70,6,3,1" in repeater.commands)
    await repeater.wait_for(lambda: driver.schedules()[2].enabled is True)
    with pytest.raises(DriverError):
        await driver.set_schedule_enabled("bogus", True)


async def test_bad_login(repeater):
    d = RA2Driver(make_config(repeater.port, password="wrong"), connect_timeout=2)
    with pytest.raises(DriverError, match="login"):
        await d.connect()
    await d.close()
    assert not d.connected


async def test_connection_refused():
    d = RA2Driver(make_config(1), connect_timeout=1)
    with pytest.raises(DriverError):
        await d.connect()
    await d.close()


async def test_not_connected_send_raises():
    d = RA2Driver(make_config(1))
    with pytest.raises(DriverError):
        await d.set_level("40", 50)


async def test_reconnects_after_drop(driver, repeater):
    assert repeater.logins == 1
    repeater.drop_clients()
    await repeater.wait_for(lambda: repeater.logins == 2, timeout=5)
    await repeater.wait_for(lambda: driver.connected)
    await driver.set_level("40", 5)
    await repeater.wait_for(lambda: driver.zone("40").level == 5)


async def test_keepalive(repeater):
    d = RA2Driver(make_config(repeater.port), keepalive_interval=0.05, connect_timeout=2)
    await d.connect()
    try:
        await repeater.wait_for(lambda: "?SYSTEM,1" in repeater.commands)
    finally:
        await d.close()


async def test_backup(driver):
    b = await driver.backup()
    assert b["system"] == "RadioRA 2"
    assert "<Project>" in b["db_xml"]
    assert b["inventory"]["zones"]
    assert "40" in b["levels"]
    json.dumps(b)


async def test_missing_report_file():
    d = RA2Driver(DriverConfig(kind="ra2", host="127.0.0.1", integration_report="/nope.xml"))
    with pytest.raises(DriverError, match="integration report"):
        await d.connect()


def test_parse_real_ra2_timeclock_events():
    """Shape of a real RadioRA 2 export (AppVer 12.8): TimeClockEvents / EventNumber."""
    xml = """<?xml version="1.0" encoding="UTF-8" ?><Project><Areas><Area Name="Home" UUID="3"
      IntegrationID="0"><Areas /></Area></Areas><Timeclocks>
      <Timeclock Name="Project Timeclock" UUID="12" IntegrationID="17" SortOrder="0" Type="0">
        <TimeclockModes><TimeclockMode Name="Normal" UUID="36" Index="3" Type="1" /></TimeclockModes>
        <TimeClockEvents>
          <TimeClockEvent Name="Exterior OFF am" UUID="7399" Type="Variable" VariableEvent="Sunrise"
            EventNumber="1" ScheduleType="Weekly"
            Days="Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Saturday" Mode="Normal">
            <Actions /></TimeClockEvent>
          <TimeClockEvent Name="Dog Room Off" UUID="9" Type="Fixed" Time="22:00" EventNumber="5"
            ScheduleType="Weekly" Days="Saturday,Sunday" Mode="Normal"><Actions /></TimeClockEvent>
        </TimeClockEvents></Timeclock></Timeclocks></Project>"""
    inv = parse_db_xml(xml)
    assert [s.id for s in inv.schedules] == ["17:1", "17:5"]
    assert inv.schedules[0].name == "Exterior OFF am"
    assert inv.schedules[0].description == "Sunrise, every day (Project Timeclock event 1)"
    assert inv.schedules[1].description.startswith("22:00, Saturday,Sunday")


async def test_dmx_ramp_over_telnet_is_budgeted_and_pipelined(driver, repeater):
    from lutron_dmx.config import AppConfig, DmxConfig
    from lutron_dmx.controller import Controller
    from lutron_dmx.dmx.engine import DmxEngine, auto_assign

    zone_ids = [z.id for z in driver.zones()]
    cfg = AppConfig(dmx=DmxConfig(enabled=True, max_commands_per_s=20.0,
                                  mappings=auto_assign(zone_ids)))
    eng = DmxEngine(Controller(cfg, driver))
    eng._init_zone_states()
    await repeater.wait_for(lambda: "?TIMECLOCK,70,6,3" in repeater.commands)
    repeater.commands.clear()
    n, t = len(zone_ids), 0.0
    while t <= 6.0:  # a 6 s ramp at 40 fps, flushed on the engine's own clock
        eng.process_frame(bytes([round(255 * t / 6.0)]) * n + bytes(512 - n), t)
        await eng.flush(t)
        t += 0.025
    await repeater.wait_for(lambda: repeater.commands and
                            repeater.levels[zone_ids[0]] == 100.0 or len(repeater.commands) > 100)
    outputs = [c for c in repeater.commands if c.startswith("#OUTPUT")]
    assert 20 <= len(outputs) <= 20 * 6 + 2 * n  # budget, not 5 Hz x zones x 6 s
    ramp = [c.split(",") for c in outputs if len(c.split(",")) == 5]
    assert ramp and all(0 < float(c[4]) <= 5 for c in ramp)  # fade given in seconds


async def test_link_metrics_count_commands_and_echo_latency(driver, repeater):
    await repeater.wait_for(lambda: "?TIMECLOCK,70,6,3" in repeater.commands)
    base = driver.link_status()
    assert base["connected_for_s"] is not None and base["inflight"] == 0
    await driver.set_levels({"40": 10.0, "50": 20.0})
    await repeater.wait_for(lambda: driver.zone("50").level == 20.0)
    st = driver.link_status()
    assert st["commands_total"] - base["commands_total"] == 2
    assert st["commands_per_s"] >= 2
    assert st["echo"]["count"] >= 2 and st["echo"]["last"] is not None
    assert st["inflight"] == 0  # both were echoed


async def test_inflight_counts_unanswered_commands_and_expires(driver):
    driver._output_sent_at["40"] = time.monotonic()
    driver._output_sent_at["50"] = time.monotonic() - 60  # sent long ago, never echoed
    assert driver.inflight() == 1
    assert driver.link_status()["inflight"] == 1


async def test_link_metrics_across_a_reconnect(driver, repeater):
    repeater.drop_clients()
    await repeater.wait_for(lambda: repeater.logins == 2, timeout=5)
    await repeater.wait_for(lambda: driver.connected)
    st = driver.link_status()
    assert st["reconnects"] == 1 and st["reconnect_attempts"] == 0
    assert st["retry_in_s"] is None and st["connected_for_s"] is not None
