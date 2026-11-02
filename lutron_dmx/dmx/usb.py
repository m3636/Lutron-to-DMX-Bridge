"""USB DMX input using the Enttec DMX USB Pro serial protocol.

Supported: Enttec DMX USB Pro / Pro Mk2 and Pro-protocol-compatible receivers
(e.g. DMXking ultraDMX). The widget appears as a virtual serial port
("COM3" on Windows, "/dev/tty.usbserial-XXXX" on macOS).

NOT supported: the Enttec **Open** DMX USB and similar "dumb" FTDI dongles.
They have no microcontroller and their hardware only drives the DMX line
(RS-485 transmit). They are output-only and physically cannot receive DMX.
For DMX input with that setup, use an Art-Net / sACN node (e.g. PKnight
CR021R) and select the "artnet" or "sacn" source instead.

Pro protocol framing:  0x7E | label | len LSB | len MSB | payload[len] | 0xE7
  label 5  Received DMX Packet:   payload = status byte, start code, slots...
  label 8  Receive DMX on Change: payload = 1 byte (0 = send every packet,
           1 = only send changes as label 9)
  label 9  Received DMX Change Of State (handled for completeness)
  label 3  Get Widget Parameters (used as a "are you a Pro?" probe)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from . import DMX_SLOTS, FrameCallback, pad_frame

log = logging.getLogger(__name__)

SOM = 0x7E
EOM = 0xE7

LABEL_GET_PARAMS = 3
LABEL_RECEIVED_DMX = 5
LABEL_RECV_ON_CHANGE = 8
LABEL_CHANGE_OF_STATE = 9

#: Largest payload the protocol allows (params reply / 513 slots + status).
MAX_PAYLOAD = 600

OPEN_DMX_MESSAGE = (
    "The selected device looks like an Enttec Open DMX USB (or another plain FTDI "
    "DMX dongle). These are output-only: the hardware cannot receive DMX. Use an "
    "Enttec DMX USB Pro (or Pro-compatible receiver), or an Art-Net / sACN node such "
    "as the PKnight CR021R with the 'artnet' or 'sacn' DMX source."
)

USB_SOURCE_NOTE = (
    "USB input requires an Enttec DMX USB Pro or Pro-compatible receiver. "
    "The Enttec Open DMX USB is output-only and cannot receive DMX."
)


class OpenDmxNotSupported(RuntimeError):
    """Raised when an output-only Open DMX style device is selected for input."""


# ---------------------------------------------------------------------------
# Pure protocol helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProMessage:
    label: int
    payload: bytes


def build_message(label: int, payload: bytes = b"") -> bytes:
    n = len(payload)
    return bytes([SOM, label & 0xFF, n & 0xFF, (n >> 8) & 0xFF]) + bytes(payload) + bytes([EOM])


class ProParser:
    """Incremental DMX USB Pro message parser. Feed it arbitrary byte chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_messages = 0

    def feed(self, chunk: bytes) -> list[ProMessage]:
        self._buf += chunk
        out: list[ProMessage] = []
        buf = self._buf
        while True:
            start = buf.find(SOM)
            if start < 0:
                buf.clear()
                break
            if start:
                del buf[:start]
            if len(buf) < 4:
                break
            length = buf[2] | (buf[3] << 8)
            if length > MAX_PAYLOAD:
                # Not a real header: skip this 0x7E and resync on the next one.
                self.bad_messages += 1
                del buf[:1]
                continue
            total = 4 + length + 1
            if len(buf) < total:
                break
            if buf[total - 1] != EOM:
                self.bad_messages += 1
                del buf[:1]
                continue
            out.append(ProMessage(buf[1], bytes(buf[4 : 4 + length])))
            del buf[:total]
        return out


