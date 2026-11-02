"""Art-Net (Art-Net 4) receiver.

Only what a DMX *input* needs is implemented:

* ArtDmx (OpCode 0x5000) — DMX data for one 15-bit Port-Address.
* ArtPoll (0x2000) — answered with an ArtPollReply (0x2100) so consoles and
  nodes (e.g. the PKnight CR021R web UI, or a lighting desk) can discover us.

The configured universe is the full 15-bit Port-Address
(Net << 8 | Sub-Net << 4 | Universe), i.e. 0-32767; 0 is "0:0:0".

Parsing / building is done by pure functions so it can be unit tested without
sockets.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
import time
from dataclasses import dataclass

from . import DMX_SLOTS, FrameCallback, pad_frame

log = logging.getLogger(__name__)

ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
PROTOCOL_VERSION = 14

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000

SHORT_NAME = "LutronToDMXBridge"
LONG_NAME = "Lutron To DMX Bridge"

_HEADER = struct.Struct("<8sH")  # ID, OpCode (little endian)


# ---------------------------------------------------------------------------
# Pure packet helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ArtDmx:
    sequence: int
    physical: int
    port_address: int  # 15-bit: Net(7) | SubNet(4) | Universe(4)
    data: bytes  # raw slot data as received (2-512 bytes)


def parse_opcode(packet: bytes) -> int | None:
    """Return the OpCode of an Art-Net packet, or None if it isn't Art-Net."""
    if len(packet) < 10 or packet[:8] != ARTNET_ID:
        return None
    return _HEADER.unpack_from(packet)[1]


def parse_artdmx(packet: bytes) -> ArtDmx | None:
    """Parse an ArtDmx packet. Returns None for anything else / malformed data."""
    if parse_opcode(packet) != OP_DMX or len(packet) < 18:
        return None
    # 10-11 ProtVer (hi, lo) is not checked: old senders use lower versions.
    sequence = packet[12]
    physical = packet[13]
    sub_uni = packet[14]
    net = packet[15] & 0x7F
    length = (packet[16] << 8) | packet[17]
    if length < 1 or length > DMX_SLOTS:
        return None
    data = packet[18 : 18 + length]
    if len(data) < length:  # truncated datagram
        return None
    return ArtDmx(sequence, physical, (net << 8) | sub_uni, bytes(data))


def build_artdmx(port_address: int, data: bytes, sequence: int = 0, physical: int = 0) -> bytes:
    """Build an ArtDmx packet (used by tests and handy for diagnostics)."""
    data = bytes(data[:DMX_SLOTS])
    if len(data) % 2:  # spec: length must be even
        data += b"\x00"
    if len(data) < 2:
        data = data.ljust(2, b"\x00")
    return (
        _HEADER.pack(ARTNET_ID, OP_DMX)
        + bytes([0, PROTOCOL_VERSION, sequence & 0xFF, physical & 0xFF,
                 port_address & 0xFF, (port_address >> 8) & 0x7F])
        + struct.pack(">H", len(data))
        + data
    )


def build_artpoll(flags: int = 0, priority: int = 0) -> bytes:
    """Build an ArtPoll packet (tests / diagnostics)."""
    return _HEADER.pack(ARTNET_ID, OP_POLL) + bytes([0, PROTOCOL_VERSION, flags, priority])


def _fixed(text: str, size: int) -> bytes:
    """Null-terminated, null-padded ASCII field."""
    raw = text.encode("ascii", "replace")[: size - 1]
    return raw + bytes(size - len(raw))


def build_artpoll_reply(
    ip: str,
    port_address: int,
    *,
    receiving: bool = False,
    node_report: str = "",
    mac: bytes = b"\x00" * 6,
    short_name: str = SHORT_NAME,
    long_name: str = LONG_NAME,
) -> bytes:
    """Build a 239-byte ArtPollReply describing one DMX *output* port.

    In Art-Net terms a device that consumes DMX from the network is an
    "output" (it outputs DMX from Art-Net), so our single port is flagged as
    an output on `port_address`.
    """
    port_address &= 0x7FFF
    net = (port_address >> 8) & 0x7F
    sub = (port_address >> 4) & 0x0F
    uni = port_address & 0x0F
    try:
        ip_bytes = socket.inet_aton(ip)
    except OSError:
        ip_bytes = b"\x00" * 4

    p = bytearray()
    p += _HEADER.pack(ARTNET_ID, OP_POLL_REPLY)
    p += ip_bytes
    p += struct.pack("<H", ARTNET_PORT)
    p += bytes([0, 1])  # VersInfo hi, lo (firmware version)
    p += bytes([net, sub])  # NetSwitch, SubSwitch
    p += bytes([0x00, 0xFF])  # OemHi, Oem (0x00FF = "unknown / generic")
    p += bytes([0])  # UbeaVersion
    p += bytes([0xD0])  # Status1: indicators normal, port-address set by front panel
    p += bytes([0, 0])  # EstaMan lo, hi (unregistered)
    p += _fixed(short_name, 18)
    p += _fixed(long_name, 64)
    p += _fixed(node_report or "#0001 [0000] OK", 64)
    p += struct.pack(">H", 1)  # NumPorts
    p += bytes([0x80, 0, 0, 0])  # PortTypes: port 0 can output DMX512
    p += bytes([0, 0, 0, 0])  # GoodInput
    p += bytes([0x80 if receiving else 0x00, 0, 0, 0])  # GoodOutput: data being output
    p += bytes([0, 0, 0, 0])  # SwIn
    p += bytes([uni, 0, 0, 0])  # SwOut
    p += bytes([0, 0, 0])  # AcnPriority, SwMacro, SwRemote
    p += bytes(3)  # Spare
    p += bytes([0x00])  # Style: StNode
    p += bytes(mac[:6]).ljust(6, b"\x00")
    p += ip_bytes  # BindIp
    p += bytes([1])  # BindIndex
    p += bytes([0x08])  # Status2: supports 15-bit Port-Address
    p += bytes([0x80 if receiving else 0, 0, 0, 0])  # GoodOutputB
    p += bytes([0])  # Status3
    p += bytes(6)  # DefaultRespUID
    p += bytes(2)  # User
    p += bytes(2)  # RefreshRate
    p += bytes(11)  # Filler
    assert len(p) == 239, len(p)
    return bytes(p)


