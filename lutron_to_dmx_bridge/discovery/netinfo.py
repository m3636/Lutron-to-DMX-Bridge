"""Which local IPv4 networks are worth probing."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: adapter names (lower-cased substrings / regexes) that are virtual or tunnels, not the home LAN
_VIRTUAL = re.compile(
    r"vethernet|wsl|hyper-v|vmware|vmnet|virtualbox|vboxnet|docker|veth|br-|virbr|"
    r"tap-|\btap\b|\btun\d|utun|tailscale|zerotier|wireguard|\bwg\d|vpn|loopback|"
    r"bluetooth|awdl|llw|bridge\d|anpi|\bap\d",
    re.IGNORECASE,
)

#: never scan a bigger network than this around our own address
MAX_PREFIX = 24
MAX_HOSTS = 1024


def is_virtual_adapter(name: str) -> bool:
    return bool(_VIRTUAL.search(name or ""))


@dataclass(frozen=True)
class LocalNetwork:
    adapter: str
    address: ipaddress.IPv4Address
    #: the network to scan: the adapter's own network, trimmed to the /24 around our address
    network: ipaddress.IPv4Network


def _default_adapters() -> Iterable:
    import ifaddr

    return ifaddr.get_adapters()


def list_local_networks(adapters: Callable[[], Iterable] | None = None) -> list[LocalNetwork]:
    """Private, non-virtual IPv4 networks this machine is on (deduplicated).

    `adapters` is injectable for tests; it returns ifaddr-style adapters
    (``.nice_name``/``.name`` and ``.ips`` with ``.ip`` / ``.network_prefix``).
    """
    try:
        found = list((adapters or _default_adapters)())
    except Exception:
        return []
    out: list[LocalNetwork] = []
    seen: set[ipaddress.IPv4Network] = set()
    for adapter in found:
        name = getattr(adapter, "nice_name", "") or getattr(adapter, "name", "")
        if is_virtual_adapter(name) or is_virtual_adapter(getattr(adapter, "name", "")):
            continue
        for ip in getattr(adapter, "ips", []):
            if not isinstance(ip.ip, str):  # IPv6 entries are tuples
                continue
            try:
                addr = ipaddress.IPv4Address(ip.ip)
                prefix = max(int(ip.network_prefix), 8)
            except ValueError:
                continue
            if not addr.is_private or addr.is_loopback or addr.is_link_local:
                continue
            net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
            if net.prefixlen < MAX_PREFIX:
                net = ipaddress.ip_network(f"{addr}/{MAX_PREFIX}", strict=False)
            if net in seen:
                continue
            seen.add(net)
            out.append(LocalNetwork(name, addr, net))
    return out


def mdns_interface_addresses(networks: list[LocalNetwork]) -> list[str]:
    return sorted({str(n.address) for n in networks})