class ProFrameDecoder:
    """Turns Pro messages into full 512-slot frames (start code 0 only)."""

    def __init__(self) -> None:
        self.frame = bytearray(DMX_SLOTS)
        self.overruns = 0

    def handle(self, msg: ProMessage) -> bytes | None:
        if msg.label == LABEL_RECEIVED_DMX:
            if len(msg.payload) < 2:
                return None
            status, start_code = msg.payload[0], msg.payload[1]
            if status & 0x03:  # bit0 queue overflow, bit1 overrun: frame unreliable
                self.overruns += 1
                return None
            if start_code != 0:
                return None
            self.frame[:] = pad_frame(msg.payload[2:])
            return bytes(self.frame)
        if msg.label == LABEL_CHANGE_OF_STATE and len(msg.payload) >= 6:
            # payload: block number (8-slot blocks, slot 0 = start code),
            # 5-byte change bitmap covering 40 slots, then the changed values.
            block = msg.payload[0]
            bitmap = msg.payload[1:6]
            values = msg.payload[6:]
            vi = 0
            for bit in range(40):
                if bitmap[bit // 8] & (1 << (bit % 8)):
                    if vi >= len(values):
                        break
                    slot = block * 8 + bit  # 0 = start code
                    if 1 <= slot <= DMX_SLOTS:
                        self.frame[slot - 1] = values[vi]
                    vi += 1
            return bytes(self.frame)
        return None


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------
def _classify(info: object) -> tuple[str, bool | None, str]:
    text = " ".join(
        str(getattr(info, a, "") or "") for a in ("description", "product", "manufacturer")
    ).lower()
    vid = getattr(info, "vid", None)
    if "open dmx" in text:
        return "open_dmx", False, OPEN_DMX_MESSAGE
    if "dmx usb pro" in text or "dmxusb pro" in text or "ultradmx" in text:
        return "dmx_usb_pro", True, "DMX USB Pro compatible: can receive DMX."
    if vid == 0x0403:  # FTDI: could be a Pro clone or an output-only Open DMX
        return "ftdi", None, (
            "FTDI serial device: works only if it is a DMX USB Pro compatible "
            "receiver. An Enttec Open DMX USB is output-only."
        )
    return "serial", None, "Generic serial port."


def list_serial_ports() -> list[dict]:
    """Serial ports for the GUI, with a best-guess of whether each can receive DMX."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        return []
    ports = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        kind, can_receive, note = _classify(p)
        ports.append({
            "device": p.device,
            "description": p.description,
            "manufacturer": p.manufacturer,
            "product": p.product,
            "serial_number": p.serial_number,
            "vid": p.vid,
            "pid": p.pid,
            "kind": kind,
            "can_receive": can_receive,
            "note": note,
        })
    return ports


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class UsbProReceiver:
    """Reads a DMX USB Pro on a background thread and calls `on_frame` on the
    asyncio loop."""

    source = "usb"

    def __init__(
        self,
        serial_port: str,
        on_frame: FrameCallback,
        *,
        probe_timeout: float = 1.5,
        serial_factory=None,
    ) -> None:
        self.serial_port = serial_port
        self.on_frame = on_frame
        self.probe_timeout = probe_timeout
        self._serial_factory = serial_factory
        self._serial = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.parser = ProParser()
        self.decoder = ProFrameDecoder()
        self.messages = 0
        self.error: str | None = None

    def _open(self):
        if self._serial_factory is not None:
            return self._serial_factory(self.serial_port)
        import serial

        # The Pro is a virtual COM port; the baud rate is ignored by the widget.
        return serial.Serial(self.serial_port, baudrate=57600, timeout=0.1, write_timeout=1)

    def _check_not_open_dmx(self) -> None:
        try:
            from serial.tools import list_ports

            for p in list_ports.comports():
                if p.device == self.serial_port and _classify(p)[0] == "open_dmx":
                    raise OpenDmxNotSupported(OPEN_DMX_MESSAGE)
        except ImportError:  # pragma: no cover
            pass

    async def start(self) -> None:
        if not self.serial_port:
            raise RuntimeError("No serial port selected for USB DMX input. " + USB_SOURCE_NOTE)
        self._check_not_open_dmx()
        self._loop = asyncio.get_running_loop()
        self._serial = await asyncio.to_thread(self._open)
        try:
            await asyncio.to_thread(self._probe_and_configure)
        except Exception:
            await asyncio.to_thread(self._serial.close)
            self._serial = None
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dmx-usb-rx", daemon=True)
        self._thread.start()

    def _probe_and_configure(self) -> None:
        """Ask the widget for its parameters. A Pro answers; an Open DMX never
        sends anything, which is how we tell them apart."""
        ser = self._serial
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(build_message(LABEL_GET_PARAMS, b"\x00\x00"))
        deadline = time.monotonic() + self.probe_timeout
        got_reply = False
        while time.monotonic() < deadline and not got_reply:
            chunk = ser.read(256)
            if chunk:
                msgs = self.parser.feed(chunk)
                got_reply = bool(msgs)
                for m in msgs:  # keep any DMX that arrived during the probe
                    self._handle(m, deliver=False)
        if not got_reply:
            raise OpenDmxNotSupported(
                f"No reply from {self.serial_port} to a DMX USB Pro request. " + OPEN_DMX_MESSAGE
            )
        # Receive every packet (label 5), not just changes (label 9).
        ser.write(build_message(LABEL_RECV_ON_CHANGE, b"\x00"))

    def _handle(self, msg: ProMessage, deliver: bool = True) -> None:
        self.messages += 1
        frame = self.decoder.handle(msg)
        if frame is not None and deliver and self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver, frame)

    def _deliver(self, frame: bytes) -> None:
        try:
            self.on_frame(frame)
        except Exception:
            log.exception("USB DMX frame handler failed")

    def _run(self) -> None:
        ser = self._serial
        while not self._stop.is_set():
            try:
                chunk = ser.read(max(1, getattr(ser, "in_waiting", 0) or 1))
            except Exception as exc:  # unplugged, etc.
                self.error = f"Serial read failed: {exc}"
                log.error("USB DMX: %s", self.error)
                break
            if chunk:
                for msg in self.parser.feed(chunk):
                    self._handle(msg)

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 2.0)
            self._thread = None
        if self._serial is not None:
            try:
                await asyncio.to_thread(self._serial.close)
            except Exception:
                pass
            self._serial = None

    def info(self) -> dict:
        return {
            "serial_port": self.serial_port,
            "messages": self.messages,
            "bad_messages": self.parser.bad_messages,
            "overruns": self.decoder.overruns,
            "note": USB_SOURCE_NOTE,
            "error": self.error,
        }
