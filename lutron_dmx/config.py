"""App configuration, persisted as JSON in the per-user data directory.

Windows: %APPDATA%\\LutronDMX\\config.json
macOS:   ~/Library/Application Support/LutronDMX/config.json
Other:   ~/.config/lutron-dmx/config.json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("LUTRON_DMX_HOME")
    if override:
        p = Path(override)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home())) / "LutronDMX"
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "LutronDMX"
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lutron-dmx"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class DriverConfig:
    #: "simulator", "ra2" (RadioRA 2 Main Repeater, telnet) or "leap" (RA3 / RA2 Select / Caseta)
    kind: str = "simulator"
    host: str = ""
    #: mDNS name of the processor (e.g. "lutron-01234abc"), remembered so a changed IP can be re-found
    hostname: str = ""
    # RA2 integration login (local only, set in RA2 Essentials/Inclusive software)
    username: str = "lutron"
    password: str = "integration"
    port: int | None = None
    #: RA2: path to the Integration Report (XML from http://<repeater>/DbXmlInfo.xml or CSV export)
    integration_report: str = ""
    #: LEAP: paths to the certificate files written by pairing
    leap_cert_dir: str = ""


@dataclass
class SceneConfig:
    id: str
    name: str
    #: zone_id -> level percent
    levels: dict[str, float] = field(default_factory=dict)
    fade: float | None = None


@dataclass
class ScheduleRef:
    """A schedule remembered by id and by the name it had when it was picked."""

    id: str
    name: str = ""


@dataclass
class ScheduleSetConfig:
    """A named group of schedules to turn off (and optionally on) together, e.g. "Performance mode"."""

    id: str
    name: str
    disable: list[ScheduleRef] = field(default_factory=list)
    enable: list[ScheduleRef] = field(default_factory=list)


@dataclass
class ActiveScheduleEntry:
    """One schedule touched by an active set, with the state to put back on restore."""

    id: str
    name: str
    #: enabled state before the set was activated
    previous: bool
    #: enabled state the set asked for
    desired: bool


@dataclass
class ActiveScheduleSet:
    """Persisted while a set is active so a restart or crash never loses the prior states."""

    set_id: str
    set_name: str
    activated_at: str
    #: only schedules this app actually changed; ones already in the wanted state are not listed
    entries: list[ActiveScheduleEntry] = field(default_factory=list)
    #: human-readable problems from the last activate / restore attempt
    warnings: list[str] = field(default_factory=list)


@dataclass
class DmxFixtureMapping:
    zone_id: str
    #: first DMX channel (1-512)
    address: int
    #: ordered parameter names occupying address, address+1, ...
    #: supported: "intensity", "on_off"
    params: list[str] = field(default_factory=lambda: ["intensity"])
    enabled: bool = True


@dataclass
class DmxConfig:
    enabled: bool = False
    #: "artnet", "sacn", "usb"
    source: str = "artnet"
    universe: int = 0
    bind_ip: str = "0.0.0.0"
    #: serial port for USB input adapters, e.g. "COM3" or "/dev/tty.usbserial-XXXX"
    serial_port: str = ""
    #: max commands per second sent to the Lutron system for each zone
    max_rate_hz: float = 5.0
    #: max commands per second sent to the Lutron system for all zones together (0 = no limit)
    max_commands_per_s: float = 20.0
    mappings: list[DmxFixtureMapping] = field(default_factory=list)


@dataclass
class UpdateConfig:
    """Checking GitHub for a newer release. This is the only call the app makes to the internet."""

    #: look for a newer release on startup and every few hours (turn off for a fully offline app)
    check: bool = True
    #: optional read-only GitHub token, only needed while the repository is private
    token: str = ""
    #: the release the user pressed "Dismiss" on; the banner returns for anything newer
    dismissed_version: str = ""


@dataclass
class AppConfig:
    driver: DriverConfig = field(default_factory=DriverConfig)
    scenes: list[SceneConfig] = field(default_factory=list)
    dmx: DmxConfig = field(default_factory=DmxConfig)
    schedule_sets: list[ScheduleSetConfig] = field(default_factory=list)
    active_schedule_set: ActiveScheduleSet | None = None
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    # -- persistence -----------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or cls.path()
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def save(self, path: Path | None = None) -> None:
        path = path or self.path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "AppConfig":
        dmx_raw = dict(raw.get("dmx", {}))
        mappings = [DmxFixtureMapping(**m) for m in dmx_raw.pop("mappings", [])]
        active_raw = raw.get("active_schedule_set")
        active = None
        if active_raw:
            active = ActiveScheduleSet(
                **{**active_raw, "entries": [ActiveScheduleEntry(**e) for e in active_raw.get("entries", [])]})
        sets = [
            ScheduleSetConfig(
                id=s["id"], name=s["name"],
                disable=[ScheduleRef(**r) for r in s.get("disable", [])],
                enable=[ScheduleRef(**r) for r in s.get("enable", [])])
            for s in raw.get("schedule_sets", [])
        ]
        return cls(
            driver=DriverConfig(**raw.get("driver", {})),
            scenes=[SceneConfig(**s) for s in raw.get("scenes", [])],
            dmx=DmxConfig(**dmx_raw, mappings=mappings),
            schedule_sets=sets,
            active_schedule_set=active,
            updates=UpdateConfig(**{k: v for k, v in raw.get("updates", {}).items()
                                    if k in UpdateConfig.__dataclass_fields__}),
            web_host=raw.get("web_host", "127.0.0.1"),
            web_port=raw.get("web_port", 8080),
        )
