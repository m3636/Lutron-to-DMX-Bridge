"""App configuration, persisted as JSON in the per-user data directory.

Windows: %APPDATA%\\LutronToDMXBridge\\config.json
macOS:   ~/Library/Application Support/LutronToDMXBridge/config.json
Other:   ~/.config/lutron-to-dmx-bridge/config.json

Before the rename (to "Lutron To DMX Bridge") these were ``LutronDMX`` / ``lutron-dmx``; an old folder is moved
to the new name on first start, so existing settings carry over.  ``LUTRON_TO_DMX_BRIDGE_HOME`` overrides the
folder (the old name ``LUTRON_DMX_HOME`` still works).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Folder / environment names used before the app was renamed from "Lutron DMX" to "Lutron To DMX Bridge".
#: The old folder is moved to the new name the first time the app starts (see :func:`data_dir`).
LEGACY_HOME_ENV = "LUTRON_DMX_HOME"


def _default_dir(name_win_mac: str, name_other: str) -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / name_win_mac
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name_win_mac
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / name_other


def _repoint_paths(config_file: Path, old: Path, new: Path) -> None:
    """After the folder move, paths saved in the config that pointed into the old folder (the pairing
    certificate folder, say) are pointed at the new one."""
    try:
        text = config_file.read_text(encoding="utf-8")
        o, n = json.dumps(str(old))[1:-1], json.dumps(str(new))[1:-1]
        if o in text:
            config_file.write_text(text.replace(o, n), encoding="utf-8")
    except OSError as e:
        log.warning("Could not update saved paths in %s: %s", config_file, e)


def data_dir() -> Path:
    override = os.environ.get("LUTRON_TO_DMX_BRIDGE_HOME") or os.environ.get(LEGACY_HOME_ENV)
    if override:
        p = Path(override)
    else:
        p = _default_dir("LutronToDMXBridge", "lutron-to-dmx-bridge")
        legacy = _default_dir("LutronDMX", "lutron-dmx")
        if not p.exists() and legacy.is_dir():
            # Settings, scenes, pairing certificates and backups made before the rename live in the old folder.
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                legacy.rename(p)
                log.info("Moved your settings from %s to %s (the app was renamed).", legacy, p)
                _repoint_paths(p / "config.json", legacy, p)
            except OSError as e:
                log.warning("Could not move %s to %s (%s); using the old folder.", legacy, p, e)
                p = legacy
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
    #: level step in percent: a zone is only sent when its DMX value moved at least this far from the
    #: last command, and above 0.5 the level sent is rounded to a multiple of it (0 and 100 stay exact)
    resolution_pct: float = 0.5
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
