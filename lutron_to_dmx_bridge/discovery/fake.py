"""Canned results for `lutron-to-dmx-bridge --simulator`, so the GUI flow can be tried without a LAN."""

from __future__ import annotations

import asyncio

from .models import KIND_LEAP, KIND_RA2, DiscoveredDevice


async def fake_discover(*, scan="auto", timeout: float = 4.0, on_progress=None, **_) -> list[DiscoveredDevice]:
    await asyncio.sleep(0.8)
    out = [DiscoveredDevice(host="192.168.1.60", port=8081, kind=KIND_LEAP, method="mdns",
                            system="RadioRA 3 (demo)", name="lutron-0123abcd", confidence=1.0,
                            simulated=True)]
    if scan is True:
        out.append(DiscoveredDevice(host="192.168.1.50", port=23, kind=KIND_RA2, method="scan",
                                    system="RadioRA 2 Main Repeater (demo)", confidence=0.85,
                                    simulated=True))
    return out
