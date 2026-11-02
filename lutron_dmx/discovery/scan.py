"""Active fallback: probe the local subnet for Lutron processors.

* TCP 8081 with a TLS handshake -> LEAP (RA3 / RA2 Select / Caseta). We don't
  have a certificate yet, so we only look at the server certificate's issuer.
* TCP 23 that answers with a ``login:`` / ``GNET>`` banner -> RadioRA 2
  Main Repeater (HomeWorks QS prints ``QNET>``).
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import ssl
from collections.abc import Callable, Iterable

from .models import KIND_LEAP, KIND_RA2, DiscoveredDevice
from .netinfo import MAX_HOSTS, LocalNetwork, list_local_networks

log = logging.getLogger(__name__)

PORTS = (8081, 23)
TIMEOUT = 0.75
CONCURRENCY = 128

#: errno values meaning "the OS refused to talk to the LAN" (e.g. macOS Local Network privacy)
_BLOCKED = {errno.EHOSTUNREACH, errno.EPERM, errno.EACCES}


class ScanBlocked(Exception):
    """Every probe failed the way a blocked-by-OS probe does."""


def _raise_if_blocked(exc: OSError) -> None:
    if not isinstance(exc, ssl.SSLError) and exc.errno in _BLOCKED:  # SSLError.errno 1 is not EPERM
        raise exc


async def probe_leap(host: str, port: int, timeout: float) -> DiscoveredDevice | None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, ssl_handshake_timeout=timeout * 2),
            timeout * 3)
    except (ssl.SSLError, asyncio.TimeoutError):
        return None
    except OSError as exc:
        _raise_if_blocked(exc)
        return None
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True) or b""
    except Exception:
        der = b""
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except Exception:
            pass
    # The issuer / subject are plain strings inside the DER; no X.509 parser needed.
    lutron = b"Lutron" in der
    return DiscoveredDevice(
        host=host, port=port, kind=KIND_LEAP, method="scan",
        system="Lutron LEAP processor" if lutron else "",
        confidence=0.9 if lutron else 0.4,
    )


def is_ra2_banner(data: bytes) -> bool:
    text = data.decode("latin-1", "replace").lower()
    return "login:" in text or "gnet>" in text or "qnet>" in text


async def probe_ra2(host: str, port: int, timeout: float) -> DiscoveredDevice | None:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return None
    except OSError as exc:
        _raise_if_blocked(exc)
        return None
    try:
        data = b""
        try:
            # telnet option negotiation (IAC ...) may come first; read for a short while
            data = await asyncio.wait_for(reader.read(256), timeout * 2)
            if not is_ra2_banner(data):
                data += await asyncio.wait_for(reader.read(256), timeout)
        except (OSError, asyncio.TimeoutError):
            pass
    finally:
        writer.close()
    if not is_ra2_banner(data):
        return None
    return DiscoveredDevice(host=host, port=port, kind=KIND_RA2, method="scan",
                            system="RadioRA 2 Main Repeater", confidence=0.85)


_PROBES = {8081: probe_leap, 23: probe_ra2}


def candidate_hosts(networks: Iterable[ipaddress.IPv4Network | LocalNetwork],
                    max_hosts: int = MAX_HOSTS) -> list[str]:
    hosts: list[str] = []
    own: set[ipaddress.IPv4Address] = set()
    seen: set[str] = set()
    for n in networks:
        if isinstance(n, LocalNetwork):
            own.add(n.address)
            n = n.network
        for h in n.hosts():
            s = str(h)
            if s not in seen and h not in own:
                seen.add(s)
                hosts.append(s)
    return hosts[:max_hosts]


async def scan_subnets(
    networks: Iterable[ipaddress.IPv4Network | LocalNetwork] | None = None,
    ports: Iterable[int] = PORTS,
    timeout: float = TIMEOUT,
    concurrency: int = CONCURRENCY,
    max_hosts: int = MAX_HOSTS,
    on_progress: Callable[[str], None] | None = None,
    probes: dict | None = None,
) -> list[DiscoveredDevice]:
    """Probe every host in `networks` (default: this machine's LAN) on `ports`."""
    if networks is None:
        networks = list_local_networks()
    nets = list(networks)
    hosts = candidate_hosts(nets, max_hosts)
    ports = tuple(ports)
    probes = probes or _PROBES
    sem = asyncio.Semaphore(concurrency)
    found: list[DiscoveredDevice] = []
    blocked = 0
    attempts = 0
    if on_progress:
        on_progress(f"Scanning {len(hosts)} addresses on {', '.join(str(getattr(n, 'network', n)) for n in nets)}")

    async def one(host: str, port: int) -> None:
        nonlocal blocked, attempts
        async with sem:
            attempts += 1
            try:
                dev = await probes[port](host, port, timeout)
            except OSError:
                blocked += 1
                return
            except Exception:
                log.debug("probe %s:%s failed", host, port, exc_info=True)
                return
            if dev is not None:
                found.append(dev)
                if on_progress:
                    on_progress(f"Scan: {dev.system or 'device'} at {host}:{port}")

    await asyncio.gather(*(one(h, p) for h in hosts for p in ports if p in probes))
    if not found and attempts and blocked == attempts:
        raise ScanBlocked(
            "The operating system blocked access to the local network. On macOS allow "
            "Local Network access for this app in System Settings > Privacy & Security.")
    return found
