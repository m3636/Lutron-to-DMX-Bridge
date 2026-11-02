"""DMX512 input: Art-Net, sACN (E1.31) and Enttec DMX USB Pro receivers plus the
engine that maps DMX channels onto Lutron zones.

Receivers deliver whole universes (always 512 slots, zero padded) to a
`FrameCallback`. The `DmxEngine` (lutron_to_dmx_bridge.dmx.engine) owns one receiver,
turns each frame into zone levels and forwards them to the active driver.

Hardware notes
--------------
* Network input is the primary path. An Art-Net / sACN node such as the
  PKnight CR021R converts a physical DMX cable into network packets.
* The Enttec *Open* DMX USB (plain FTDI chip, no microcontroller) is
  output-only: it physically cannot receive DMX. USB input needs an
  Enttec DMX USB Pro or a Pro-protocol-compatible receiver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

#: Number of slots in one DMX512 universe.
DMX_SLOTS = 512

#: Called with exactly DMX_SLOTS bytes of slot data (start code excluded).
FrameCallback = Callable[[bytes], None]


def pad_frame(data: bytes | bytearray | memoryview) -> bytes:
    """Return `data` truncated / zero padded to exactly 512 slots."""
    b = bytes(data[:DMX_SLOTS])
    if len(b) < DMX_SLOTS:
        b += bytes(DMX_SLOTS - len(b))
    return b


if TYPE_CHECKING:  # pragma: no cover
    from .engine import DmxEngine, auto_assign, validate_mappings

__all__ = [
    "DMX_SLOTS",
    "FrameCallback",
    "pad_frame",
    "DmxEngine",
    "auto_assign",
    "validate_mappings",
]


def __getattr__(name: str) -> Any:
    # Lazy re-exports so importing a receiver module never drags in the engine.
    if name in ("DmxEngine", "auto_assign", "validate_mappings"):
        from . import engine

        return getattr(engine, name)
    raise AttributeError(name)
