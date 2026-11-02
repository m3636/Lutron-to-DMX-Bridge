"""Lutron system drivers. Use `create_driver` to build one from config."""

from .base import DriverError, LevelCallback, LutronDriver

__all__ = ["DriverError", "LevelCallback", "LutronDriver", "create_driver"]


def create_driver(config: "DriverConfig") -> LutronDriver:  # noqa: F821
    from ..config import DriverConfig  # noqa: F401  (type only)

    kind = config.kind
    if kind == "simulator":
        from .simulator import SimulatorDriver

        return SimulatorDriver(config)
    if kind == "ra2":
        from .ra2 import RA2Driver

        return RA2Driver(config)
    if kind == "leap":
        from .leap import LeapDriver

        return LeapDriver(config)
    raise DriverError(f"Unknown driver kind: {kind!r}")
