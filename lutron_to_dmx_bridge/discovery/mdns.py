"""mDNS (Bonjour) discovery.

RadioRA 3, RA2 Select, Caseta and HomeWorks QS(X) processors advertise
``_lutron._tcp.local.`` with a ``SYSTYPE`` TXT record and a ``lutron-XXXXXXXX``
hostname (this is what the Lutron phone apps, Home Assistant and
pylutron-caseta's ``leap-scan`` all use). A classic RadioRA 2 Main Repeater is
not known to advertise this way; the TCP scan covers it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from .models import KIND_LEAP, DiscoveredDevice
from .netinfo import list_local_networks, mdns_interface_addresses

log = logging.getLogger(__name__)

SERVICE_TYPES = ("_lutron._tcp.local.", "_leap._tcp.local.")
DEFAULT_PORT = 8081
#: after the first hit keep listening this long for other controllers
GRACE = 1.0

#: SYSTYPE prefix (lower-case) -> display name; from Home Assistant's lutron_caseta manifest
_SYSTEMS = (
    ("radiora3", "RadioRA 3"),
    ("ra2select", "RadioRA 2 Select"),
    ("smartbridge", "Caseta Smart Bridge"),
    ("hwqs", "HomeWorks QS"),
)


def system_from_systype(systype: str) -> str:
    s = (systype or "").lower()
    for prefix, label in _SYSTEMS:
        if s.startswith(prefix):
            return label
    return ""


def device_from_service(name: str, addresses: Iterable[str], port: int | None,
                        properties: dict) -> DiscoveredDevice | None:
    """Turn a resolved mDNS service into a result (pure, for tests)."""
    props = {}
    for k, v in (properties or {}).items():
        k = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
        v = v.decode("utf-8", "replace") if isinstance(v, bytes) else ("" if v is None else str(v))
        props[k] = v
    hosts = [a for a in addresses if a and ":" not in a]  # IPv4 only
    if not hosts:
        return None
    hostname = name.split("._", 1)[0]  # "Lutron-01234ABC._lutron._tcp.local." -> "Lutron-01234ABC"
    system = system_from_systype(props.get("SYSTYPE", ""))
    lutron_name = hostname.lower().startswith("lutron-")
    confidence = 1.0 if (system and lutron_name) else 0.9 if (system or lutron_name) else 0.6
    return DiscoveredDevice(
        host=hosts[0], port=port or DEFAULT_PORT, kind=KIND_LEAP, method="mdns",
        system=system, name=hostname.lower(), properties=props, confidence=confidence,
    )


async def discover_mdns(
    timeout: float = 4.0,
    service_types: Iterable[str] = SERVICE_TYPES,
    interfaces: list[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
    grace: float = GRACE,
) -> list[DiscoveredDevice]:
    from zeroconf import IPVersion, InterfaceChoice, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    if interfaces is None:
        interfaces = mdns_interface_addresses(list_local_networks())
    choice = interfaces or InterfaceChoice.Default
    found: dict[str, DiscoveredDevice] = {}
    first_hit = asyncio.Event()
    tasks: set[asyncio.Task] = set()
    types = list(service_types)

    try:
        az = AsyncZeroconf(interfaces=choice, ip_version=IPVersion.V4Only)
    except Exception as exc:
        log.warning("mDNS unavailable: %s", exc)
        return []

    async def resolve(zc, stype: str, name: str) -> None:
        info = AsyncServiceInfo(stype, name)
        try:
            if not await info.async_request(zc, 3000):
                return
        except Exception:
            log.debug("mDNS resolve failed for %s", name, exc_info=True)
            return
        dev = device_from_service(name, info.parsed_addresses(), info.port, info.properties)
        if dev is not None and dev.host not in found:
            found[dev.host] = dev
            if on_progress:
                on_progress(f"mDNS: found {dev.system or 'Lutron device'} at {dev.host}")
            first_hit.set()

    def handler(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        task = asyncio.ensure_future(resolve(zeroconf, service_type, name))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    browser = AsyncServiceBrowser(az.zeroconf, types, handlers=[handler])
    try:
        try:
            await asyncio.wait_for(first_hit.wait(), timeout)
            await asyncio.sleep(min(grace, timeout))  # let a second controller answer too
        except asyncio.TimeoutError:
            pass
        if tasks:
            await asyncio.wait(set(tasks), timeout=3.0)
    finally:
        for t in list(tasks):
            t.cancel()
        await browser.async_cancel()
        await az.async_close()
    return list(found.values())
