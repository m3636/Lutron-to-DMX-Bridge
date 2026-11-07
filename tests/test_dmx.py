"""Tests for DMX input: packet codecs, USB Pro parser, engine mapping and sending."""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from lutron_to_dmx_bridge.config import AppConfig, DmxConfig, DmxFixtureMapping
from lutron_to_dmx_bridge.dmx import pad_frame
from lutron_to_dmx_bridge.dmx import artnet, sacn, usb
from lutron_to_dmx_bridge.dmx.engine import (
    DEADBAND,
    SETTLE_TIME,
    DmxEngine,
    auto_assign,
    fixture_target,
    validate_mappings,
)
from lutron_to_dmx_bridge.models import Zone


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDriver:
    connected = True

    def __init__(self, zones: list[str] | None = None) -> None:
        self._zones = {z: Zone(id=z, name=f"Zone {z}") for z in (zones or [])}
        self.calls: list[tuple[str, dict[str, float], float | None]] = []

    def zone(self, zone_id: str):
        return self._zones.get(zone_id)

    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        self.calls.append(("one", {zone_id: level}, fade))

    async def set_levels(self, levels: dict[str, float], fade: float | None = None) -> None:
        self.calls.append(("many", dict(levels), fade))

    def sent(self) -> list[dict[str, float]]:
        return [c[1] for c in self.calls]


class FakeController:
    def __init__(self, dmx: DmxConfig, zones: list[str] | None = None) -> None:
        self.config = AppConfig(dmx=dmx)
        self.driver = FakeDriver(zones)


def frame(**channels: int) -> bytes:
    """frame(c1=255, c3=10) -> 512 bytes."""
    f = bytearray(512)
    for k, v in channels.items():
        f[int(k[1:]) - 1] = v
    return bytes(f)


# ---------------------------------------------------------------------------
# Art-Net
# ---------------------------------------------------------------------------
def test_artdmx_hand_built_packet_parses():
    data = bytes(range(1, 11))
    # Net 1, SubUni 0x23 -> Port-Address 0x123
    pkt = b"Art-Net\x00" + struct.pack("<H", 0x5000) + bytes([0, 14, 7, 0, 0x23, 0x01]) \
        + struct.pack(">H", len(data)) + data
    parsed = artnet.parse_artdmx(pkt)
    assert parsed is not None
    assert parsed.port_address == 0x123
    assert parsed.sequence == 7
    assert parsed.data == data


def test_artdmx_build_roundtrip_and_rejects():
    pkt = artnet.build_artdmx(5, b"\x01\x02\x03", sequence=9)
    p = artnet.parse_artdmx(pkt)
    assert p.port_address == 5 and p.data[:3] == b"\x01\x02\x03" and len(p.data) == 4
    assert artnet.parse_artdmx(b"Not-Art\x00" + pkt[8:]) is None
    assert artnet.parse_artdmx(pkt[:-2]) is None  # truncated
    assert artnet.parse_artdmx(artnet.build_artpoll()) is None
    assert artnet.parse_opcode(artnet.build_artpoll()) == artnet.OP_POLL


def test_artpoll_reply_layout():
    r = artnet.build_artpoll_reply("192.168.1.50", 0x0123, receiving=True)
    assert len(r) == 239
    assert r[:8] == b"Art-Net\x00"
    assert struct.unpack_from("<H", r, 8)[0] == 0x2100
    assert r[10:14] == socket.inet_aton("192.168.1.50")
    assert struct.unpack_from("<H", r, 14)[0] == 6454
    assert r[18] == 0x01 and r[19] == 0x02  # NetSwitch, SubSwitch
    assert r[26:44].split(b"\x00")[0] == b"LutronToDMXBridge"
    assert r[44:108].split(b"\x00")[0] == b"Lutron To DMX Bridge"
    assert struct.unpack_from(">H", r, 172)[0] == 1  # NumPorts
    assert r[174] == 0x80  # PortTypes[0]: output
    assert r[182] == 0x80  # GoodOutput[0]
    assert r[190] == 0x03  # SwOut[0]
    assert r[212] & 0x08  # Status2: 15-bit port address


