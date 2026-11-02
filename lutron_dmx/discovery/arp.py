"""ARP-table hints: a MAC address in Lutron's range raises confidence in a candidate.

Only an extra hint: Lutron has more than one OUI and the table only contains
hosts we have recently talked to.
"""

from __future__ import annotations

import asyncio
import re
import sys

#: Lutron Electronics Co., Inc. (the only prefix registered under that name that we could confirm)
LUTRON_OUIS = frozenset({"00:0f:e7"})

# Windows: "  192.168.1.1   aa-bb-cc-dd-ee-ff   dynamic"
# macOS/Linux: "? (192.168.1.20) at 0:f:e7:1:2:3 on en0 ifscope [ethernet]"
_ENTRY = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})\)?\s+(?:at\s+)?"
    r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})(?![0-9a-fA-F:-])"
)


def normalize_mac(mac: str) -> str:
    """'0:f:E7:1:2:3' / 'AA-BB-...' -> 'aa:bb:..' (macOS drops leading zeros)."""
    return ":".join(f"{int(p, 16):02x}" for p in re.split(r"[:-]", mac.strip()))


def parse_arp(text: str) -> dict[str, str]:
    """ip -> normalized mac from `arp -a` / `arp -an` output."""
    table: dict[str, str] = {}
    for ip, mac in _ENTRY.findall(text or ""):
        mac = normalize_mac(mac)
        first = int(mac[:2], 16)
        if mac == "ff:ff:ff:ff:ff:ff" or first & 1:  # broadcast / multicast
            continue
        table[ip] = mac
    return table


def is_lutron_oui(mac: str) -> bool:
    return bool(mac) and normalize_mac(mac)[:8] in LUTRON_OUIS


async def read_arp_table(timeout: float = 5.0) -> dict[str, str]:
    args = ["arp", "-a"] if sys.platform == "win32" else ["arp", "-an"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return {}
    return parse_arp(out.decode("utf-8", "replace"))
