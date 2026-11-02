"""PyInstaller entry point for the double-clickable app (a plain script so the bundle can import the package)."""

import sys

from lutron_dmx.launcher import main

if __name__ == "__main__":
    sys.exit(main())
