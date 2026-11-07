"""Replay a DMX storm against the simulator's modeled repeater, with and without protection.

A full-house triangle sweep (what a console chase or a QLC+ scene fade sends) runs for
``--seconds`` against a repeater that takes ``--service-ms`` per command. The numbers are exact:
the run is in virtual time, so a minute takes a fraction of a second.

    python tools/simulate_storm.py                       # 82 lights, repeater does 10 commands/s
    python tools/simulate_storm.py --service-ms 50 --seconds 120

"After the app closes" is how long the repeater needs to finish what it was handed once nothing
more is sent: the time the lights keep moving by themselves.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_protect import Rig, sweep  # noqa: E402


async def run(zones: int, service_ms: float, seconds: float, protect: bool, budget: float) -> dict:
    rig = await Rig(zones=zones, service_ms=service_ms, protect=protect, max_commands_per_s=budget).start()
    await rig.run(seconds, sweep(rig.n))
    sent, executed = rig.eng.commands_sent, rig.sim.executed
    depth = rig.sim.queue_depth()
    tail = await rig.drain()
    return {"peak": rig.peak, "latency": rig.peak_latency / 1000, "sent": sent,
            "per_s": executed / seconds, "left": depth, "tail": tail,
            "rate": rig.eng.protector.rate if protect else None}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--zones", type=int, default=82)
    ap.add_argument("--service-ms", type=float, default=100.0, help="repeater time per command")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--budget", type=float, default=20.0, help="max_commands_per_s")
    a = ap.parse_args()
    cap = 1000 / a.service_ms
    print(f"{a.zones} lights, {a.seconds:g} s sweep, limit {a.budget:g} commands/s, "
          f"repeater takes {a.service_ms:g} ms per command ({cap:.0f}/s)\n")
    print(f"{'':14}{'sent':>7}{'carried out/s':>15}{'deepest backlog':>17}{'worst lag':>11}"
          f"{'still queued when':>19}{'lights keep':>13}")
    print(f"{'':14}{'':>7}{'':>15}{'(commands)':>17}{'(s)':>11}{'the sweep ends':>19}{'moving (s)':>13}")
    for protect in (False, True):
        r = await run(a.zones, a.service_ms, a.seconds, protect, a.budget)
        print(f"{'protected' if protect else 'unprotected':14}{r['sent']:>7}{r['per_s']:>15.1f}{r['peak']:>17}"
              f"{r['latency']:>11.1f}{r['left']:>19}{r['tail']:>13.1f}")


if __name__ == "__main__":
    asyncio.run(main())
