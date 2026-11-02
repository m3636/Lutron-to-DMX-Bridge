"""System-agnostic data model shared by every driver, the web GUI and DMX."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ZoneType(str, Enum):
    DIMMER = "dimmer"
    SWITCH = "switch"
    SHADE = "shade"
    FAN = "fan"


@dataclass
class Area:
    id: str
    name: str


@dataclass
class Zone:
    """A controllable output (light, switch, shade). Level is 0-100 percent."""

    id: str
    name: str
    area_id: str | None = None
    type: ZoneType = ZoneType.DIMMER
    level: float = 0.0

    @property
    def is_on(self) -> bool:
        return self.level > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["is_on"] = self.is_on
        return d


@dataclass
class Keypad:
    """A keypad / control station. Read-only in this app."""

    id: str
    name: str
    area_id: str | None = None
    buttons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Schedule:
    """A Lutron timeclock event / schedule. Only `enabled` can be changed."""

    id: str
    name: str
    enabled: bool = True
    description: str = ""
    timeclock_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemInventory:
    areas: list[Area] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    keypads: list[Keypad] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "areas": [asdict(a) for a in self.areas],
            "zones": [z.to_dict() for z in self.zones],
            "keypads": [k.to_dict() for k in self.keypads],
            "schedules": [s.to_dict() for s in self.schedules],
        }
