"""The RA2 driver's side of DMX storm protection, against a fake Main Repeater over real sockets."""

from __future__ import annotations

import asyncio
import time

import pytest
from test_ra2 import FakeRepeater, make_config

from lutron_to_dmx_bridge.config import AppConfig, DmxConfig
from lutron_to_dmx_bridge.controller import Controller
from lutron_to_dmx_bridge.dmx.engine import DmxEngine, auto_assign
from lutron_to_dmx_bridge.drivers.ra2 import RA2Driver
from lutron_to_dmx_bridge.models import Zone, ZoneType


class MuteRepeater(FakeRepeater):
    """Swallows ?SYSTEM,1 while `mute` is set: a repeater that has stopped answering."""

    mute = False

    def reply(self, cmd: str) -> str:
        if self.mute and cmd.startswith("?SYSTEM"):
            return "GNET> \r\n"
        return super().reply(cmd)


class SerialRepeater(FakeRepeater):
    """Does its telnet commands one after another, `service` seconds each, and counts them."""

    def __init__(self, service: float) -> None:
        super().__init__()
        self.service = service
        self.executed = 0

    async def handle(self, reader, writer):
        self.writers.append(writer)
        try:
            writer.write(b"login: ")
            await reader.readline()
            writer.write(b"password: ")
            await reader.readline()
            writer.write(b"\r\nGNET> ")
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                cmd = raw.decode().strip()
                self.commands.append(cmd)
                if self.service and cmd.startswith(("#OUTPUT", "?SYSTEM")):
                    await asyncio.sleep(self.service)
                if cmd.startswith("#OUTPUT"):
                    self.executed += 1
                writer.write(self.reply(cmd).encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def mute_repeater():
    r = MuteRepeater()
    await r.start()
    yield r
    await r.stop()


async def test_load_reports_how_long_an_unanswered_probe_has_waited(mute_repeater):
    d = RA2Driver(make_config(mute_repeater.port), connect_timeout=2, probe_interval=0.05, probe_fast=0.05)
    await d.connect()
    try:
        await mute_repeater.wait_for(lambda: d.metrics.latency.last is not None)
        assert d.load().latency_ms is not None and d.load().latency_ms < 200  # answering: a normal reading
        mute_repeater.mute = True
        # no new round trip arrives, yet the signal keeps rising instead of going quiet
        await mute_repeater.wait_for(lambda: (d.load().latency_ms or 0) >= 400, timeout=5)
        first = d.load().latency_ms
        await asyncio.sleep(0.3)
        assert d.load().latency_ms > first  # still climbing, whatever this machine's timer granularity
        assert len(d._probes_sent) <= d.max_probes_waiting  # and the probes do not pile up behind a dead repeater
    finally:
        await d.close()


async def test_probes_wait_up_to_a_minute_before_they_are_forgotten(repeater_driver):
    d, rep = repeater_driver
    # a repeater that is 6 s behind still answers in order: that probe must not be dropped from the pairing
    d._probes_sent.append(time.monotonic() - 6)
    await d._send("?SYSTEM,1")
    assert len(d._probes_sent) >= 1 and d._probes_sent[0] < time.monotonic() - 5
    d._probes_sent.clear()
    d._probes_sent.append(time.monotonic() - 61)
    await d._send("?SYSTEM,1")
    assert all(time.monotonic() - t < 5 for t in d._probes_sent)  # a lost reply is eventually given up on


@pytest.fixture
async def repeater_driver():
    rep = FakeRepeater()
    rep.levels = {"40": 75.0, "50": 0.0}
    await rep.start()
    d = RA2Driver(make_config(rep.port), connect_timeout=2)
    await d.connect()
    yield d, rep
    await d.close()
    await rep.stop()


async def test_inflight_window_forgets_commands_sooner_than_the_display_does(repeater_driver):
    d, _ = repeater_driver
    d._output_sent_at["40"] = time.monotonic() - 3
    d._output_sent_at["50"] = time.monotonic()
    assert d.inflight() == 2  # the Status tab still counts both (5 s)
    assert d.load().inflight == 1  # the engine's window only trusts the last 2 s


async def test_probes_speed_up_while_commands_flow():
    rep = FakeRepeater()
    rep.levels = {"40": 0.0}
    await rep.start()
    d = RA2Driver(make_config(rep.port), connect_timeout=2, probe_interval=5.0, probe_fast=0.05)
    await d.connect()
    try:
        await asyncio.sleep(0.3)
        idle = sum(1 for c in rep.commands if c.startswith("?SYSTEM"))
        await d.set_level("40", 30.0)
        await rep.wait_for(lambda: sum(1 for c in rep.commands if c.startswith("?SYSTEM")) >= 3, timeout=5)
        assert idle == 0  # (probe_interval is 5 s: without the speed-up none would have come yet)
    finally:
        await d.close()
        await rep.stop()


async def test_reset_connection_aborts_the_socket_and_the_driver_reconnects(repeater_driver):
    d, rep = repeater_driver
    assert await d.reset_connection() is True
    await rep.wait_for(lambda: rep.logins == 2, timeout=5)
    await rep.wait_for(lambda: d.connected)
    await d.close()
    assert await d.reset_connection() is False  # nothing to reset


async def test_the_engine_pause_resets_a_connection_that_is_behind():
    rep = MuteRepeater()
    await rep.start()
    d = RA2Driver(make_config(rep.port), connect_timeout=2, probe_interval=0.05, probe_fast=0.05)
    await d.connect()
    cfg = AppConfig(dmx=DmxConfig(enabled=True, mappings=auto_assign(["40"])))
    eng = DmxEngine(Controller(cfg, d))
    try:
        await eng.pause()
        assert rep.logins == 1  # the repeater was answering: nothing to throw away, no reset
        rep.mute = True
        await asyncio.sleep(0.8)  # probes unanswered for a while: the repeater is behind
        eng.resume()
        await eng.pause()
        await rep.wait_for(lambda: rep.logins == 2, timeout=5)
    finally:
        await d.close()
        await rep.stop()


async def _storm(protect: bool, seconds: float = 4.0, service: float = 0.04, n: int = 40):
    """A full-house triangle sweep, real time, against a repeater that does 25 commands/s with
    the limit set to 60/s. Returns the deepest backlog the repeater had (written, not yet executed)."""
    rep = SerialRepeater(service)
    await rep.start()
    d = RA2Driver(make_config(rep.port), connect_timeout=2, probe_interval=2.0, probe_fast=0.2)
    await d.connect()
    zones = [Zone(id=str(i + 1), name=f"z{i + 1}", type=ZoneType.DIMMER) for i in range(n)]
    d._inventory.zones = zones
    cfg = AppConfig(dmx=DmxConfig(enabled=True, mappings=auto_assign([z.id for z in zones]),
                                  max_commands_per_s=60.0, protect=protect, latency_limit_ms=300.0))
    eng = DmxEngine(Controller(cfg, d))
    eng._init_zone_states()
    await rep.wait_for(lambda: any(c.startswith("?TIMECLOCK") for c in rep.commands))
    await asyncio.sleep(0.1)
    base_cmds = d.metrics.commands.total
    base_exec = rep.executed
    peak = 0
    start = time.monotonic()
    while (el := time.monotonic() - start) < seconds:
        x = (el % 2.0) / 2.0
        v = 255 * (2 * x if x < 0.5 else 2 - 2 * x)
        eng.process_frame(bytes([round(v)]) * n + bytes(512 - n), time.monotonic())
        await eng.flush()
        peak = max(peak, d.metrics.commands.total - base_cmds - (rep.executed - base_exec))
        await asyncio.sleep(0.025)
    rate = eng.protector.rate
    await d.close()
    await rep.stop()
    return peak, rate


async def test_over_real_sockets_protection_keeps_the_repeaters_backlog_short():
    unprotected, _ = await _storm(protect=False)
    protected, rate = await _storm(protect=True)
    assert unprotected > 50  # 60 commands/s into a repeater that does 25: the backlog grows every second
    assert protected <= 20  # held to the window: rate (60/s) x latency limit (0.3 s) = 18
    assert rate < 60  # it noticed
