"""Find a Lutron processor on the local network without needing its IP address.

    devices = await discover()            # mDNS first, then a subnet scan if nothing answered

See ``mdns`` (RA3 / RA2 Select / Caseta / HomeWorks QS advertise over Bonjour),
``scan`` (TCP probe; the only way to find a classic RadioRA 2 Main Repeater)
and ``arp`` (MAC-address hint).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import arp
from .models import KIND_LEAP, KIND_RA2, DiscoveredDevice

__all__ = ["discover", "rediscover_host", "DiscoveredDevice", "KIND_LEAP", "KIND_RA2"]

log = logging.getLogger(__name__)


def merge(devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """One result per (host, kind); mDNS details win, methods are combined."""
    merged: dict[tuple[str, str], DiscoveredDevice] = {}
    for d in devices:
        key = (d.host, d.kind)
        old = merged.get(key)
        if old is None:
            merged[key] = d
            continue
        first, second = (old, d) if old.method == "mdns" else (d, old)
        merged[key] = DiscoveredDevice(
            host=d.host, port=first.port, kind=d.kind,
            method="+".join(sorted({*first.method.split("+"), *second.method.split("+")})),
            system=first.system or second.system, name=first.name or second.name,
            mac=first.mac or second.mac, properties={**second.properties, **first.properties},
            confidence=max(first.confidence, second.confidence),
        )
    return list(merged.values())


def add_arp_hints(devices: list[DiscoveredDevice], table: dict[str, str]) -> list[DiscoveredDevice]:
    out = []
    for d in devices:
        mac = table.get(d.host, d.mac)
        conf = d.confidence
        if mac and arp.is_lutron_oui(mac):
            conf = min(1.0, conf + 0.1)
        out.append(DiscoveredDevice(**{**d.__dict__, "mac": mac, "confidence": conf}))
    return out


async def discover(
    *,
    scan: bool | str = "auto",
    timeout: float = 4.0,
    on_progress: Callable[[str], None] | None = None,
    mdns_fn=None,
    scan_fn=None,
    arp_fn=None,
) -> list[DiscoveredDevice]:
    """Return candidate Lutron processors, most likely first.

    scan: "auto" scans the subnet only when mDNS found nothing, True always scans,
    False never does. The *_fn arguments replace the real methods in tests.
    """
    from .mdns import discover_mdns
    from .scan import ScanBlocked, scan_subnets

    mdns_fn = mdns_fn or discover_mdns
    scan_fn = scan_fn or scan_subnets
    arp_fn = arp_fn or arp.read_arp_table
    if on_progress:
        on_progress("Listening for Lutron processors (mDNS)...")
    try:
        found = list(await mdns_fn(timeout=timeout, on_progress=on_progress))
    except Exception as exc:
        log.warning("mDNS discovery failed: %s", exc)
        found = []
    blocked: Exception | None = None
    if scan is True or (scan == "auto" and not found):
        try:
            found += await scan_fn(on_progress=on_progress)
        except ScanBlocked as exc:
            blocked = exc
        except Exception as exc:
            log.warning("Subnet scan failed: %s", exc)
    if found:
        found = add_arp_hints(merge(found), await arp_fn())
    elif blocked:
        raise blocked
    return sorted(found, key=lambda d: (-d.confidence, d.host))


async def rediscover_host(name: str, timeout: float = 4.0, mdns_fn=None) -> str | None:
    """New IP for a processor we already know by mDNS hostname, or None."""
    from .mdns import discover_mdns

    if not name:
        return None
    try:
        found = await (mdns_fn or discover_mdns)(timeout=timeout)
    except Exception:
        return None
    wanted = name.lower().rstrip(".")
    for d in found:
        if d.name.lower().rstrip(".") == wanted:
            return d.host
    return None
