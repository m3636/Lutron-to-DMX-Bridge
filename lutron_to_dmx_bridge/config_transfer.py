"""Export and import of the app's settings as one versioned JSON file.

The file holds everything needed to rebuild the app on a fresh install: the Lutron connection, the DMX
mapping and input settings, the custom scenes, the schedule sets ("Performance mode") and the other
persisted settings.  It is a different thing from the Lutron *system* backup (``GET /api/backup``), which
snapshots what the Lutron processor knows.

File layout (``schema_version`` 1)::

    {"format": "lutron-to-dmx-bridge-config", "schema_version": 1, "app_version": "0.4.2",
     "created": "...", "includes_credentials": false,
     "config": {"driver": {...}, "dmx": {...}, "scenes": [...], "schedule_sets": [...],
                "active_schedule_set": null, "updates": {...}, "web_host": "...", "web_port": 8080},
     "files": {"leap_certs": {"caseta.key": "<base64>", ...}}}      # only with credentials

**Credentials are opt-in.**  Without ``include_credentials`` the integration login (user name and password),
the GitHub update token and the LEAP pairing certificates are left out, and an import keeps the ones the app
already has.  With it the file is as sensitive as the app's own ``config.json``.

**Versions.**  ``schema_version`` is bumped whenever the layout changes in a way an older reader would
misunderstand.  A file from a *newer* schema is refused (it is not guessed at); an older one is upgraded
step by step through :data:`MIGRATIONS`.  ``app_version`` is informational: a file from a newer app than the
running one is accepted with a warning, because settings added since are then ignored.

**Import** is two steps for the caller: :func:`parse_document` validates the file and
:func:`describe_changes` says what would change; :func:`import_into_config` then saves a backup of the current
settings and replaces the chosen sections.  Nothing is changed when validation fails.
"""

from __future__ import annotations

import base64
import binascii
import copy
import dataclasses
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from . import __version__
from .config import (DMX_PARAMS, DMX_SOURCES, DRIVER_KINDS, ActiveScheduleEntry, ActiveScheduleSet, AppConfig,
                     DmxConfig, DmxFixtureMapping, DriverConfig, SceneConfig, ScheduleRef, ScheduleSetConfig,
                     data_dir)
from .updater import parse_version

log = logging.getLogger(__name__)

FORMAT = "lutron-to-dmx-bridge-config"
SCHEMA_VERSION = 1
#: The Lutron-system backup written by ``GET /api/backup``; its ``app_config`` part can be imported too.
BACKUP_FORMAT = "lutron-to-dmx-bridge-backup"

#: schema version N -> function turning a document of version N into one of version N + 1 (none needed yet)
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}

#: section id -> label shown to the user
SECTIONS: dict[str, str] = {
    "driver": "Lutron connection",
    "dmx": "DMX mapping and input settings",
    "scenes": "Custom scenes",
    "schedule_sets": "Schedule sets",
    "updates": "Update checks",
    "web": "Web GUI address and port",
}
#: Sections an import selects unless told otherwise.  The web address is left out: it decides who on the
#: network can reach the app, so it is never changed by accident.
DEFAULT_SECTIONS = ("driver", "dmx", "scenes", "schedule_sets", "updates")

#: The only files read from / written to a LEAP certificate folder (see ``drivers.leap``).
MAX_CERT_FILE_BYTES = 64 * 1024
RESTORED_CERT_SUBDIR = ("leap", "restored")


class ConfigImportError(Exception):
    """A file that cannot be imported, or an import that must not run.  ``status`` is the HTTP status to use."""

    def __init__(self, message: str, problems: list[str] | None = None, status: int = 422) -> None:
        super().__init__(message)
        self.problems = problems or []
        self.status = status

    def __str__(self) -> str:
        text = super().__str__()
        if not self.problems:
            return text
        more = f" (+{len(self.problems) - 5} more)" if len(self.problems) > 5 else ""
        return f"{text}: {'; '.join(self.problems[:5])}{more}"


