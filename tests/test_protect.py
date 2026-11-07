"""DMX storm protection: adaptive rate, in-flight window, gate, storm trips and the pause switch.

The scenarios run in virtual time against the simulator's modeled repeater (a first-in
first-out queue that takes ``service_ms`` per command), so a minute-long storm takes
milliseconds and the numbers are exact.
"""

from __future__ import annotations

import pytest

from lutron_to_dmx_bridge.config import AppConfig, DmxConfig, DriverConfig
from lutron_to_dmx_bridge.controller import Controller
from lutron_to_dmx_bridge.dmx import protect as P
from lutron_to_dmx_bridge.dmx.engine import DmxEngine, auto_assign
from lutron_to_dmx_bridge.drivers.simulator import SimulatorDriver
from lutron_to_dmx_bridge.metrics import LinkLoad
from lutron_to_dmx_bridge.models import Zone, ZoneType

DT = 0.025  # 40 frames per second, what a console sends


class VClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class NoReceiver:
    async def start(self): ...
    async def stop(self): ...


class Rig:
    """The real engine and protector driving a simulated Main Repeater, in virtual time."""

    def __init__(self, *, zones: int = 82, service_ms: float = 100.0, **dmx: object) -> None:
        self.clk = VClock()
        self.n = zones
        self.sim = SimulatorDriver(service_ms=service_ms, clock=self.clk)
        self.sim._inventory.zones = [Zone(id=str(i + 1), name=f"z{i + 1}", type=ZoneType.DIMMER)
                                     for i in range(zones)]
        cfg = AppConfig(driver=DriverConfig(kind="simulator"))
        cfg.dmx = DmxConfig(enabled=True, mappings=auto_assign([z.id for z in self.sim.zones()]), **dmx)
        self.ctl = Controller(cfg, self.sim)
        self.eng = DmxEngine(self.ctl, receiver_factory=lambda e: NoReceiver())
        self.eng._clock = self.clk
        self.peak = 0
        self.peak_latency = 0.0

    async def start(self) -> "Rig":
        await self.sim.connect()
        self.eng._init_zone_states()
        self.eng.running = True
        return self

    def frame(self, values) -> bytes:
        f = bytearray(512)
        for i, v in enumerate(values):
            f[i] = max(0, min(255, round(v)))
        return bytes(f)

    async def step(self, values=None) -> None:
        """One 25 ms tick: a DMX frame (if any) arrives, the engine flushes, the repeater works."""
        self.clk.t += DT
        if values is not None:
            self.eng.process_frame(self.frame(values), self.clk.t)
        await self.eng.flush(self.clk.t)
        await self.sim.settle(self.clk.t)
        self.peak = max(self.peak, self.sim.queue_depth())
        lat = self.sim.load().latency_ms
        self.peak_latency = max(self.peak_latency, lat or 0.0)

    async def run(self, seconds: float, source) -> None:
        """Feed `source(t_since_start)` -> list of 0-255 values for `seconds`."""
        t0 = self.clk.t
        for _ in range(round(seconds / DT)):
            await self.step(source(self.clk.t - t0))

    async def drain(self, limit: float = 600.0) -> float:
        """Nobody sends any more (the app was closed). Seconds the repeater needs to finish."""
        t0 = self.clk.t
        while self.sim.queue_depth() and self.clk.t - t0 < limit:
            self.clk.t += 0.1
            await self.sim.settle(self.clk.t)
        return self.clk.t - t0


def sweep(n: int, period: float = 6.0):
    """Every light fading up and down together, a triangle wave: a whole-house QLC+ chase."""
    def source(t: float):
        x = (t % period) / period
        v = 255 * (2 * x if x < 0.5 else 2 - 2 * x)
        return [v] * n
    return source


# ---------------------------------------------------------------------------
# The storm that motivated this: a repeater slower than the command limit
# ---------------------------------------------------------------------------
async def test_without_protection_the_backlog_outlives_the_app():
    # Repeater does 10 commands/s, the fixed limit lets 20/s through: the queue grows without bound.
    rig = await Rig(service_ms=100, protect=False).start()
    await rig.run(60, sweep(rig.n))
    assert rig.peak > 300
    assert await rig.drain() > 30  # lights keep moving for over half a minute after the app is gone


async def test_protection_keeps_the_backlog_small():
    rig = await Rig(service_ms=100, protect=True).start()
    await rig.run(60, sweep(rig.n))
    assert rig.eng.protector.state != "tripped"
    assert rig.peak <= 10  # the in-flight window: rate x latency limit
    assert rig.peak_latency < 1_500  # against minutes of lag in the unprotected run
    # The rate saw-tooths around the repeater's real capacity (10/s minus the probes' share) instead
    # of sitting at the 20/s limit; on average the repeater is kept busy but never buried.
    assert 5 <= rig.sim.executed / 60 <= 10
    assert await rig.drain() < 5