# ---------------------------------------------------------------------------
# sACN
# ---------------------------------------------------------------------------
def test_sacn_hand_built_packet_parses():
    data = bytes([10, 20, 30])
    pkt = sacn.build_sacn(258, data, source_name="Console", priority=150, sequence=3)
    assert pkt[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert struct.unpack_from(">H", pkt, 16)[0] == 0x7000 | (len(pkt) - 16)
    assert struct.unpack_from(">H", pkt, 38)[0] == 0x7000 | (len(pkt) - 38)
    assert struct.unpack_from(">H", pkt, 115)[0] == 0x7000 | (len(pkt) - 115)
    p = sacn.parse_sacn(pkt)
    assert p is not None
    assert (p.universe, p.priority, p.sequence, p.source_name) == (258, 150, 3, "Console")
    assert p.start_code == 0 and p.data == data
    assert sacn.parse_sacn(pkt[:100]) is None
    assert sacn.parse_sacn(b"x" * len(pkt)) is None


def test_sacn_universe_and_group():
    assert sacn.effective_universe(0) == 1
    assert sacn.multicast_group(0) == "239.255.0.1"
    assert sacn.multicast_group(258) == "239.255.1.2"


def test_sacn_priority_merge_and_termination():
    m = sacn.SourceMerger()
    a = sacn.parse_sacn(sacn.build_sacn(1, bytes([100, 0]), cid=b"A" * 16, priority=100))
    b = sacn.parse_sacn(sacn.build_sacn(1, bytes([50, 200]), cid=b"B" * 16, priority=100))
    hi = sacn.parse_sacn(sacn.build_sacn(1, bytes([1, 1]), cid=b"C" * 16, priority=150))
    assert m.update(a, 0.0)[:2] == bytes([100, 0])
    assert m.update(b, 0.1)[:2] == bytes([100, 200])  # HTP at equal priority
    assert m.update(hi, 0.2)[:2] == bytes([1, 1])  # higher priority wins
    term = sacn.parse_sacn(sacn.build_sacn(1, bytes([1, 1]), cid=b"C" * 16, priority=150,
                                           options=sacn.OPT_STREAM_TERMINATED))
    assert m.update(term, 0.3)[:2] == bytes([100, 200])
    # Non-zero start code ignored
    sc = sacn.parse_sacn(sacn.build_sacn(1, bytes([9]), cid=b"D" * 16, start_code=0xDD))
    assert m.update(sc, 0.4) is None
    # Sources time out
    assert m.merged(10.0) is None


# ---------------------------------------------------------------------------
# USB Pro
# ---------------------------------------------------------------------------
def test_usb_pro_parser_fragmented_with_garbage():
    dmx = bytes([0, 0]) + bytes([1, 2, 3, 255])  # status, start code, slots
    stream = b"\x00\xe7garbage" + usb.build_message(5, dmx) + usb.build_message(3, b"\x01\x02\x03") \
        + usb.build_message(5, dmx)
    parser = usb.ProParser()
    msgs = []
    for i in range(0, len(stream), 3):  # 3-byte fragments
        msgs += parser.feed(stream[i : i + 3])
    assert [m.label for m in msgs] == [5, 3, 5]
    dec = usb.ProFrameDecoder()
    f = dec.handle(msgs[0])
    assert len(f) == 512 and f[:4] == bytes([1, 2, 3, 255]) and f[4] == 0


def test_usb_pro_parser_resyncs_on_bad_end_byte():
    bad = bytes([0x7E, 5, 2, 0, 0, 0, 0x00])  # wrong EOM
    good = usb.build_message(5, b"\x00\x00\x42")
    msgs = usb.ProParser().feed(bad + good)
    assert len(msgs) == 1 and msgs[0].payload == b"\x00\x00\x42"


def test_usb_decoder_ignores_bad_frames_and_handles_change_of_state():
    dec = usb.ProFrameDecoder()
    assert dec.handle(usb.ProMessage(5, b"\x02\x00\x10")) is None  # overrun
    assert dec.handle(usb.ProMessage(5, b"\x00\xcc\x10")) is None  # non-zero start code
    # Block 0, bits 1 and 3 -> slots 1 and 3
    f = dec.handle(usb.ProMessage(9, bytes([0, 0b00001010, 0, 0, 0, 0, 77, 88])))
    assert f[0] == 77 and f[2] == 88


async def test_usb_receiver_detects_silent_open_dmx():
    class SilentSerial:  # an Open DMX never answers
        def reset_input_buffer(self): ...
        def write(self, b): return len(b)
        def read(self, n): return b""
        def close(self): ...

    rx = usb.UsbProReceiver("COM9", lambda f: None, probe_timeout=0.05,
                            serial_factory=lambda p: SilentSerial())
    with pytest.raises(usb.OpenDmxNotSupported, match="output-only"):
        await rx.start()


async def test_usb_receiver_reads_frames():
    class ProSerial:
        def __init__(self):
            self.out: list[bytes] = []
            self.queue = [usb.build_message(3, b"\x01\x02\x00\x00\x00")]
        def reset_input_buffer(self): ...
        def write(self, b):
            self.out.append(b)
            return len(b)
        def read(self, n):
            if self.queue:
                return self.queue.pop(0)
            if any(o[1] == usb.LABEL_RECV_ON_CHANGE for o in self.out):
                import time; time.sleep(0.01)
                return usb.build_message(5, b"\x00\x00\x80\x40")
            return b""
        def close(self): ...

    ser = ProSerial()
    got: list[bytes] = []
    rx = usb.UsbProReceiver("COM3", got.append, serial_factory=lambda p: ser)
    await rx.start()
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.01)
    await rx.stop()
    assert got and got[0][:2] == b"\x80\x40"
    assert ser.out[-1] == usb.build_message(8, b"\x00")