# ---------------------------------------------------------------------------
# Socket helpers (shared with sacn.py)
# ---------------------------------------------------------------------------
def make_udp_socket(bind_host: str, port: int) -> socket.socket:
    """Non-blocking UDP socket with address (and, if available, port) reuse so
    other lighting software on the same machine can share the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # macOS / BSD / Linux; not Windows
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((bind_host, port))
        sock.setblocking(False)
    except Exception:
        sock.close()
        raise
    return sock


def broadcast_bind_host(bind_ip: str) -> str:
    """Address to bind a socket that must also receive broadcasts.

    On macOS and Linux a socket bound to a specific unicast address does NOT
    receive broadcast datagrams (Art-Net is usually sent to 2.255.255.255 or
    the subnet broadcast), so we bind to all interfaces there. Windows does
    deliver broadcasts to interface-bound sockets, so bind_ip is honoured.

    A loopback address is always honoured: nothing broadcasts on loopback, and
    binding it specifically makes datagrams to 127.0.0.1 reach us even when
    another program (e.g. QLC+ on the same computer) holds a wildcard socket
    on the same port. Without it, which of the two receives is decided by the
    order they started in.
    """
    if not bind_ip or bind_ip == "0.0.0.0":
        return ""
    if bind_ip.startswith("127."):
        return bind_ip
    return bind_ip if sys.platform == "win32" else ""


def local_ip_for(remote_ip: str) -> str:
    """The local address the OS would use to reach `remote_ip` (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, ARTNET_PORT))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class ArtNetReceiver(asyncio.DatagramProtocol):
    """Receives one Art-Net universe and calls `on_frame(512 bytes)` per ArtDmx."""

    source = "artnet"

    def __init__(
        self,
        universe: int,
        on_frame: FrameCallback,
        bind_ip: str = "0.0.0.0",
        port: int = ARTNET_PORT,
    ) -> None:
        self.universe = universe & 0x7FFF
        self.on_frame = on_frame
        self.bind_ip = bind_ip or "0.0.0.0"
        self.port = port
        self.bound_port: int | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.packets_total = 0
        self.polls_answered = 0
        self.last_sender: str | None = None
        #: other Port-Addresses seen recently (helps diagnose a wrong universe)
        self.other_universes: dict[int, float] = {}
        self._ip_cache: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = make_udp_socket(broadcast_bind_host(self.bind_ip), self.port)
        self.bound_port = sock.getsockname()[1]
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        log.info("Art-Net listening on UDP %s, Port-Address %d", self.bound_port, self.universe)

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def info(self) -> dict:
        now = time.monotonic()
        return {
            "port": self.bound_port,
            "packets_total": self.packets_total,
            "polls_answered": self.polls_answered,
            "last_sender": self.last_sender,
            "other_universes": sorted(u for u, t in self.other_universes.items() if now - t < 10),
        }

    # -- DatagramProtocol ------------------------------------------------
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        op = parse_opcode(data)
        if op is None:
            return
        self.packets_total += 1
        if op == OP_DMX:
            pkt = parse_artdmx(data)
            if pkt is None:
                return
            if pkt.port_address != self.universe:
                self.other_universes[pkt.port_address] = time.monotonic()
                return
            self.last_sender = addr[0]
            try:
                self.on_frame(pad_frame(pkt.data))
            except Exception:  # never let a consumer bug kill the socket
                log.exception("Art-Net frame handler failed")
        elif op == OP_POLL:
            self._reply_poll(addr)

    def error_received(self, exc: Exception) -> None:  # e.g. ICMP port unreachable on Windows
        log.debug("Art-Net socket error: %s", exc)

    # -- ArtPoll ---------------------------------------------------------
    def _reply_poll(self, addr: tuple[str, int]) -> None:
        if self.transport is None:
            return
        if self.bind_ip != "0.0.0.0":
            ip = self.bind_ip
        else:
            ip = self._ip_cache.get(addr[0]) or self._ip_cache.setdefault(addr[0], local_ip_for(addr[0]))
        reply = build_artpoll_reply(ip, self.universe, receiving=self.last_sender is not None)
        # Art-Net 4: unicast the reply to the poller. Spec traffic is 6454->6454;
        # also answer the source port in case the poller used an ephemeral one.
        targets = {addr, (addr[0], ARTNET_PORT)}
        for target in targets:
            try:
                self.transport.sendto(reply, target)
            except OSError as exc:
                log.debug("ArtPollReply to %s failed: %s", target, exc)
        self.polls_answered += 1