async def test_protection_does_not_slow_a_healthy_repeater():
    rig = await Rig(service_ms=5, protect=True).start()  # 200 commands/s capacity
    await rig.run(30, sweep(rig.n))
    p = rig.eng.protector
    assert p.state == "ok" and p.rate == 20.0 and p.decreases == 0
    assert rig.eng.commands_sent > 20 * 25  # about the 20/s limit, all of it let through


async def test_rate_climbs_back_after_the_repeater_recovers():
    rig = await Rig(service_ms=100, protect=True).start()
    await rig.run(30, sweep(rig.n))
    assert rig.eng.protector.rate < 14
    rig.sim.service = 0.005  # the radio traffic that slowed it is gone
    await rig.run(30, sweep(rig.n))
    assert rig.eng.protector.rate == pytest.approx(20.0)
    assert rig.eng.protector.state == "ok"


async def test_whole_house_cue_is_capped_by_the_window():
    rig = await Rig(service_ms=40, protect=True).start()
    await rig.step([0] * rig.n)
    await rig.step([255] * rig.n)  # snap everything on at once
    assert rig.sim.queue_depth() <= 10  # not all 82 at once
    for _ in range(400):
        await rig.step([255] * rig.n)
    assert rig.peak <= 10
    assert all(z.level == 100.0 for z in rig.sim.zones())  # but every light gets there


async def test_gate_holds_everything_while_the_repeater_is_seconds_behind():
    rig = await Rig(zones=10, service_ms=100, protect=True).start()
    for _ in range(100):  # somebody else (another app, a long fade list) put 10 s of work ahead of us
        rig.sim._enqueue("out", "1", 50.0)
    for _ in range(int(4.5 / DT)):  # the first probe has to wait its turn before anyone can tell
        await rig.step([255] * 10)
    assert rig.eng.protector.gate_closed and rig.eng.protector.state == "hold"
    sent = rig.eng.commands_sent
    for _ in range(40):  # a second of DMX while the backlog is still ahead of us
        await rig.step([255] * 10)
    assert rig.eng.commands_sent == sent  # nothing more was added to it
    assert rig.eng.held_back > 0
    for _ in range(600):
        await rig.step([255] * 10)
    assert not rig.eng.protector.gate_closed
    assert all(z.level == 100.0 for z in rig.sim.zones())


# ---------------------------------------------------------------------------
# Storm detection and the pause switch
# ---------------------------------------------------------------------------
async def test_a_frame_flood_pauses_the_output():
    rig = await Rig(zones=4, service_ms=5, protect=True).start()
    for _ in range(int(3 / 0.004)):  # 250 frames per second
        rig.clk.t += 0.004
        rig.eng.process_frame(rig.frame([100, 0, 0, 0]), rig.clk.t)
        await rig.eng.flush(rig.clk.t)
        if rig.eng.paused:
            break
    p = rig.eng.protector
    assert rig.eng.paused and p.auto and p.state == "tripped"
    assert "frames per second" in p.reason


async def test_a_reversal_storm_pauses_the_output():
    rig = await Rig(service_ms=5, protect=True).start()
    flip = [0]

    def noise(t: float):  # every light jumps between 10 % and 90 % every frame
        flip[0] ^= 1
        return [230 if flip[0] else 25] * rig.n

    await rig.run(10, noise)
    p = rig.eng.protector
    assert p.state == "tripped" and "reverse direction" in p.reason
    sent = rig.eng.commands_sent
    await rig.run(5, noise)
    assert rig.eng.commands_sent == sent  # nothing more goes out until somebody resumes


async def test_a_normal_chase_is_not_a_storm():
    rig = await Rig(service_ms=5, protect=True).start()
    await rig.run(30, sweep(rig.n, period=2.0))  # a quick 2 s up-down on every light
    assert not rig.eng.paused


async def test_a_repeater_that_never_recovers_pauses_the_output():
    rig = await Rig(zones=8, service_ms=4_000, protect=True).start()  # one command per 4 s
    await rig.run(60, sweep(8))
    p = rig.eng.protector
    assert p.state == "tripped" and "has not recovered" in p.reason


async def test_manual_pause_stops_writing_and_resume_eases_back_in():
    rig = await Rig(service_ms=5, protect=True).start()
    await rig.run(5, sweep(rig.n))
    await rig.eng.pause()
    assert rig.eng.protector.state == "paused" and not rig.eng.protector.auto
    sent = rig.eng.commands_sent
    await rig.run(5, sweep(rig.n))  # the console keeps going
    assert rig.eng.commands_sent == sent
    assert rig.eng.status()["paused"] is True
    rig.eng.resume()
    assert rig.eng.protector.rate == P.RESUME_RATE
    await rig.step(sweep(rig.n)(3.0))
    assert rig.eng.commands_sent > sent  # the current DMX state goes out again
    assert rig.eng.commands_sent - sent <= P.RESUME_RATE + 1  # but not in one burst


