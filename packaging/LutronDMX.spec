# PyInstaller spec for the double-clickable app: "Lutron DMX.app" (macOS) / "Lutron DMX\Lutron DMX.exe" (Windows).
# Build it with `python packaging/build.py` rather than calling PyInstaller directly.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is provided by PyInstaller)
sys.path.insert(0, str(ROOT))
from lutron_dmx import __version__  # noqa: E402

NAME = "Lutron DMX"
ICONS = ROOT / "packaging" / "icons"

datas = [
    (str(ROOT / "lutron_dmx" / "web" / "static"), "lutron_dmx/web/static"),
    (str(ROOT / "lutron_dmx" / "drivers" / "sample_data"), "lutron_dmx/drivers/sample_data"),
    (str(ROOT / "lutron_dmx" / "assets"), "lutron_dmx/assets"),
    (str(ROOT / "LICENSE"), "licenses"),
    (str(ROOT / "NOTICE"), "licenses"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "licenses"),
]
binaries = []
hiddenimports = []
for pkg in ("lutron_dmx", "uvicorn", "websockets", "zeroconf", "ifaddr"):
    hiddenimports += collect_submodules(pkg)
d, b, h = collect_all("pylutron_caseta")
datas += d
binaries += b
hiddenimports += h

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "app_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,  # a window, not a terminal: the log window is the app's face
    icon=str(ICONS / "LutronDMX.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{NAME}.app",
        icon=str(ICONS / "LutronDMX.icns"),
        bundle_identifier="com.m3636.lutrondmx",
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            # macOS 15+ asks before an app may talk to devices on the local network (the Lutron
            # controller, Art-Net nodes); this is the text of that question.
            "NSLocalNetworkUsageDescription":
                "Lutron DMX connects to your Lutron controller and to Art-Net / sACN lighting equipment on your network.",
            "NSBonjourServices": ["_lutron._tcp"],
        },
    )