def test_list_serial_ports_returns_list():
    assert isinstance(usb.list_serial_ports(), list)


# ---------------------------------------------------------------------------
# Mapping math
# ---------------------------------------------------------------------------
def test_fixture_target_math():
    m = DmxFixtureMapping("z", 10, ["intensity", "on_off", "fade"])
    assert fixture_target(m, frame(c10=255, c11=255, c12=30)) == (100.0, 3.0)
    assert fixture_target(m, frame(c10=128, c11=200)) == (round(128 * 100 / 255, 2), 0.0)
    assert fixture_target(m, frame(c10=255, c11=127))[0] == 0.0
    onoff = DmxFixtureMapping("z", 1, ["on_off"])
    assert fixture_target(onoff, frame(c1=128)) == (100.0, None)
    assert fixture_target(onoff, frame(c1=0)) == (0.0, None)
    assert fixture_target(DmxFixtureMapping("z", 512, ["intensity"]), frame(c512=51))[0] == 20.0
    assert fixture_target(DmxFixtureMapping("z", 1, ["fade"]), frame())[0] is None


def test_auto_assign_and_validate():
    maps = auto_assign(["a", "b", "c"], ["intensity", "fade"], start=5)
    assert [m.address for m in maps] == [5, 7, 9]
    assert all(m.params == ["intensity", "fade"] for m in maps)
    assert validate_mappings(maps) == []
    with pytest.raises(ValueError):
        auto_assign(["a", "b"], ["intensity"], start=512)
    warns = validate_mappings([
        DmxFixtureMapping("a", 1, ["intensity", "fade"]),
        DmxFixtureMapping("b", 2, ["intensity"]),
        DmxFixtureMapping("c", 512, ["intensity", "on_off"]),
        DmxFixtureMapping("d", 0, ["bogus"]),
        DmxFixtureMapping("e", 2, ["intensity"], enabled=False),
    ])
    text = "\n".join(warns)
    assert "channel 2 overlaps" in text
    assert "outside 1-512" in text
    assert "unknown param 'bogus'" in text
    assert "zone e" not in text


# ---------------------------------------------------------------------------
# Engine sending policy
# ---------------------------------------------------------------------------
def make_engine(mappings, rate=5.0, zones=("a", "b")):
    ctl = FakeController(DmxConfig(enabled=True, max_rate_hz=rate, mappings=mappings), list(zones))
    return DmxEngine(ctl), ctl.driver


async def test_engine_batches_and_deadband():
    eng, drv = make_engine(auto_assign(["a", "b"], ["intensity", "fade"]))
    eng._init_zone_states()  # zones start at level 0 -> no send for zero frame
    eng.process_frame(frame(), 0.0)
    assert await eng.flush(0.0) == 0

    eng.process_frame(frame(c1=255, c2=20, c3=255, c4=20), 1.0)
    assert await eng.flush(1.0) == 2
    assert drv.calls == [("many", {"a": 100.0, "b": 100.0}, 2.0)]

    # 1 DMX step (0.39 %) is under the deadband: not sent immediately...
    eng.process_frame(frame(c1=254, c2=20, c3=255, c4=20), 2.0)
    assert await eng.flush(2.0) == 0
    # ...but is sent once stable (trailing edge)
    assert await eng.flush(2.0 + SETTLE_TIME) == 1
    assert drv.calls[-1] == ("one", {"a": round(254 * 100 / 255, 2)}, 2.0)
    assert DEADBAND > 100 / 255