async def test_pausing_a_repeater_that_is_behind_discards_what_is_on_the_wire():
    rig = await Rig(service_ms=100, protect=False).start()  # no protection: a backlog builds
    await rig.run(30, sweep(rig.n))
    before = rig.sim.queue_depth()
    assert before > 100
    await rig.eng.pause("test")
    assert rig.sim.queue_depth() <= rig.sim.WIRE_AFTER
    assert rig.sim.reset_dropped == before - rig.sim.queue_depth()


async def test_pause_works_with_protection_switched_off():
    rig = await Rig(service_ms=5, protect=False).start()
    await rig.run(3, sweep(rig.n))
    await rig.eng.pause()
    sent = rig.eng.commands_sent
    await rig.run(3, sweep(rig.n))
    assert rig.eng.commands_sent == sent


async def test_protect_off_keeps_the_old_fixed_limit():
    rig = await Rig(service_ms=5, protect=False).start()
    await rig.run(10, sweep(rig.n))
    assert rig.eng.protector.rate is None  # never consulted
    assert rig.eng._budget() == 20.0


# ---------------------------------------------------------------------------
# The controller on its own
# ---------------------------------------------------------------------------
class Cfg:
    max_commands_per_s = 20.0
    latency_limit_ms = 500.0
    max_inflight = 40


def test_rate_is_halved_once_per_queue_drain_time():
    p = P.Protector()
    cfg = Cfg()
    p.update(0.0, LinkLoad(100, 0), cfg)
    assert p.rate == 20.0
    p.update(1.0, LinkLoad(900, 5), cfg)
    assert p.rate == 10.0
    p.update(1.2, LinkLoad(950, 5), cfg)  # same slow reading, still about the old rate
    assert p.rate == 10.0
    p.update(2.3, LinkLoad(950, 5), cfg)
    assert p.rate == 5.0
    for i in range(20):
        p.update(3.5 + i * 2, LinkLoad(4000, 5), cfg)
    assert p.rate == P.MIN_RATE  # never below the floor


def test_rate_grows_linearly_while_the_repeater_is_quick():
    p = P.Protector()
    cfg = Cfg()
    p.update(0.0, LinkLoad(50, 0), cfg)
    p.update(0.1, LinkLoad(900, 0), cfg)
    assert p.rate == 10.0
    p.update(1.2, LinkLoad(50, 0), cfg)
    start = p.rate
    for i in range(1, 11):
        p.update(1.2 + i * 0.1, LinkLoad(50, 0), cfg)
    assert p.rate == pytest.approx(start + P.INCREASE_PER_S * 1.0)
    # between "quick" and "slow" the rate holds
    held = p.rate
    p.update(3.0, LinkLoad(400, 0), cfg)
    assert p.rate == held


def test_unknown_latency_is_not_treated_as_slow():
    p = P.Protector()
    p.update(0.0, LinkLoad(None, 0), Cfg())
    p.update(5.0, LinkLoad(None, 0), Cfg())
    assert p.rate == 20.0 and p.state == "ok"


def test_unlimited_budget_still_gets_a_ceiling():
    p = P.Protector()

    class Unlimited(Cfg):
        max_commands_per_s = 0.0

    p.update(0.0, LinkLoad(10, 0), Unlimited())
    assert p.effective_rate(0.0) == P.UNLIMITED_CEILING
    p.update(1.0, LinkLoad(2000, 0), Unlimited())
    assert p.effective_rate(0.0) == P.UNLIMITED_CEILING / 2


def test_inflight_cap_counts_as_congestion_and_limits_headroom():
    p = P.Protector()
    cfg = Cfg()
    p.update(0.0, LinkLoad(10, 0), cfg)
    assert p.window(cfg) == 10  # 20 commands/s x 0.5 s
    assert p.headroom(LinkLoad(10, 7), cfg) == 3
    assert p.headroom(LinkLoad(10, 80), cfg) == 0
    cfg.max_inflight = 6
    assert p.window(cfg) == 6  # the hard cap wins
    cfg.max_inflight = 0
    assert p.headroom(LinkLoad(10, 80), cfg) is None
    cfg.max_inflight = 40
    p.update(1.0, LinkLoad(10, 40), cfg)  # latency fine, but the window is full
    assert p.congested and p.rate == 10.0


def test_gate_has_hysteresis():
    p = P.Protector()
    cfg = Cfg()
    p.update(0.0, LinkLoad(100, 0), cfg)
    p.update(1.0, LinkLoad(3500, 0), cfg)
    assert p.gate_closed and p.state == "hold"
    p.update(2.0, LinkLoad(1500, 0), cfg)  # better, but not under 2 x the limit yet
    assert p.gate_closed
    p.update(3.0, LinkLoad(800, 0), cfg)
    assert not p.gate_closed
