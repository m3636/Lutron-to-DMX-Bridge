"""Result type shared by the discovery methods."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: driver kinds a result can suggest (matches DriverConfig.kind)
KIND_LEAP = "leap"
KIND_RA2 = "ra2"


@dataclass(frozen=True)
class DiscoveredDevice:
    host: str
    port: int
    #: suggested DriverConfig.kind: "leap" (RA3 / RA2 Select / Caseta) or "ra2" (Main Repeater)
    kind: str
    #: "mdns", "scan" (or "mdns+scan" once merged)
    method: str
    #: human-readable system, e.g. "RadioRA 3"
    system: str = ""
    #: mDNS hostname, e.g. "lutron-01234abc.local."; used to re-find the device if its IP changes
    name: str = ""
    mac: str = ""
    properties: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    #: 0..1, how sure we are that this is a Lutron processor
    confidence: float = 0.5
    simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
