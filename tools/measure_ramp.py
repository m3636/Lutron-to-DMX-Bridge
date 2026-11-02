"""Measure how a DMX ramp reaches a RadioRA 2 repeater, without any hardware.

Runs the real DMX engine and the real RA2 driver against a fake repeater that
executes telnet commands one at a time (``--service-ms`` each), feeds the engine
a 10 s linear ramp on N channels at 40 fps (what a QLC+ scene fade sends), and
reports how many commands were sent and how close the simulated lights stayed to
the ideal ramp (a command moves a light linearly to its level over its fade).

    python tools/measure_ramp.py --zones 82 --service-ms 20 [--budget 20]
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_ra2 import FakeRepeater, make_config  # noqa: E402

from lutron_dmx.config import AppConfig, DmxConfig  # noqa: E402
from lutron_dmx.controller import Controller  # noqa: E402
from lutron_dmx.dmx.engine import DmxEngine, auto_assign  # noqa: E402
from lutron_dmx.drivers.ra2 import RA2Driver  # noqa: E402
from lutron_dmx.models import Zone, ZoneType  # noqa: E402

DUR = 10.0


class SlowRepeater(FakeRepeater):
    """Executes #OUTPUT commands one after another, taking `service` seconds each."""

    def __init__(self, service: float) -> None:
        super().__init__()
        self.service = service
        self.stamps: list[tuple[float, str, float, str | None]] = []

    async def handle(self, reader, writer):
        self.writers.append(writer)
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
            if cmd.startswith("#OUTPUT"):
                if self.service:
                    await asyncio.sleep(self.service)
                p = cmd.split(",")
                self.stamps.append((time.monotonic(), p[1], float(p[3]), p[4] if len(p) > 4 else None))
            writer.write(self.reply(cmd).encode())
            await writer.drain()


async def main(n: int, service_ms: float, budget: float | None) -> None:
    rep = SlowRepeater(service_ms / 1000)
    await rep.start()
    cfg = AppConfig()
    cfg.driver = make_config(rep.port)
    drv = RA2Driver(cfg.driver, connect_timeout=2)
    await drv.connect()
    zones = [Zone(id=str(i + 1), name=f"z{i}", type=ZoneType.DIMMER) for i in range(n)]
    drv._inventory.zones = zones
    extra = {} if budget is None else {"max_commands_per_s": budget}
    cfg.dmx = DmxConfig(enabled=True, mappings=auto_assign([z.id for z in zones]), **extra)

    class NoReceiver:
        async def start(self): ...
        async def stop(self): ...

    eng = DmxEngine(Controller(cfg, drv), receiver_factory=lambda e: NoReceiver())
    await eng.start()
    rep.stamps.clear()
    start = time.monotonic()
    while (el := time.monotonic() - start) <= DUR:
        eng.on_frame(bytes([round(255 * el / DUR)]) * n + bytes(512 - n))
        await asyncio.sleep(0.025)
    eng.on_frame(bytes([255]) * n + bytes(512 - n))
    ramp_end = time.monotonic()
    executed_at_end = len(rep.stamps)
    for _ in range(600):  # let the repeater work off whatever is still queued (max 60 s)
        await asyncio.sleep(0.1)
        if len(rep.stamps) >= eng.commands_sent and time.monotonic() - ramp_end > 1:
            break

    st = sorted(rep.stamps)
    track = {z.id: [(0.0, 0.0, 0.0, 0.0)] for z in zones}

    def level(z: str, t: float) -> float:
        tr = track[z]
        ta, a, b, f = tr[bisect.bisect_right([x[0] for x in tr], t) - 1]
        return b if f <= 0 or t >= ta + f else a + (b - a) * (t - ta) / f

    for t, z, lvl, f in st:
        track[z].append((t - start, level(z, t - start), lvl, float(f) if f else 0.0))
    gap = err = 0.0
    t_all = None
    t = 0.0
    while t < DUR + 70:
        ls = [level(z, t) for z in track]
        if t <= DUR:
            gap = max(gap, max(ls) - min(ls))
            err = max(err, max(abs(x - t * 100 / DUR) for x in ls))
        if t_all is None and t > 1 and min(ls) >= 99.5:
            t_all = t
        t += 0.05
    before = executed_at_end
    print(f"{n} zones, repeater takes {service_ms:g} ms per command, "
          f"budget {'default' if budget is None else f'{budget:g}/s'}")
    print(f"  commands sent: {eng.commands_sent} ({eng.commands_sent / DUR:.0f}/s); "
          f"executed by the end of the ramp: {before}; still queued: {eng.commands_sent - before}")
    print(f"  lights differ from each other by up to {gap:.1f} points (of 100), "
          f"from the ideal ramp by up to {err:.1f}")
    print(f"  all lights at 100%: " + (f"{t_all:.1f} s after the start (ideal {DUR:g} s)" if t_all else "never"))
    await eng.stop()
    await drv.close()
    await rep.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--zones", type=int, default=82)
    ap.add_argument("--service-ms", type=float, default=0.0, help="repeater time per command")
    ap.add_argument("--budget", type=float, default=None, help="max_commands_per_s (default: app default)")
    a = ap.parse_args()
    asyncio.run(main(a.zones, a.service_ms, a.budget))