async def test_engine_rate_limit_and_trailing_flush():
    eng, drv = make_engine([DmxFixtureMapping("a", 1)], rate=5.0)  # 200 ms per zone
    eng._init_zone_states()
    t = 10.0
    # A 1 s fader move at 40 fps from 0 to 255
    for i in range(41):
        eng.process_frame(frame(c1=round(255 * i / 40)), t + i * 0.025)
        await eng.flush(t + i * 0.025)
    sends = [c for c in drv.calls]
    assert 4 <= len(sends) <= 6  # ~5 Hz over 1 s
    # The final 100 % may still be pending; the periodic flush must deliver it.
    end = t + 40 * 0.025
    for k in range(1, 10):
        await eng.flush(end + k * 0.05)
    assert drv.sent()[-1] == {"a": 100.0}
    n = len(drv.calls)
    for k in range(10, 20):  # nothing more once stable
        await eng.flush(end + k * 0.05)
    assert len(drv.calls) == n


async def test_engine_rate_limit_spacing():
    eng, drv = make_engine([DmxFixtureMapping("a", 1)], rate=2.0)
    eng._init_zone_states()
    stamps = []
    orig = drv.set_level

    async def rec(z, lvl, fade=None):
        stamps.append(now)
        await orig(z, lvl, fade)

    drv.set_level = rec
    now = 0.0
    for i in range(100):
        now = i * 0.02
        eng.process_frame(frame(c1=(i * 7) % 256), now)
        await eng.flush(now)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps and min(gaps) >= 0.5 - 1e-9


async def test_engine_does_not_fight_external_changes_and_status():
    eng, drv = make_engine([DmxFixtureMapping("a", 1, ["on_off"])])
    eng._init_zone_states()
    eng.process_frame(frame(c1=255), 0.0)
    await eng.flush(0.0)
    assert drv.sent() == [{"a": 100.0}]
    drv.zone("a").level = 30.0  # someone used a keypad
    eng.process_frame(frame(c1=255), 1.0)
    assert await eng.flush(1.0) == 0

    st = eng.status()
    assert st["packets"] == 2 and st["source"] == "artnet" and st["mapped_zones"] == 1
    cmap = eng.channel_map()
    assert cmap == [{"channel": 1, "zone_id": "a", "zone_name": "Zone a", "param": "on_off",
                     "supported": True, "enabled": True, "value": 255, "conflict": False}]
    assert len(eng.last_frame) == 512


async def test_engine_send_failure_is_retried():
    eng, drv = make_engine([DmxFixtureMapping("a", 1)], rate=10.0)
    eng._init_zone_states()
    fail = {"n": 1}
    orig = drv.set_level

    async def flaky(z, lvl, fade=None):
        if fail["n"]:
            fail["n"] -= 1
            raise ConnectionError("boom")
        await orig(z, lvl, fade)

    drv.set_level = flaky
    eng.process_frame(frame(c1=255), 0.0)
    assert await eng.flush(0.0) == 0
    assert "boom" in eng.status()["error"]
    assert await eng.flush(0.05) == 0  # rate limited
    assert await eng.flush(0.1) == 1
    assert eng.status()["error"] is None


def test_usb_status_mentions_open_dmx():
    eng, _ = make_engine([], zones=())
    eng.config.source = "usb"
    assert "Open DMX USB is output-only" in eng.status()["note"]


async def test_engine_unknown_source_errors():
    eng, _ = make_engine([], zones=())
    eng.config.source = "bogus"
    with pytest.raises(ValueError):
        await eng.start()
    assert "bogus" in eng.status()["error"]
    await eng.stop()


