"""Streaming ACN (ANSI E1.31 / sACN) receiver.

* UDP port 5568, multicast group 239.255.<universe_hi>.<universe_lo>.
  Unicast sACN to this machine is accepted too (and is the fallback when the
  multicast join fails, e.g. no multicast-capable interface).
* Universes are numbered 1-63999. A configured universe of 0 (the shared
  config default, which suits Art-Net) is treated as universe 1.
* Only data packets with DMX start code 0 are used; preview packets are ignored.
* Source priority: every source (CID) is tracked. The highest priority among
  live sources wins; sources at that same priority are merged HTP (highest
  takes precedence per slot). Sources that send Stream_Terminated, or that
  stop sending for 2.5 s (E1.31 "network data loss"), are dropped.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field

from . import DMX_SLOTS, FrameCallback, pad_frame
from .artnet import make_udp_socket

log = logging.getLogger(__name__)

SACN_PORT = 5568
ACN_PACKET_ID = b"ASC-E1.17\x00\x00\x00"
VECTOR_ROOT_E131_DATA = 0x00000004
VECTOR_E131_DATA_PACKET = 0x00000002
VECTOR_DMP_SET_PROPERTY = 0x02

OPT_PREVIEW = 0x80
OPT_STREAM_TERMINATED = 0x40

SOURCE_TIMEOUT = 2.5  # seconds, E1.31 network data loss
DEFAULT_PRIORITY = 100

_DATA_OFFSET = 126  # first DMX slot (after the start code at 125)


def effective_universe(universe: int) -> int:
    """Map the config value to a valid sACN universe (1-63999)."""
    return min(max(int(universe), 1), 63999)


def multicast_group(universe: int) -> str:
    u = effective_universe(universe)
    return f"239.255.{(u >> 8) & 0xFF}.{u & 0xFF}"


# ---------------------------------------------------------------------------
# Pure packet helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SacnPacket:
    cid: bytes
    source_name: str
    priority: int
    sequence: int
    options: int
    universe: int
    start_code: int
    data: bytes

    @property
    def terminated(self) -> bool:
        return bool(self.options & OPT_STREAM_TERMINATED)

    @property
    def preview(self) -> bool:
        return bool(self.options & OPT_PREVIEW)


def parse_sacn(packet: bytes) -> SacnPacket | None:
    """Parse an E1.31 data packet; returns None for other / malformed packets
    (including E1.31 sync and universe discovery packets)."""
    if len(packet) < _DATA_OFFSET:
        return None
    if packet[0:2] != b"\x00\x10" or packet[4:16] != ACN_PACKET_ID:
        return None
    (root_vector,) = struct.unpack_from(">I", packet, 18)
    if root_vector != VECTOR_ROOT_E131_DATA:
        return None
    cid = bytes(packet[22:38])
    (frame_vector,) = struct.unpack_from(">I", packet, 40)
    if frame_vector != VECTOR_E131_DATA_PACKET:
        return None
    source_name = bytes(packet[44:108]).split(b"\x00", 1)[0].decode("utf-8", "replace")
    priority = packet[108]
    sequence = packet[111]
    options = packet[112]
    (universe,) = struct.unpack_from(">H", packet, 113)
    if packet[117] != VECTOR_DMP_SET_PROPERTY or packet[118] != 0xA1:
        return None
    (count,) = struct.unpack_from(">H", packet, 123)  # start code + slots
    if count < 1 or count > DMX_SLOTS + 1:
        return None
    start_code = packet[125]
    data = bytes(packet[_DATA_OFFSET : _DATA_OFFSET + count - 1])
    return SacnPacket(cid, source_name, priority, sequence, options, universe, start_code, data)


def build_sacn(
    universe: int,
    data: bytes,
    *,
    cid: bytes = b"\x01" * 16,
    source_name: str = "test",
    priority: int = DEFAULT_PRIORITY,
    sequence: int = 0,
    options: int = 0,
    start_code: int = 0,
) -> bytes:
    """Build an E1.31 data packet (tests / diagnostics)."""
    data = bytes(data[:DMX_SLOTS])
    slots = len(data) + 1
    dmp_len = 10 + slots
    framing_len = 77 + dmp_len
    root_len = 22 + framing_len

    def fl(length: int) -> bytes:  # flags (0x7) + 12-bit length
        return struct.pack(">H", 0x7000 | length)

    p = bytearray()
    p += b"\x00\x10\x00\x00" + ACN_PACKET_ID
    p += fl(root_len) + struct.pack(">I", VECTOR_ROOT_E131_DATA) + cid[:16].ljust(16, b"\x00")
    p += fl(framing_len) + struct.pack(">I", VECTOR_E131_DATA_PACKET)
    name = source_name.encode("utf-8")[:63]
    p += name + bytes(64 - len(name))
    p += bytes([priority & 0xFF]) + b"\x00\x00" + bytes([sequence & 0xFF, options & 0xFF])
    p += struct.pack(">H", universe)
    p += fl(dmp_len) + bytes([VECTOR_DMP_SET_PROPERTY, 0xA1]) + struct.pack(">HHH", 0, 1, slots)
    p += bytes([start_code]) + data
    return bytes(p)


# ---------------------------------------------------------------------------
# Source arbitration (pure, testable)
# ---------------------------------------------------------------------------
@dataclass
class _Source:
    name: str
    priority: int
    data: bytes
    last_seen: float = field(default=0.0)


class SourceMerger:
    """Tracks sACN sources for one universe and produces the winning frame."""

    def __init__(self, timeout: float = SOURCE_TIMEOUT) -> None:
        self.timeout = timeout
        self.sources: dict[bytes, _Source] = {}

    def update(self, pkt: SacnPacket, now: float) -> bytes | None:
        """Feed a packet; return the merged 512-slot frame, or None if the
        packet contributes nothing (non-zero start code, preview, ...)."""
        if pkt.terminated:
            self.sources.pop(pkt.cid, None)
            return self.merged(now) if self.sources else None
        if pkt.start_code != 0 or pkt.preview:
            return None
        self.sources[pkt.cid] = _Source(pkt.source_name, pkt.priority, pad_frame(pkt.data), now)
        return self.merged(now)

    def expire(self, now: float) -> None:
        for cid in [c for c, s in self.sources.items() if now - s.last_seen > self.timeout]:
            del self.sources[cid]

    def merged(self, now: float) -> bytes | None:
        self.expire(now)
        if not self.sources:
            return None
        top = max(s.priority for s in self.sources.values())
        winners = [s.data for s in self.sources.values() if s.priority == top]
        if len(winners) == 1:
            return winners[0]
        return bytes(max(slot) for slot in zip(*winners))  # HTP merge

    def describe(self, now: float) -> list[dict]:
        self.expire(now)
        return [
            {"cid": cid.hex(), "name": s.name, "priority": s.priority,
             "age": round(now - s.last_seen, 2)}
            for cid, s in self.sources.items()
        ]


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class SacnReceiver(asyncio.DatagramProtocol):
    """Receives one sACN universe and calls `on_frame(512 bytes)` per packet."""

    source = "sacn"

    def __init__(
        self,
        universe: int,
        on_frame: FrameCallback,
        bind_ip: str = "0.0.0.0",
        port: int = SACN_PORT,
        join_multicast: bool = True,
    ) -> None:
        self.universe = effective_universe(universe)
        self.on_frame = on_frame
        self.bind_ip = bind_ip or "0.0.0.0"
        self.port = port
        self.join_multicast = join_multicast
        self.bound_port: int | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.merger = SourceMerger()
        self.packets_total = 0
        self.multicast_joined = False
        self.multicast_error: str | None = None
        self.other_universes: dict[int, float] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # Always bind to all interfaces: on macOS/Linux a socket bound to a
        # unicast address never sees multicast. bind_ip selects the interface
        # for the multicast membership instead.
        sock = make_udp_socket("", self.port)
        self.bound_port = sock.getsockname()[1]
        if self.join_multicast:
            self._join(sock)
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        log.info("sACN listening on UDP %s, universe %d (multicast %s)",
                 self.bound_port, self.universe,
                 "joined" if self.multicast_joined else f"unavailable: {self.multicast_error}")

    def _join(self, sock: socket.socket) -> None:
        group = multicast_group(self.universe)
        try:
            mreq = socket.inet_aton(group) + socket.inet_aton(self.bind_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.multicast_joined = True
            self.multicast_error = None
        except OSError as exc:
            # Still useful: unicast sACN (most nodes/consoles can target an IP).
            self.multicast_joined = False
            self.multicast_error = f"could not join {group}: {exc}; only unicast sACN will be received"
            log.warning("sACN %s", self.multicast_error)

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def info(self) -> dict:
        now = time.monotonic()
        return {
            "port": self.bound_port,
            "effective_universe": self.universe,
            "multicast_group": multicast_group(self.universe),
            "multicast_joined": self.multicast_joined,
            "multicast_error": self.multicast_error,
            "packets_total": self.packets_total,
            "sources": self.merger.describe(now),
            "other_universes": sorted(u for u, t in self.other_universes.items() if now - t < 10),
        }

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        pkt = parse_sacn(data)
        if pkt is None:
            return
        self.packets_total += 1
        now = time.monotonic()
        if pkt.universe != self.universe:
            self.other_universes[pkt.universe] = now
            return
        frame = self.merger.update(pkt, now)
        if frame is None:
            return
        try:
            self.on_frame(frame)
        except Exception:
            log.exception("sACN frame handler failed")

    def error_received(self, exc: Exception) -> None:
        log.debug("sACN socket error: %s", exc)
