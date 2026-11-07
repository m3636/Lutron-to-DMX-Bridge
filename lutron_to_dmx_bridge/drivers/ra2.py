"""RadioRA 2 driver: Lutron Integration Protocol over telnet to the Main Repeater.

Protocol reference: Lutron "Integration Protocol" guide (doc 040249), the
RadioRA 2 sections. Summary of what this driver uses:

* TCP port 23, plain text. The repeater prompts ``login: `` then
  ``password: `` and, once logged in, prints the ``GNET> `` prompt. The login
  is the integration user configured in RadioRA 2 Essentials/Inclusive
  (factory default ``lutron`` / ``integration``).
* Commands end in CR LF. ``#`` = execute, ``?`` = query, ``~`` = reply /
  unsolicited monitoring message.
* ``#OUTPUT,<id>,1,<level>[,<fade>[,<delay>]]`` sets an output (action 1 =
  "set zone level"); level 0-100 with up to 2 decimals; fade/delay are
  ``SS``, ``SS.ss``, ``MM:SS`` or ``HH:MM:SS``.
* ``?OUTPUT,<id>,1`` -> ``~OUTPUT,<id>,1,<level>``.
* ``#MONITORING,5,1`` enables Zone (output level) monitoring (type 5 = zone,
  action 1 = enable) so every level change, whatever caused it, arrives as a
  ``~OUTPUT`` line. Monitoring settings are per-connection and reset when the
  socket drops, so they are re-sent after every (re)login.
* ``#TIMECLOCK,<tc_id>,6,<event_index>,<1|2>`` enables (1) / disables (2) a
  timeclock event; ``?TIMECLOCK,<tc_id>,6,<event_index>`` ->
  ``~TIMECLOCK,<tc_id>,6,<event_index>,<state>``. Taken from the Integration
  Protocol document - NOT yet verified against real RA2 hardware. In
  particular the event index is assumed to be the 1-based position of the
  event in DbXmlInfo.xml, which must be confirmed on the actual system.

The inventory (areas, outputs, keypads, timeclock events) is not available
over telnet; the Main Repeater serves it as XML at ``http://<host>/DbXmlInfo.xml``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any

from ..models import Area, Keypad, Schedule, SystemInventory, Zone, ZoneType
from .base import DriverError, LutronDriver

log = logging.getLogger(__name__)

DEFAULT_PORT = 23
#: cheap query the repeater always answers (~SYSTEM,1,<time>); its round trip is the latency graph
PROBE_COMMAND = "?SYSTEM,1"
PROMPT = b"GNET> "
LOGIN_PROMPT = b"login: "
PASSWORD_PROMPT = b"password: "

# Leading prompt(s) the repeater prepends to output, e.g. "GNET> ~OUTPUT,...".
# (QNET is the HomeWorks QS equivalent; harmless to accept.)
_PROMPT_RE = re.compile(r"^(?:[GQ]NET>\s*)+")

# -- output type mapping ---------------------------------------------------
_SWITCH_PREFIXES = ("NON_DIM", "RELAY", "CCO")
_SHADE_TYPES = {"SYSTEM_SHADE", "SHADE", "MOTOR", "VENETIAN_BLIND", "HORIZONTAL_SHEER_BLIND"}
_FAN_TYPES = {"CEILING_FAN_TYPE"}

# Devices whose buttons aren't a user keypad (phantom buttons, sensors, etc.)
_NON_KEYPAD_DEVICE_TYPES = {"MAIN_REPEATER", "REPEATER", "MOTION_SENSOR", "VISOR_CONTROL_RECEIVER"}


def output_type_to_zone_type(output_type: str) -> ZoneType:
    """Map a DbXmlInfo ``OutputType`` (INC, MLV, NON_DIM, ...) to a ZoneType.

    Anything unknown is treated as a dimmer, which is safe: dimmers accept
    every level and on/off-only loads simply snap.
    """
    t = (output_type or "").upper()
    if t in _FAN_TYPES:
        return ZoneType.FAN
    if t in _SHADE_TYPES or "SHADE" in t:
        return ZoneType.SHADE
    if t.startswith(_SWITCH_PREFIXES):
        return ZoneType.SWITCH
    return ZoneType.DIMMER


def format_fade(seconds: float) -> str:
    """Format a fade/delay time for the Integration Protocol.

    <60 s -> ``SS`` or ``SS.ss`` ("2", "0.25"); <1 h -> ``M:SS`` ("1:30");
    otherwise ``H:MM:SS``. Times >= 60 s are rounded to whole seconds.
    """
    secs = round(max(0.0, float(seconds)), 2)
    if secs < 60:
        if secs == int(secs):
            return str(int(secs))
        return f"{secs:.2f}".rstrip("0").rstrip(".")
    whole = int(round(secs))
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_level(level: float) -> str:
    """0-100 percent with at most 2 decimals, trailing zeros stripped."""
    lvl = max(0.0, min(100.0, float(level)))
    return f"{lvl:.2f}".rstrip("0").rstrip(".")


# -- DbXmlInfo.xml parsing ---------------------------------------------------
def parse_db_xml(text: str | bytes) -> SystemInventory:
    """Parse the RA2 Main Repeater's DbXmlInfo.xml into a SystemInventory.

    Areas nest recursively (Project > Areas > Area > Areas > Area ...). Pure
    "container" areas (no outputs or keypads of their own, only sub-areas -
    typically the root "Home" area) are omitted from the area list.
    """
    if isinstance(text, bytes):
        root = ET.fromstring(text)
    else:
        # ElementTree rejects a str that still carries a BOM.
        root = ET.fromstring(text.lstrip("﻿"))

    inv = SystemInventory()
    used_area_ids: set[str] = set()

    def area_id_for(el: ET.Element) -> str:
        # Prefer the integration ID; fall back to a prefixed UUID so the two
        # number spaces can't collide (the root area usually has ID 0).
        uuid = el.get("UUID")
        for cand in (el.get("IntegrationID"), f"uuid-{uuid}" if uuid else None):
            if cand and cand != "0" and cand not in used_area_ids:
                used_area_ids.add(cand)
                return cand
        aid = f"area{len(used_area_ids) + 1}"
        used_area_ids.add(aid)
        return aid

    def walk(area_el: ET.Element) -> None:
        aid = area_id_for(area_el)
        zones: list[Zone] = []
        keypads: list[Keypad] = []

        outputs = area_el.find("Outputs")
        if outputs is not None:
            for out in outputs.findall("Output"):
                iid = out.get("IntegrationID")
                if not iid:
                    continue
                zones.append(Zone(
                    id=iid,
                    name=out.get("Name", f"Output {iid}"),
                    area_id=aid,
                    type=output_type_to_zone_type(out.get("OutputType", "")),
                ))

        groups = area_el.find("DeviceGroups")
        if groups is not None:
            # Devices may sit in DeviceGroup/Devices or directly under DeviceGroups.
            for dev in groups.iter("Device"):
                kp = _parse_keypad(dev, aid)
                if kp is not None:
                    keypads.append(kp)

        children = area_el.find("Areas")
        child_areas = children.findall("Area") if children is not None else []

        if zones or keypads or not child_areas:
            inv.areas.append(Area(id=aid, name=area_el.get("Name", aid)))
            inv.zones.extend(zones)
            inv.keypads.extend(keypads)
        for child in child_areas:
            walk(child)

    top = root.find("Areas")
    if top is not None:
        for a in top.findall("Area"):
            walk(a)

    # Timeclocks normally live under Project, but search anywhere to be safe.
    for tc in root.iter("Timeclock"):
        tc_id = tc.get("IntegrationID")
        if not tc_id:
            continue
        tc_name = tc.get("Name", f"Timeclock {tc_id}")
        # Real RA2 exports (AppVer 12.x) use <TimeClockEvents><TimeClockEvent EventNumber=..>;
        # <Events><Event> is kept for older / hand-made files.
        events = tc.find("TimeClockEvents")
        if events is None:
            events = tc.find("Events")
        if events is None:
            continue
        for pos, ev in enumerate(list(events), start=1):
            if ev.tag not in ("TimeClockEvent", "Event"):
                continue
            index = ev.get("EventNumber") or str(pos)
            inv.schedules.append(Schedule(
                id=f"{tc_id}:{index}",
                name=ev.get("Name", f"Event {index}"),
                enabled=True,  # real state is queried after login
                description=_describe_event(ev, tc_name, index),
                timeclock_id=tc_id,
            ))
    return inv


def _describe_event(ev: ET.Element, tc_name: str, index: str) -> str:
    """e.g. "Sunset, every day (Project Timeclock event 2)"."""
    when = ev.get("VariableEvent") if ev.get("Type") == "Variable" else ev.get("Time")
    days = ev.get("Days", "")
    if days and len(days.split(",")) == 7:
        days = "every day"
    parts = [p for p in (when, days) if p]
    where = f"{tc_name} event {index}"
    return f"{', '.join(parts)} ({where})" if parts else where


def _parse_keypad(dev: ET.Element, area_id: str) -> Keypad | None:
    iid = dev.get("IntegrationID")
    dtype = (dev.get("DeviceType") or "").upper()
    if not iid or dtype in _NON_KEYPAD_DEVICE_TYPES:
        return None
    buttons: list[str] = []
    for comp in dev.iter("Component"):
        if comp.get("ComponentType") != "BUTTON":
            continue
        btn = comp.find("Button")
        if btn is None:
            continue
        label = (btn.get("Engraving") or "").strip() or btn.get("Name") \
            or f"Button {comp.get('ComponentNumber')}"
        buttons.append(label)
    if not buttons:
        return None  # sensors, receivers etc. without buttons
    return Keypad(id=iid, name=dev.get("Name", f"Keypad {iid}"), area_id=area_id, buttons=buttons)


def _parse_schedule_id(schedule_id: str) -> tuple[str, str]:
    tc, sep, idx = schedule_id.partition(":")
    if not sep or not tc.isdigit() or not idx.isdigit():
        raise DriverError(f"Bad RA2 schedule id {schedule_id!r} (expected '<timeclock>:<index>')")
    return tc, idx


# -- driver ----------------------------------------------------------------
class RA2Driver(LutronDriver):
    system_name = "RadioRA 2"
    #: a command with no echo after this long stops counting as in flight (a command
    #: that changes nothing may never be echoed)
    inflight_expiry = 5.0
    #: how long refresh_schedules waits for the repeater's ~TIMECLOCK replies
    schedule_query_timeout = 3.0

    def __init__(
        self,
        config: Any,
        *,
        probe_interval: float = 2.0,
        connect_timeout: float = 10.0,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        super().__init__()
        self.config = config
        self.probe_interval = probe_interval
        self.connect_timeout = connect_timeout
        self.reconnect_max_delay = reconnect_max_delay

        self._inventory = SystemInventory()
        self._db_xml: str = ""
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        #: zone id -> monotonic time of the last #OUTPUT we sent, to log how long the echo takes
        #: and to count commands in flight
        self._output_sent_at: dict[str, float] = {}
        #: monotonic send times of the ?SYSTEM,1 probes still waiting for their ~SYSTEM reply
        #: (the repeater answers in order, so replies pair with probes first-in first-out)
        self._probes_sent: deque[float] = deque()
        self._closing = False
        self._logged_in = False
        self._read_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._schedule_replies: set[str] = set()

    @property
    def port(self) -> int:
        return int(self.config.port or DEFAULT_PORT)

    # -- lifecycle -------------------------------------------------------
    async def connect(self) -> None:
        self._closing = False
        text = await self._load_db_xml()
        try:
            self._inventory = parse_db_xml(text)
        except ET.ParseError as exc:
            raise DriverError(f"Could not parse RA2 integration report XML: {exc}") from exc
        self._db_xml = text
        await self._open()
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def close(self) -> None:
        self._closing = True
        tasks = [t for t in (self._probe_task, self._reconnect_task, self._read_task) if t]
        self._probe_task = self._reconnect_task = self._read_task = None
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._drop_connection()

    @property
    def connected(self) -> bool:
        return self._logged_in and self._writer is not None and not self._closing

    def inventory(self) -> SystemInventory:
        return self._inventory

    async def _load_db_xml(self) -> str:
        """Read the integration report from a local file, else from the repeater."""
        report = (self.config.integration_report or "").strip()
        if report:
            path = Path(report).expanduser()
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise DriverError(f"Cannot read integration report {path}: {exc}") from exc
        else:
            if not self.config.host:
                raise DriverError("No RA2 Main Repeater host configured")
            url = f"http://{self.config.host}/DbXmlInfo.xml"
            try:
                data = await asyncio.to_thread(_http_get, url, 30.0)
            except Exception as exc:  # urllib raises a zoo of exception types
                raise DriverError(
                    f"Could not download {url}: {exc}. Save that page from a browser and "
                    "set it as the integration report file instead."
                ) from exc
        return data.decode("utf-8-sig", errors="replace")

    async def _open(self) -> None:
        """Open the telnet socket, log in and start monitoring."""
        host = self.config.host
        if not host:
            raise DriverError("No RA2 Main Repeater host configured")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self.port), self.connect_timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise DriverError(f"Cannot connect to {host}:{self.port}: {exc}") from exc

        try:
            await self._login(reader, writer)
        except BaseException:
            writer.close()
            raise

        self._reader, self._writer = reader, writer
        self._logged_in = True
        self.metrics.mark_connected()
        self._read_task = asyncio.create_task(self._read_loop(reader, writer))
        log.info("Connected to RadioRA 2 at %s:%s", host, self.port)

        # Monitoring resets per connection. Zone (output) monitoring is all we
        # need for level feedback.
        await self._send("#MONITORING,5,1")
        await self.refresh_levels()
        await self._query_schedules()

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        t = self.connect_timeout
        if await _read_until(reader, (LOGIN_PROMPT,), t) != LOGIN_PROMPT:
            raise DriverError("RA2 repeater did not send a login prompt")
        writer.write(f"{self.config.username}\r\n".encode())
        await writer.drain()
        if await _read_until(reader, (PASSWORD_PROMPT,), t) != PASSWORD_PROMPT:
            raise DriverError("RA2 repeater did not send a password prompt")
        writer.write(f"{self.config.password}\r\n".encode())
        await writer.drain()
        # A bad login sends "bad login" followed by a fresh "login: " prompt.
        got = await _read_until(reader, (PROMPT, LOGIN_PROMPT), t)
        if got != PROMPT:
            raise DriverError("RA2 login rejected - check the integration username/password")

    async def _drop_connection(self, only: asyncio.StreamWriter | None = None) -> None:
        """Close the current socket (or just `only`, if it is no longer current)."""
        writer = self._writer
        if only is not None and only is not writer:
            writer = only  # stale socket from an earlier connection
        else:
            self._logged_in = False
            self._writer = self._reader = None
            self._output_sent_at.clear()  # the replies will never come
            self._probes_sent.clear()
            self.metrics.mark_disconnected()
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:
                pass

    async def _read_loop(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        buf = ""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("ascii", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    try:
                        await self._handle_line(line)
                    except Exception:
                        log.exception("Error handling RA2 line %r", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("RA2 connection error: %s", exc)
        # Connection lost (not cancelled by close()).
        if self._read_task is asyncio.current_task():
            self._read_task = None
        stale = writer is not self._writer
        await self._drop_connection(writer)
        if not self._closing and not stale:
            log.warning("RA2 connection lost; reconnecting")
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 1.0
        while not self._closing:
            self.metrics.mark_retry(delay)
            await asyncio.sleep(delay)
            try:
                await self._open()
                return
            except Exception as exc:
                log.warning("RA2 reconnect failed (%s); retrying in %.0fs", exc, delay)
                delay = min(delay * 2, self.reconnect_max_delay)

    async def _probe_loop(self) -> None:
        # Every few seconds ask the repeater for its clock (a cheap query that always
        # gets a reply) and time the answer: that round trip, which includes any queue
        # of commands ahead of it, is the latency the GUI graphs. It doubles as the
        # keepalive: some network gear drops idle TCP sessions, and a dead socket
        # surfaces here quickly (the write fails -> reconnect).
        while not self._closing:
            await asyncio.sleep(self.probe_interval)
            if self.connected:
                try:
                    await self._send(PROBE_COMMAND)
                except Exception as exc:
                    log.warning("RA2 latency probe failed: %s", exc)
                    if self._writer is not None:
                        self._writer.close()  # read loop sees EOF and reconnects

    async def _handle_line(self, raw: str) -> None:
        line = _PROMPT_RE.sub("", raw.strip())
        if not line:
            return
        parts = line.split(",")
        cmd = parts[0]
        if cmd == "~OUTPUT" and len(parts) >= 4 and parts[2] == "1":
            try:
                level = float(parts[3])
            except ValueError:
                return
            sent_at = self._output_sent_at.pop(parts[1], None)
            if sent_at is not None and log.isEnabledFor(logging.DEBUG):
                log.debug("RA2 zone %s echoed %.0f ms after the command", parts[1], (time.monotonic() - sent_at) * 1000)
            await self._notify(parts[1], level)
        elif cmd == "~SYSTEM" and len(parts) >= 2 and parts[1] == "1":
            if self._probes_sent:
                self.metrics.latency.add((time.monotonic() - self._probes_sent.popleft()) * 1000)
        elif cmd == "~TIMECLOCK" and len(parts) >= 5 and parts[2] == "6":
            sid = f"{parts[1]}:{parts[3]}"
            self._schedule_replies.add(sid)
            sched = next((s for s in self._inventory.schedules if s.id == sid), None)
            if sched is not None:
                sched.enabled = parts[4].strip() == "1"
        elif cmd == "~ERROR":
            log.warning("RA2 reported error: %s", line)

    # -- sending ---------------------------------------------------------
    async def _send(self, *commands: str) -> None:
        """Write one or more commands back-to-back under the command lock.

        Replies are not awaited - they arrive asynchronously via the reader
        (monitoring) which keeps multi-zone commands as simultaneous as possible.
        """
        async with self._lock:
            writer = self._writer
            if writer is None or not self._logged_in:
                raise DriverError("Not connected to the RadioRA 2 repeater")
            now = time.monotonic()
            for c in commands:
                if c.startswith("#OUTPUT,"):
                    self._output_sent_at[c.split(",", 2)[1]] = now
                    self.metrics.commands.add(1, now)
                elif c == PROBE_COMMAND:
                    # a reply lost to a dropped line must not pair every later probe with the wrong send
                    while self._probes_sent and now - self._probes_sent[0] > self.inflight_expiry:
                        self._probes_sent.popleft()
                    self._probes_sent.append(now)
                writer.write(c.encode("ascii", errors="replace") + b"\r\n")
            try:
                await writer.drain()
            except (ConnectionError, OSError) as exc:
                raise DriverError(f"RA2 send failed: {exc}") from exc

    def inflight(self) -> int:
        cutoff = time.monotonic() - self.inflight_expiry
        return sum(1 for t in self._output_sent_at.values() if t >= cutoff)

    @staticmethod
    def output_command(zone_id: str, level: float, fade: float | None = None) -> str:
        cmd = f"#OUTPUT,{zone_id},1,{format_level(level)}"
        if fade is not None and fade > 0:
            cmd += f",{format_fade(fade)}"
        return cmd

    async def set_level(self, zone_id: str, level: float, fade: float | None = None) -> None:
        await self._send(self.output_command(zone_id, level, fade))

    async def set_levels(self, levels: dict[str, float], fade: float | None = None) -> None:
        if levels:
            await self._send(*(self.output_command(z, lvl, fade) for z, lvl in levels.items()))

    async def refresh_levels(self) -> None:
        cmds = [f"?OUTPUT,{z.id},1" for z in self._inventory.zones]
        if cmds:
            await self._send(*cmds)

    async def _query_schedules(self) -> None:
        cmds = []
        for s in self._inventory.schedules:
            try:
                tc, idx = _parse_schedule_id(s.id)
            except DriverError:
                continue
            cmds.append(f"?TIMECLOCK,{tc},6,{idx}")
        if cmds:
            await self._send(*cmds)

    async def refresh_schedules(self, ids: list[str] | None = None, timeout: float | None = None) -> set[str]:
        """Query ``?TIMECLOCK`` for each schedule and wait for the ``~TIMECLOCK`` replies."""
        known = {s.id for s in self._inventory.schedules}
        wanted = {i for i in (ids if ids is not None else known) if i in known}
        if not wanted:
            return set()
        self._schedule_replies -= wanted
        cmds = []
        for sid in sorted(wanted):
            tc, idx = _parse_schedule_id(sid)
            cmds.append(f"?TIMECLOCK,{tc},6,{idx}")
        await self._send(*cmds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.schedule_query_timeout if timeout is None else timeout)
        while not wanted <= self._schedule_replies and loop.time() < deadline:
            await asyncio.sleep(0.02)
        return wanted & self._schedule_replies

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        # UNVERIFIED on hardware - see module docstring.
        tc, idx = _parse_schedule_id(schedule_id)
        await self._send(f"#TIMECLOCK,{tc},6,{idx},{1 if enabled else 2}")
        sched = next((s for s in self._inventory.schedules if s.id == schedule_id), None)
        if sched is not None:
            sched.enabled = bool(enabled)  # optimistic; a ~TIMECLOCK reply corrects it
        await self._send(f"?TIMECLOCK,{tc},6,{idx}")

    # -- backup ----------------------------------------------------------
    async def backup(self) -> dict:
        """Reference snapshot of the system: DbXmlInfo.xml, inventory and levels.

        This is NOT a restorable RA2 backup. Full recovery of a RadioRA 2 system
        requires the project file (.rra2) from RadioRA 2 Essentials/Inclusive
        software, held by the installer/dealer. This snapshot documents names,
        integration IDs and current levels so a system could be re-created.
        """
        return {
            "system": self.system_name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": self.config.host,
            "db_xml": self._db_xml,
            "inventory": self._inventory.to_dict(),
            "levels": {z.id: z.level for z in self._inventory.zones},
        }


# -- helpers ---------------------------------------------------------------
def _http_get(url: str, timeout: float) -> bytes:
    # Bypass any system proxy - the repeater is always on the local network.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return resp.read()


async def _read_until(reader: asyncio.StreamReader, tokens: tuple[bytes, ...],
                      timeout: float) -> bytes | None:
    """Read until one of `tokens` appears (case-insensitive); return it or None on EOF."""
    buf = b""
    lowered = tuple(t.lower() for t in tokens)

    async def _inner() -> bytes | None:
        nonlocal buf
        while True:
            low = buf.lower()
            hits = [(low.find(t), tok) for t, tok in zip(lowered, tokens) if t in low]
            if hits:
                return min(hits)[1]
            chunk = await reader.read(1024)
            if not chunk:
                return None
            buf += chunk

    try:
        return await asyncio.wait_for(_inner(), timeout)
    except asyncio.TimeoutError as exc:
        raise DriverError(f"Timed out waiting for {tokens!r} from RA2 repeater") from exc