# ---------------------------------------------------------------------------
# End to end over real UDP
# ---------------------------------------------------------------------------
async def _wait_for(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def test_artnet_end_to_end_udp():
    dmx = DmxConfig(enabled=True, source="artnet", universe=3, bind_ip="0.0.0.0", max_rate_hz=20,
                    mappings=auto_assign(["a", "b"], ["intensity"], start=1))
    ctl = FakeController(dmx, ["a", "b"])
    eng = DmxEngine(ctl, port=0)
    await eng.start()
    try:
        port = eng.receiver.bound_port
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.settimeout(2.0)
        tx.bind(("127.0.0.1", 0))
        # Wrong universe is ignored
        tx.sendto(artnet.build_artdmx(4, bytes([255, 255])), ("127.0.0.1", port))
        tx.sendto(artnet.build_artdmx(3, bytes([255, 51])), ("127.0.0.1", port))
        assert await _wait_for(lambda: {"a": 100.0, "b": 20.0} in ctl.driver.sent()
                               or any(c[1] == {"a": 100.0, "b": 20.0} for c in ctl.driver.calls))
        st = eng.status()
        assert st["running"] and st["packets"] == 1
        assert 4 in st["receiver"]["other_universes"]

        # ArtPoll -> ArtPollReply back to the sender
        tx.sendto(artnet.build_artpoll(), ("127.0.0.1", port))
        reply = await asyncio.to_thread(tx.recv, 1024)
        assert artnet.parse_opcode(reply) == artnet.OP_POLL_REPLY
        assert reply[10:14] == socket.inet_aton("127.0.0.1")
        assert reply[26:44].split(b"\x00")[0] == b"LutronToDMXBridge"
        tx.close()
    finally:
        await eng.stop()
    assert not eng.status()["running"]


async def test_sacn_end_to_end_udp_unicast():
    dmx = DmxConfig(enabled=True, source="sacn", universe=0, max_rate_hz=20,
                    mappings=[DmxFixtureMapping("a", 2, ["intensity"])])
    ctl = FakeController(dmx, ["a"])
    eng = DmxEngine(ctl, port=0)
    await eng.start()
    try:
        port = eng.receiver.bound_port
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            tx.sendto(sacn.build_sacn(1, bytes([0, 255])), ("127.0.0.1", port))
        assert await _wait_for(lambda: ctl.driver.sent() == [{"a": 100.0}])
        assert eng.status()["effective_universe"] == 1
    finally:
        await eng.stop()


def test_pad_frame():
    assert len(pad_frame(b"\x01")) == 512
    assert len(pad_frame(bytes(600))) == 512


# ---------------------------------------------------------------------------
# Many zones moving together (a console ramp or chase)
# ---------------------------------------------------------------------------
def ramp_frame(n: int, value: int) -> bytes:
    return bytes([value]) * n + bytes(512 - n)


async def run_ramp(eng, n, seconds=10.0, fps=40, start=100.0):
    """Feed a linear 0->255 ramp on n channels; flush at every frame and every 50 ms tick."""
    t, end = start, start + seconds
    while t <= end + 1e-9:
        v = round(255 * min(1.0, (t - start) / seconds))
        eng.process_frame(ramp_frame(n, v), t)
        await eng.flush(t)
        t += 1.0 / fps
    for k in range(1, 200):  # trailing edge
        await eng.flush(end + k * 0.05)
    return end


class LightModel:
    """Each command moves the light linearly to the level over its fade."""

    def __init__(self):
        self.track: dict[str, list[tuple[float, float, float, float]]] = {}

    def apply(self, t, zone, level, fade):
        tr = self.track.setdefault(zone, [(-1e9, 0.0, 0.0, 0.0)])
        tr.append((t, self.at(zone, t), level, fade or 0.0))

    def at(self, zone, t):
        t0, a, b, f = self.track[zone][-1] if zone in self.track else (-1e9, 0.0, 0.0, 0.0)
        for entry in self.track.get(zone, []):
            if entry[0] <= t:
                t0, a, b, f = entry
        return b if f <= 0 or t >= t0 + f else a + (b - a) * (t - t0) / f


def timed_driver(eng, drv, model, clock):
    async def many(levels, fade=None):
        drv.calls.append(("many", dict(levels), fade))
        for z, lvl in levels.items():
            model.apply(clock["t"], z, lvl, fade)

    async def one(zone_id, level, fade=None):
        await many({zone_id: level}, fade)

    drv.set_levels, drv.set_level = many, one


async def test_engine_total_command_budget_serves_zones_in_turn():
    n = 40
    zones = [str(i) for i in range(n)]
    eng, drv = make_engine(auto_assign(zones), rate=5.0, zones=zones)
    eng.config.max_commands_per_s = 10.0
    eng.config.max_inflight = 0  # the in-flight window has its own tests (test_protect.py)
    eng._init_zone_states()
    # A cue that takes every zone from 0 to 100% at once: one burst, nothing held back.
    eng.process_frame(ramp_frame(n, 255), 0.0)
    assert await eng.flush(0.0) == n
    assert all(c[2] is None for c in drv.calls)  # a jump has no fade
    # Everything drops again: the budget now lets 10/s through, longest-waiting first.
    drv.calls.clear()
    eng.process_frame(ramp_frame(n, 0), 5.0)
    first = await eng.flush(5.0)
    assert first == n  # bucket refilled to one command per zone while idle
    # Twenty zones start changing at the same moment, but only 10 commands/s are allowed.
    drv.calls.clear()
    eng.process_frame(ramp_frame(n, 255), 10.0)
    await eng.flush(10.0)  # burst
    drv.calls.clear()
    eng.process_frame(ramp_frame(n, 0), 10.5)
    seen: dict[str, int] = {}
    for k in range(40):  # 2 s of 50 ms ticks
        await eng.flush(10.5 + k * 0.05)
        for c in drv.calls:
            for z in c[1]:
                seen[z] = seen.get(z, 0) + 1
        drv.calls.clear()
    assert 15 <= sum(seen.values()) <= 25  # ~10/s over 2 s
    assert max(seen.values()) == 1 and len(seen) == sum(seen.values())  # nobody sent twice before everybody once


async def test_engine_ramp_uses_fades_and_few_commands():
    n = 30
    zones = [str(i) for i in range(n)]
    eng, drv = make_engine(auto_assign(zones), rate=5.0, zones=zones)
    eng.config.max_commands_per_s = 20.0
    eng._init_zone_states()
    model, clock = LightModel(), {"t": 0.0}
    timed_driver(eng, drv, model, clock)
    orig_flush = eng.flush

    async def flush(now=None):
        clock["t"] = now
        return await orig_flush(now)

    eng.flush = flush
    end = await run_ramp(eng, n, seconds=10.0, start=100.0)
    cmds = sum(len(c[1]) for c in drv.calls)
    assert cmds < 20.0 * 10.0 + n + 10  # budget, not 5 Hz x 30 zones = 1500
    assert any(c[2] for c in drv.calls)  # smooth ramp commands carry a fade
    for t in (102.0, 105.0, 108.0, end):
        ideal = min(100.0, (t - 100.0) * 10.0)
        levels = [model.at(z, t) for z in zones]
        assert max(abs(x - ideal) for x in levels) < 8.0, (t, min(levels), max(levels))
    assert all(model.at(z, end + 6) == 100.0 for z in zones)


async def test_engine_ramp_down_and_endpoints_arrive_on_time():
    eng, drv = make_engine(auto_assign(["a"]), rate=5.0, zones=["a"])
    drv.zone("a").level = 100.0
    eng._init_zone_states()
    model, clock = LightModel(), {"t": 0.0}
    timed_driver(eng, drv, model, clock)
    model.track["a"] = [(-1e9, 100.0, 100.0, 0.0)]
    orig_flush = eng.flush

    async def flush(now=None):
        clock["t"] = now
        return await orig_flush(now)

    eng.flush = flush
    t, end = 50.0, 56.0
    while t <= end:
        v = round(255 * (1 - (t - 50.0) / 6.0))
        eng.process_frame(frame(c1=v), t)
        await eng.flush(t)
        t += 0.025
    for k in range(1, 120):
        await eng.flush(end + k * 0.05)
    assert model.at("a", end + 0.3) < 4.0
    assert model.at("a", end + 6) == 0.0
    assert abs(model.at("a", 53.0) - 50.0) < 10.0


async def test_engine_cue_jump_is_not_faded_and_fade_channel_wins():
    maps = [DmxFixtureMapping("a", 1), DmxFixtureMapping("b", 2, ["intensity", "fade"])]
    eng, drv = make_engine(maps, zones=["a", "b"])
    eng._init_zone_states()
    # a ramp on both zones, with b also carrying a fade channel of 3 s
    t = 0.0
    for i in range(40):
        eng.process_frame(frame(c1=i * 2, c2=i * 2, c3=30), t)
        await eng.flush(t)
        t += 0.025
    ramp_fades = {z: c[2] for c in drv.calls for z in c[1]}
    assert ramp_fades["b"] == 3.0
    assert any(c[2] for c in drv.calls if "a" in c[1])  # a glides
    # then both jump: a has no fade at all, b keeps its channel
    drv.calls.clear()
    eng.process_frame(frame(c1=255, c2=255, c3=30), t + 1.0)
    await eng.flush(t + 1.0)
    jump = {z: c[2] for c in drv.calls for z in c[1]}
    assert jump["a"] is None and jump["b"] == 3.0


async def test_engine_does_not_ramp_switches():
    eng, drv = make_engine([DmxFixtureMapping("a", 1)], zones=["a"])
    from lutron_to_dmx_bridge.models import ZoneType

    drv.zone("a").type = ZoneType.SWITCH
    eng._init_zone_states()
    t = 0.0
    for i in range(60):
        eng.process_frame(frame(c1=i * 4), t)
        await eng.flush(t)
        t += 0.025
    for k in range(1, 20):
        await eng.flush(t + k * 0.05)
    assert drv.calls and all(c[2] is None for c in drv.calls)
    assert drv.sent()[-1]["a"] == pytest.approx(round(59 * 4 * 100 / 255, 2), abs=2.5)


async def test_engine_small_first_move_waits_to_see_if_it_is_a_ramp():
    from lutron_to_dmx_bridge.dmx.engine import RAMP_HOLD

    eng, drv = make_engine([DmxFixtureMapping("a", 1)], zones=["a"])
    eng._init_zone_states()
    eng.process_frame(frame(c1=10), 0.0)  # 3.9 %, over the deadband but only a nudge
    assert await eng.flush(0.0) == 0
    assert await eng.flush(RAMP_HOLD - 0.01) == 0
    assert await eng.flush(RAMP_HOLD) == 1
    assert drv.calls[-1][2] is None


async def test_engine_reverse_during_glide_is_not_held_back():
    zones = [str(i) for i in range(20)]
    eng, drv = make_engine(auto_assign(zones), rate=5.0, zones=zones)
    eng.config.max_commands_per_s = 10.0  # long glide: ~2 s
    eng._init_zone_states()
    t = 0.0
    for i in range(24):
        eng.process_frame(ramp_frame(20, i * 4), t)
        await eng.flush(t)
        t += 0.025
    drv.calls.clear()
    for i in range(10):  # the console reverses hard
        eng.process_frame(ramp_frame(20, max(0, 92 - i * 12)), t)
        await eng.flush(t)
        t += 0.025
    for k in range(20):
        await eng.flush(t + k * 0.05)
    assert drv.calls  # we did not sit out the rest of the glide


def test_old_config_without_command_budget_gets_default():
    cfg = AppConfig.from_dict({"dmx": {"enabled": True, "max_rate_hz": 4.0}})
    assert cfg.dmx.max_commands_per_s == 20.0 and cfg.dmx.max_rate_hz == 4.0


async def test_engine_reports_held_back_zones_and_command_rate():
    n = 40
    zones = [str(i) for i in range(n)]
    eng, drv = make_engine(auto_assign(zones), rate=5.0, zones=zones)
    eng.config.max_commands_per_s = 10.0
    eng.config.max_inflight = 0  # the in-flight window has its own tests (test_protect.py)
    eng._init_zone_states()
    eng.process_frame(ramp_frame(n, 255), 0.0)
    assert await eng.flush(0.0) == n  # the opening burst fits the bucket: nothing held
    assert eng.held_back == 0
    eng.process_frame(ramp_frame(n, 0), 0.5)
    sent = await eng.flush(0.5)
    assert sent < n and eng.held_back == n - sent  # everything else waits for the budget
    assert eng.status()["held_back"] == eng.held_back
    for k in range(1, 200):  # drain it
        await eng.flush(0.5 + k * 0.05)
    assert eng.held_back == 0
    assert eng.commands.total == eng.commands_sent
    assert "commands_per_s" in eng.status()


# ---------------------------------------------------------------------------
# Level step (dmx.resolution_pct)
# ---------------------------------------------------------------------------
def step_engine(step, zones=("a",)):
    eng, drv = make_engine(auto_assign(list(zones)), rate=50.0, zones=zones)
    eng.config.resolution_pct = step
    eng._init_zone_states()
    return eng, drv


def test_snap_rounds_to_the_step_but_keeps_off_full_and_dim():
    eng, _ = step_engine(5.0)
    assert [eng._snap(x) for x in (0.0, 100.0)] == [0.0, 100.0]
    assert eng._snap(12.4) == 10.0 and eng._snap(12.6) == 15.0
    assert eng._snap(1.0) == 5.0, "a light that is on never rounds down to off"
    assert eng._snap(99.0) == 95.0, "and never rounds up to full"
    eng.config.resolution_pct = 3.0  # does not divide 100: the top grid point is 99
    assert eng._snap(98.9) == 99.0 and eng._snap(100.0) == 100.0
    eng.config.resolution_pct = DEADBAND  # the default sends exact levels
    assert eng._snap(33.33) == 33.33
    eng.config.resolution_pct = 0  # a bad value falls back to the default
    assert eng._step() == DEADBAND and eng._snap(33.33) == 33.33


async def test_coarse_step_sends_fewer_commands_with_hysteresis():
    exact, drv_exact = step_engine(DEADBAND)
    coarse, drv_coarse = step_engine(5.0)
    for eng in (exact, coarse):
        for i in range(101):  # a 4 s fader move, 0 -> 100 %, at 25 fps
            t = 10.0 + i * 0.04
            eng.process_frame(frame(c1=round(255 * i / 100)), t)
            await eng.flush(t)
        for k in range(200):  # let anything pending finish
            await eng.flush(15.0 + k * 0.05)
    assert len(drv_coarse.calls) < len(drv_exact.calls) / 2
    assert len(drv_coarse.calls) >= 8, "still a usable ramp"
    sent = [next(iter(c[1].values())) for c in drv_coarse.calls]
    assert sent[-1] == 100.0, "the final level is exact"
    assert all(lvl == 100.0 or lvl % 5.0 == 0 for lvl in sent), sent  # every level is on the grid


async def test_jitter_inside_the_step_sends_nothing_and_settles_to_the_grid():
    eng, drv = step_engine(5.0)
    eng.process_frame(frame(c1=128), 0.0)  # 50.2 %
    for _ in range(20):
        await eng.flush(5.0)
    n = len(drv.calls)
    assert n >= 1 and next(iter(drv.calls[-1][1].values())) == 50.0
    for i, v in enumerate((126, 129, 127, 130, 128, 131, 125)):  # +-1 % noise on the fader
        eng.process_frame(frame(c1=v), 10.0 + i * 0.05)
        await eng.flush(10.0 + i * 0.05)
    for k in range(40):
        await eng.flush(11.0 + k * 0.05)
    assert len(drv.calls) == n, "noise smaller than the step costs no commands"


def test_resolution_default_and_old_configs():
    cfg = AppConfig.from_dict({"dmx": {"enabled": True}})
    assert cfg.dmx.resolution_pct == DEADBAND
# Clearing the queue
# ---------------------------------------------------------------------------
async def test_clear_pending_drops_held_back_zones_until_the_source_moves():
    n = 40
    zones = [str(i) for i in range(n)]
    eng, drv = make_engine(auto_assign(zones), rate=5.0, zones=zones)
    eng.config.max_commands_per_s = 10.0
    eng.config.max_inflight = 0  # the in-flight window has its own tests (test_protect.py)
    eng._init_zone_states()
    eng.process_frame(ramp_frame(n, 255), 0.0)
    assert await eng.flush(0.0) == n
    eng.process_frame(ramp_frame(n, 0), 0.5)
    sent = await eng.flush(0.5)
    assert sent < n and eng.pending() == n - sent == eng.held_back
    assert eng.status()["pending"] == n - sent

    drv.calls.clear()
    result = eng.clear_pending(0.5)
    assert result["dropped"] == n - sent
    assert eng.pending() == 0 and eng.held_back == 0

    # The console keeps sending the same frame: nothing is sent, however long we wait.
    for k in range(1, 100):
        eng.process_frame(ramp_frame(n, 0), 0.5 + k * 0.05)
        assert await eng.flush(0.5 + k * 0.05) == 0
    assert drv.calls == [] and eng.pending() == 0

    # The next change from the source is sent as usual, and so is the one after it. The lights
    # whose cleared update never went out are still at 100 %, so only the others have to move.
    eng.process_frame(ramp_frame(n, 255), 10.0)
    assert eng.pending() == sent
    assert await eng.flush(10.0) > 0
    drv.calls.clear()
    eng.process_frame(ramp_frame(n, 0), 20.0)
    assert await eng.flush(20.0) > 0


async def test_cleared_value_is_sent_again_after_the_source_moves_away_and_back():
    eng, drv = make_engine(auto_assign(["a"]), rate=5.0, zones=["a"])
    eng._init_zone_states()
    eng.process_frame(frame(c1=255), 0.0)
    eng.config.max_commands_per_s = 1.0
    eng._zones["a"].last_send = 0.0  # inside the per-zone interval: it waits
    assert eng.pending() == 1
    assert eng.clear_pending(0.0)["dropped"] == 1
    eng.process_frame(frame(c1=128), 1.0)  # the source moves on...
    eng.process_frame(frame(c1=255), 2.0)  # ...and returns to the cleared value
    assert eng.pending() == 1
    assert await eng.flush(5.0) == 1
    assert drv.calls[-1][1] == {"a": 100.0}


async def test_clear_pending_reports_lights_still_fading_on_the_repeater():
    eng, drv = make_engine(auto_assign(["a", "b"], ["intensity", "fade"]))
    eng._init_zone_states()
    eng.process_frame(frame(c1=255, c2=20, c3=255, c4=20), 1.0)  # 2 s fade channel
    assert await eng.flush(1.0) == 2
    assert eng.clear_pending(1.5) == {"dropped": 0, "fading": 2}
    assert eng.clear_pending(4.0) == {"dropped": 0, "fading": 0}  # the fades are over