# ---------------------------------------------------------------------------
# validation models (strict types: "5" is not a port, true is not a number)
# ---------------------------------------------------------------------------
class _M(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class _Driver(_M):
    kind: str
    host: str = ""
    hostname: str = ""
    username: str | None = None
    password: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    integration_report: str = ""
    leap_cert_dir: str = ""

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in DRIVER_KINDS:
            raise ValueError(f"must be one of {', '.join(DRIVER_KINDS)}")
        return v

    @model_validator(mode="after")
    def _host(self) -> "_Driver":
        if self.kind in ("ra2", "leap") and not self.host.strip():
            raise ValueError("a host / IP address is required for this system type")
        return self


class _Mapping(_M):
    zone_id: str = Field(min_length=1)
    address: int = Field(ge=1, le=512)
    params: list[str] = Field(min_length=1, max_length=8)
    enabled: bool = True

    @field_validator("params")
    @classmethod
    def _params(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in DMX_PARAMS]
        if bad:
            raise ValueError(f"unsupported DMX parameter(s): {', '.join(bad)}")
        return v

    @model_validator(mode="after")
    def _fits(self) -> "_Mapping":
        if self.address + len(self.params) - 1 > 512:
            raise ValueError(f"zone {self.zone_id!r} runs past channel 512")
        return self


class _Dmx(_M):
    enabled: bool = False
    source: str = "artnet"
    universe: int = Field(default=0, ge=0, le=32767)
    bind_ip: str = "0.0.0.0"
    serial_port: str = ""
    max_rate_hz: float = Field(default=5.0, gt=0, le=50)
    max_commands_per_s: float = Field(default=20.0, ge=0, le=1000)
    resolution_pct: float = Field(default=0.5, ge=0.1, le=25)
    protect: bool = True
    latency_limit_ms: float = Field(default=500.0, ge=50, le=10000)
    max_inflight: int = Field(default=40, ge=0, le=500)
    mappings: list[_Mapping] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v not in DMX_SOURCES:
            raise ValueError(f"must be one of {', '.join(DMX_SOURCES)}")
        return v


class _Scene(_M):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    levels: dict[str, float] = Field(default_factory=dict)
    fade: float | None = Field(default=None, ge=0, le=3600)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [z for z, lvl in v.items() if not 0 <= lvl <= 100]
        if bad:
            raise ValueError(f"levels must be 0-100 (zone {bad[0]!r})")
        return v


class _Ref(_M):
    id: str = Field(min_length=1)
    name: str = ""


class _ScheduleSet(_M):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    disable: list[_Ref] = Field(default_factory=list)
    enable: list[_Ref] = Field(default_factory=list)


class _ActiveEntry(_M):
    id: str
    name: str
    previous: bool
    desired: bool


class _ActiveSet(_M):
    set_id: str
    set_name: str
    activated_at: str
    entries: list[_ActiveEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class _Updates(_M):
    check: bool = True
    token: str | None = None


_SCENES = TypeAdapter(list[_Scene])
_SETS = TypeAdapter(list[_ScheduleSet])


def _check(model: Any, raw: Any, where: str, problems: list[str], warnings: list[str] | None = None) -> Any:
    """Validate one section; collect readable problems instead of raising."""
    try:
        out = model.model_validate(raw) if hasattr(model, "model_validate") else model.validate_python(raw)
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join([where, *(str(x) for x in e["loc"])])
            problems.append(f"{loc}: {e['msg']}")
        return None
    if warnings is not None and isinstance(raw, dict) and hasattr(model, "model_fields"):
        extra = sorted(set(raw) - set(model.model_fields))
        if extra:
            warnings.append(f"Ignored settings this version does not know in {where}: {', '.join(extra)}")
    return out


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def _cert_names() -> tuple[str, ...]:
    from .drivers.leap import CA_FILE, CERT_FILE, KEY_FILE

    return (KEY_FILE, CERT_FILE, CA_FILE)


def read_cert_files(cert_dir: str) -> dict[str, bytes]:
    """The pairing certificate files in ``cert_dir`` (only the three fixed names), if the folder exists."""
    out: dict[str, bytes] = {}
    if not cert_dir:
        return out
    base = Path(cert_dir).expanduser()
    for name in _cert_names():
        try:
            data = (base / name).read_bytes()
        except OSError:
            continue
        if len(data) <= MAX_CERT_FILE_BYTES:
            out[name] = data
    return out


def build_export(config: AppConfig, *, include_credentials: bool = False) -> dict:
    """The export document for ``config``.  Credentials are only included when asked for."""
    cfg = copy.deepcopy(config.to_dict())
    cfg["updates"].pop("dismissed_version", None)  # "the banner I closed": per-install, not a setting
    doc: dict[str, Any] = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "app_version": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "includes_credentials": bool(include_credentials),
        "config": cfg,
    }
    if include_credentials:
        certs = read_cert_files(config.driver.leap_cert_dir)
        if certs:
            doc["files"] = {"leap_certs": {n: base64.b64encode(b).decode("ascii") for n, b in certs.items()}}
    else:
        cfg["driver"].pop("username", None)
        cfg["driver"].pop("password", None)
        cfg["updates"].pop("token", None)
    return doc


def export_filename(now: datetime | None = None) -> str:
    return f"lutron-to-dmx-bridge-config-{(now or datetime.now()):%Y%m%d-%H%M%S}.json"


def write_private(path: Path, content: str | bytes) -> None:
    """Write a file only the current user can read (best effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)  # the mode above only applies to a newly created file
    except OSError:
        pass


# ---------------------------------------------------------------------------
# parsing and validation
# ---------------------------------------------------------------------------
@dataclass
class ParsedImport:
    format: str
    schema_version: int
    app_version: str | None
    created: str | None
    includes_credentials: bool
    #: sections the file actually contains (a missing one is never wiped by an import)
    sections: list[str]
    #: validated settings; only the sections listed above hold meaningful values
    config: AppConfig
    #: the integration login / update token are in the file (otherwise the current ones are kept)
    login_included: bool = False
    token_included: bool = False
    #: the active schedule set's saved prior states, if the file was exported while a set was active
    active_set: ActiveScheduleSet | None = None
    cert_files: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def info(self) -> dict:
        newer = False
        a, b = parse_version(self.app_version or ""), parse_version(__version__)
        if a and b:
            newer = a > b
        return {"format": self.format, "schema_version": self.schema_version, "app_version": self.app_version,
                "created": self.created, "includes_credentials": self.includes_credentials,
                "login_included": self.login_included, "token_included": self.token_included,
                "has_certificates": bool(self.cert_files), "newer_app": newer,
                "active_set": self.active_set.set_name if self.active_set else None}


def _migrate(doc: dict, version: int) -> dict:
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ConfigImportError(f"This file uses config format {version}, which this version of the app "
                                    "cannot upgrade. Export it again from the app that made it.")
        doc = step(copy.deepcopy(doc))
        version += 1
    return doc


def _decode_certs(raw: Any, problems: list[str], warnings: list[str]) -> dict[str, bytes]:
    if raw is None:
        return {}
    certs = raw.get("leap_certs") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or (certs is not None and not isinstance(certs, dict)):
        problems.append("files: expected an object")
        return {}
    if set(raw) - {"leap_certs"}:
        warnings.append("Ignored files in the export that this version does not know.")
    out: dict[str, bytes] = {}
    allowed = _cert_names()
    for name, text in (certs or {}).items():
        if name not in allowed:
            warnings.append(f"Ignored unexpected file {name!r} in the export.")
            continue
        try:
            if not isinstance(text, str):
                raise ValueError("not text")
            data = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            problems.append(f"files.leap_certs.{name}: not valid base64")
            continue
        if len(data) > MAX_CERT_FILE_BYTES:
            problems.append(f"files.leap_certs.{name}: too large to be a certificate file")
            continue
        out[name] = data
    return out


def parse_document(raw: Any) -> ParsedImport:
    """Validate an export (or a ``/api/backup`` file) and return its settings.  Raises ConfigImportError."""
    if not isinstance(raw, dict):
        raise ConfigImportError("This is not a Lutron To DMX Bridge export file (expected a JSON object).")
    warnings: list[str] = []
    problems: list[str] = []
    fmt = raw.get("format")
    cert_raw: Any = None

    if fmt == BACKUP_FORMAT:
        cfg = raw.get("app_config")
        if not isinstance(cfg, dict):
            raise ConfigImportError("This backup file has no app settings in it.")
        cfg = copy.deepcopy(cfg)
        # The backup masks the password and (older ones) holds the token; neither is a usable credential here.
        if isinstance(cfg.get("driver"), dict):
            cfg["driver"].pop("username", None)
            cfg["driver"].pop("password", None)
        if isinstance(cfg.get("updates"), dict):
            cfg["updates"].pop("token", None)
        schema, app_version, creds = 1, None, False
        warnings.append("This is a Lutron system backup file; only the app settings in it are imported.")
    elif fmt == FORMAT:
        schema = raw.get("schema_version")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise ConfigImportError("The export file has no valid schema_version, so it cannot be read safely.")
        app_version = raw.get("app_version") if isinstance(raw.get("app_version"), str) else None
        if schema > SCHEMA_VERSION:
            raise ConfigImportError(
                f"This file was made by a newer version of the app (config format {schema}"
                + (f", app {app_version}" if app_version else "")
                + f"); this version reads up to format {SCHEMA_VERSION}. Update the app, then import it again.")
        raw = _migrate(raw, schema)
        cfg = raw.get("config")
        if not isinstance(cfg, dict):
            raise ConfigImportError("The export file has no \"config\" section.")
        cfg = copy.deepcopy(cfg)
        creds = raw.get("includes_credentials") is True
        cert_raw = raw.get("files") if creds else None
        if not creds:  # a hand-edited file that carries a password anyway is not trusted to be intentional
            if isinstance(cfg.get("driver"), dict):
                cfg["driver"].pop("username", None)
                cfg["driver"].pop("password", None)
            if isinstance(cfg.get("updates"), dict):
                cfg["updates"].pop("token", None)
        a, b = parse_version(app_version or ""), parse_version(__version__)
        if a and b and a > b:
            warnings.append(f"This file was exported by version {app_version}, newer than this app "
                            f"({__version__}). Settings added since then are ignored; update the app for a "
                            "complete restore.")
    else:
        raise ConfigImportError("This is not a Lutron To DMX Bridge export file "
                                "(its \"format\" is missing or different).")

    known = {"driver", "dmx", "scenes", "schedule_sets", "active_schedule_set", "updates", "web_host", "web_port"}
    extra = sorted(set(cfg) - known)
    if extra:
        warnings.append(f"Ignored settings this version does not know: {', '.join(extra)}")

    out = AppConfig()
    sections: list[str] = []
    login = token = False

    if cfg.get("driver") is not None:
        m = _check(_Driver, cfg["driver"], "driver", problems, warnings)
        if m is not None:
            d = m.model_dump(exclude={"username", "password"})
            out.driver = DriverConfig(**d)
            if m.password is not None:
                out.driver.password = m.password
                out.driver.username = m.username if m.username is not None else out.driver.username
                login = True
            sections.append("driver")
    if cfg.get("dmx") is not None:
        m = _check(_Dmx, cfg["dmx"], "dmx", problems, warnings)
        if m is not None:
            d = m.model_dump(exclude={"mappings"})
            out.dmx = DmxConfig(**d, mappings=[DmxFixtureMapping(**x.model_dump()) for x in m.mappings])
            sections.append("dmx")
            _dmx_warnings(out.dmx, warnings)
    if cfg.get("scenes") is not None:
        scenes = _check(_SCENES, cfg["scenes"], "scenes", problems)
        if scenes is not None:
            ids = [s.id for s in scenes]
            if len(set(ids)) != len(ids):
                problems.append("scenes: two scenes share the same id")
            out.scenes = [SceneConfig(**s.model_dump()) for s in scenes]
            sections.append("scenes")
    if cfg.get("schedule_sets") is not None:
        sets = _check(_SETS, cfg["schedule_sets"], "schedule_sets", problems)
        if sets is not None:
            _check_sets(sets, problems)
            out.schedule_sets = [ScheduleSetConfig(
                id=s.id, name=s.name, disable=[ScheduleRef(**r.model_dump()) for r in s.disable],
                enable=[ScheduleRef(**r.model_dump()) for r in s.enable]) for s in sets]
            sections.append("schedule_sets")
    active = None
    if cfg.get("active_schedule_set") is not None:
        a = _check(_ActiveSet, cfg["active_schedule_set"], "active_schedule_set", problems)
        if a is not None:
            active = ActiveScheduleSet(
                set_id=a.set_id, set_name=a.set_name, activated_at=a.activated_at,
                entries=[ActiveScheduleEntry(**e.model_dump()) for e in a.entries], warnings=list(a.warnings))
    if cfg.get("updates") is not None:
        m = _check(_Updates, cfg["updates"], "updates", problems, warnings)
        if m is not None:
            out.updates.check = m.check
            if m.token is not None:
                out.updates.token, token = m.token, True
            sections.append("updates")
    if cfg.get("web_host") is not None or cfg.get("web_port") is not None:
        host, port = cfg.get("web_host", out.web_host), cfg.get("web_port", out.web_port)
        if not isinstance(host, str) or not host.strip():
            problems.append("web_host: must be text")
        elif not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            problems.append("web_port: must be a number from 1 to 65535")
        else:
            out.web_host, out.web_port = host.strip(), port
            sections.append("web")

    certs = _decode_certs(cert_raw, problems, warnings)
    if problems:
        raise ConfigImportError("The file has settings this app cannot accept", problems)
    if not sections:
        raise ConfigImportError("The file does not contain any settings that can be imported.")
    return ParsedImport(
        format=fmt, schema_version=schema, app_version=app_version, created=_text(raw.get("created")),
        includes_credentials=creds, sections=sections, config=out, login_included=login,
        token_included=token, active_set=active, cert_files=certs, warnings=warnings)


def _text(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _check_sets(sets: list[_ScheduleSet], problems: list[str]) -> None:
    if len({s.id for s in sets}) != len(sets):
        problems.append("schedule_sets: two sets share the same id")
    if len({s.name.casefold() for s in sets}) != len(sets):
        problems.append("schedule_sets: two sets share the same name")
    for s in sets:
        if {r.id for r in s.disable} & {r.id for r in s.enable}:
            problems.append(f"schedule_sets: \"{s.name}\" both disables and enables the same schedule")


def _dmx_warnings(dmx: DmxConfig, warnings: list[str]) -> None:
    used: dict[int, str] = {}
    clashes = 0
    for m in dmx.mappings:
        if not m.enabled:
            continue
        for i in range(len(m.params)):
            ch = m.address + i
            if ch in used and used[ch] != m.zone_id:
                clashes += 1
            used.setdefault(ch, m.zone_id)
    if clashes:
        warnings.append(f"{clashes} DMX channel(s) in the file are mapped to more than one zone "
                        "(the first mapping wins, as it does today).")


# ---------------------------------------------------------------------------
# what would change
# ---------------------------------------------------------------------------
@dataclass
class SectionPreview:
    id: str
    label: str
    changes: list[str]
    #: set when this section cannot be imported right now (the reason)
    blocked: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "changes": self.changes, "changed": self.changed,
                "blocked": self.blocked, "default": self.id in DEFAULT_SECTIONS}


def _short(names: list[str], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _list_changes(noun: str, cur: list, new: list, key: Callable, label: Callable,
                  detail: Callable | None = None) -> list[str]:
    cm, nm = {key(x): x for x in cur}, {key(x): x for x in new}
    added = [label(nm[k]) for k in nm if k not in cm]
    removed = [label(cm[k]) for k in cm if k not in nm]
    out = []
    if added:
        out.append(f"Add {len(added)} {noun}{'' if len(added) == 1 else 's'}: {_short(added)}")
    if removed:
        out.append(f"Remove {len(removed)} {noun}{'' if len(removed) == 1 else 's'}: {_short(removed)}")
    changed = []
    for k in nm:
        if k in cm and dataclasses.asdict(nm[k]) != dataclasses.asdict(cm[k]):
            changed.append(detail(cm[k], nm[k]) if detail else label(nm[k]))
    if changed:
        out.append(f"Change {len(changed)} {noun}{'' if len(changed) == 1 else 's'}: {_short(changed)}")
    return out


def _fields(obj_cur: Any, obj_new: Any, names: list[str], labels: dict[str, str] | None = None) -> list[str]:
    out = []
    for n in names:
        a, b = getattr(obj_cur, n), getattr(obj_new, n)
        if a != b:
            lab = (labels or {}).get(n, n.replace("_", " "))
            out.append(f"{lab}: {_show(a)} → {_show(b)}")
    return out


def _show(v: Any) -> str:
    if v is None or v == "":
        return "(none)"
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def effective_driver(config: AppConfig, parsed: ParsedImport) -> DriverConfig:
    """The driver settings an import would leave in place: the file's, with the current login / certificates
    kept when the file does not carry them."""
    new = dataclasses.replace(parsed.config.driver)
    cur = config.driver
    if not parsed.login_included:
        new.username, new.password = cur.username, cur.password
    if parsed.cert_files:
        new.leap_cert_dir = str(restored_cert_dir())
    elif cur.leap_cert_dir:
        new.leap_cert_dir = cur.leap_cert_dir
    return new


def restored_cert_dir() -> Path:
    return data_dir().joinpath(*RESTORED_CERT_SUBDIR)


def active_set_status(config: AppConfig, parsed: ParsedImport, sections: list[str]) -> tuple[str | None, bool]:
    """(blocked reason for the schedule_sets section, the file's active set can be adopted)."""
    if "schedule_sets" not in sections:
        return None, False
    new_ids = {s.id for s in parsed.config.schedule_sets}
    cur = config.active_schedule_set
    if cur is not None and cur.set_id not in new_ids:
        return (f"The schedule set \"{cur.set_name}\" is active right now and is not in the file. Restore the "
                "schedules first (Schedules tab), or leave this section out."), False
    adoptable = cur is None and parsed.active_set is not None and parsed.active_set.set_id in new_ids
    return None, adoptable


def describe_changes(config: AppConfig, parsed: ParsedImport, sections: list[str] | None = None,
                     zone_names: dict[str, str] | None = None) -> list[SectionPreview]:
    """What importing ``sections`` (default: every default section the file has) would change."""
    sections = select_sections(parsed, sections)
    zn = zone_names or {}

    def zname(zid: str) -> str:
        return zn.get(zid) or f"zone {zid}"

    out: list[SectionPreview] = []
    for sec in sections:
        new, cur = parsed.config, config
        if sec == "driver":
            nd, cd = effective_driver(config, parsed), config.driver
            ch = _fields(cd, nd, ["kind", "host", "hostname", "port", "integration_report", "leap_cert_dir"],
                         {"kind": "system type", "host": "address", "integration_report": "integration report",
                          "leap_cert_dir": "certificate folder"})
            if parsed.login_included and (nd.username, nd.password) != (cd.username, cd.password):
                ch.append("Integration login: changed")
            if parsed.cert_files:
                ch.append("Pairing certificates: restored from the file")
            p = SectionPreview(sec, SECTIONS[sec], ch)
        elif sec == "dmx":
            nd, cd = new.dmx, cur.dmx
            ch = _fields(cd, nd, ["enabled", "source", "universe", "bind_ip", "serial_port", "max_rate_hz",
                                  "max_commands_per_s", "resolution_pct", "protect", "latency_limit_ms",
                                  "max_inflight"],
                         {"enabled": "DMX input", "bind_ip": "bind address", "serial_port": "serial port",
                          "max_rate_hz": "max rate per zone (Hz)", "max_commands_per_s": "max commands per second",
                          "resolution_pct": "level step (%)",
                          "protect": "repeater protection", "latency_limit_ms": "latency limit (ms)",
                          "max_inflight": "max commands in flight"})

            def desc(m: DmxFixtureMapping) -> str:
                chans = f"{m.address}" if len(m.params) == 1 else f"{m.address}-{m.address + len(m.params) - 1}"
                return f"{zname(m.zone_id)} (ch {chans}{'' if m.enabled else ', off'})"

            ch += _list_changes("mapping", cd.mappings, nd.mappings, lambda m: m.zone_id,
                                lambda m: desc(m), lambda a, b: f"{desc(a)} → {desc(b)}")
            p = SectionPreview(sec, SECTIONS[sec], ch)
        elif sec == "scenes":
            ch = _list_changes("scene", cur.scenes, new.scenes, lambda s: s.id, lambda s: f"\"{s.name}\"",
                               lambda a, b: f"\"{b.name}\"" if a.name == b.name else f"\"{a.name}\" → \"{b.name}\"")
            p = SectionPreview(sec, SECTIONS[sec], ch)
        elif sec == "schedule_sets":
            ch = _list_changes("set", cur.schedule_sets, new.schedule_sets, lambda s: s.id, lambda s: f"\"{s.name}\"",
                               lambda a, b: f"\"{b.name}\"" if a.name == b.name else f"\"{a.name}\" → \"{b.name}\"")
            blocked, adoptable = active_set_status(config, parsed, sections)
            if parsed.active_set is not None:
                ch.append(f"The file was exported while \"{parsed.active_set.set_name}\" was active"
                          + (" (its saved prior schedule states can be restored too)" if adoptable else ""))
            p = SectionPreview(sec, SECTIONS[sec], ch, blocked)
        elif sec == "updates":
            ch = _fields(cur.updates, new.updates, ["check"], {"check": "check for updates automatically"})
            if parsed.token_included and new.updates.token != cur.updates.token:
                ch.append("GitHub token: changed")
            p = SectionPreview(sec, SECTIONS[sec], ch)
        else:  # web
            ch = _fields(cur, new, ["web_host", "web_port"], {"web_host": "web address", "web_port": "web port"})
            if ch:
                ch.append("Takes effect the next time the app starts")
            p = SectionPreview(sec, SECTIONS[sec], ch)
        out.append(p)
    return out


def select_sections(parsed: ParsedImport, wanted: list[str] | None) -> list[str]:
    """Validate a section choice; ``None`` means the defaults the file has."""
    if wanted is None:
        wanted = [s for s in DEFAULT_SECTIONS if s in parsed.sections]
    bad = [s for s in wanted if s not in SECTIONS]
    if bad:
        raise ConfigImportError(f"Unknown section(s): {', '.join(bad)}")
    missing = [s for s in wanted if s not in parsed.sections]
    if missing:
        raise ConfigImportError(f"The file has no {', '.join(SECTIONS[s] for s in missing)} to import.")
    if not wanted:
        raise ConfigImportError("Pick at least one section to import.")
    return [s for s in SECTIONS if s in wanted]


def import_warnings(config: AppConfig, parsed: ParsedImport, sections: list[str],
                    zone_ids: set[str] | None = None, schedule_ids: set[str] | None = None
                    ) -> list[tuple[str | None, str]]:
    """Things to know before importing, as (section or None for the whole file, text).

    ``zone_ids`` / ``schedule_ids`` are what the connected Lutron system has (``None`` = not connected, or
    about to be replaced, so not checked).
    """
    out: list[tuple[str | None, str]] = [(None, w) for w in parsed.warnings]
    if "driver" in sections:
        nd = effective_driver(config, parsed)
        if not parsed.login_included and nd.kind == "ra2":
            out.append(("driver", "The file does not carry the integration login; the one this app has now is kept."))
        if nd.kind == "leap" and not parsed.cert_files:
            from .drivers.leap import cert_paths

            if not all(p.exists() for p in cert_paths(nd.leap_cert_dir or "-")):
                out.append(("driver", "No LEAP pairing certificates were found for this connection; "
                                      "pair again in Settings."))
        if nd.kind == "ra2" and nd.integration_report and not Path(nd.integration_report).expanduser().exists():
            out.append(("driver", f"The integration report file {nd.integration_report} does not exist on "
                                  "this computer."))
    if "updates" in sections and not parsed.token_included and config.updates.token:
        out.append(("updates", "The file does not carry the update token; the current one is kept."))
    if zone_ids is not None:
        if "scenes" in sections:
            n = sum(1 for s in parsed.config.scenes if any(z not in zone_ids for z in s.levels))
            if n:
                out.append(("scenes", f"{n} scene(s) use zones that the connected Lutron system does not have."))
        if "dmx" in sections:
            n = sum(1 for m in parsed.config.dmx.mappings if m.zone_id not in zone_ids)
            if n:
                out.append(("dmx", f"{n} DMX mapping(s) point at zones that the connected Lutron system "
                                   "does not have."))
    if schedule_ids is not None and "schedule_sets" in sections:
        n = sum(1 for s in parsed.config.schedule_sets for r in (*s.disable, *s.enable) if r.id not in schedule_ids)
        if n:
            out.append(("schedule_sets", f"{n} schedule(s) in the schedule sets are not found in the connected "
                                         "Lutron system (they are matched by name when a set is activated)."))
    if "web" in sections and parsed.config.web_host not in ("127.0.0.1", "localhost", "::1"):
        out.append(("web", f"The file sets the web GUI to listen on {parsed.config.web_host}, which other "
                           "computers on the network can reach."))
    return out


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
@dataclass
class ImportResult:
    applied: list[str]
    backup: str
    driver_changed: bool = False
    dmx_changed: bool = False
    restart_required: bool = False
    active_set_restored: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def write_pre_import_backup(config: AppConfig) -> Path:
    """Save the current settings (login and certificates included) so an import can be undone by importing
    that file.  Kept private to the user because it holds the same secrets as ``config.json``."""
    bdir = data_dir() / "backups"
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    path = bdir / f"config-before-import-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = bdir / f"config-before-import-{stamp}-{n}.json"
    write_private(path, json.dumps(build_export(config, include_credentials=True), indent=2))
    return path


def _write_certs(files: dict[str, bytes]) -> Path:
    dest = restored_cert_dir()
    dest.mkdir(parents=True, exist_ok=True)
    allowed = _cert_names()
    for name, data in files.items():
        if name not in allowed:  # parse_document already filtered; this is the last line of defence
            raise ConfigImportError(f"Unexpected certificate file {name!r}")
        write_private(dest / name, data)
    return dest


def import_into_config(config: AppConfig, parsed: ParsedImport, sections: list[str] | None = None,
                       restore_active_set: bool = False) -> ImportResult:
    """Back up the current settings, replace the chosen sections in ``config`` and save it.

    Whole sections are replaced (scenes that are not in the file are removed), which is what makes this a
    restore.  The caller reconnects the Lutron driver / restarts DMX for the sections reported as changed.
    """
    sections = select_sections(parsed, sections)
    blocked, adoptable = active_set_status(config, parsed, sections)
    if blocked:
        raise ConfigImportError(blocked, status=409)
    backup = write_pre_import_backup(config)
    result = ImportResult(applied=list(sections), backup=str(backup))
    new = parsed.config

    if "driver" in sections:
        if parsed.cert_files:
            _write_certs(parsed.cert_files)
        nd = effective_driver(config, parsed)
        result.driver_changed = nd != config.driver or bool(parsed.cert_files)
        config.driver = nd
    if "dmx" in sections:
        result.dmx_changed = new.dmx != config.dmx
        config.dmx = new.dmx
    if "scenes" in sections:
        config.scenes = new.scenes
    if "schedule_sets" in sections:
        config.schedule_sets = new.schedule_sets
        active = config.active_schedule_set
        if active is not None:  # still present (checked above); follow a rename
            active.set_name = next(s.name for s in new.schedule_sets if s.id == active.set_id)
        elif restore_active_set and adoptable:
            config.active_schedule_set = parsed.active_set
            result.active_set_restored = True
        elif restore_active_set:
            result.warnings.append("The saved schedule states in the file were not restored: the file's active "
                                   "set is not among the imported sets.")
    if "updates" in sections:
        config.updates.check = new.updates.check
        if parsed.token_included:
            config.updates.token = new.updates.token
    if "web" in sections:
        result.restart_required = (new.web_host, new.web_port) != (config.web_host, config.web_port)
        config.web_host, config.web_port = new.web_host, new.web_port
    config.save()
    log.info("Imported settings (%s); previous settings saved to %s", ", ".join(sections), backup)
    return result
