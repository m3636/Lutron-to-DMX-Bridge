# PyInstaller spec for the double-clickable app: "Lutron To DMX Bridge.app" (macOS) / "Lutron To DMX Bridge\Lutron To DMX Bridge.exe" (Windows).
# Build it with `python packaging/build.py` rather than calling PyInstaller directly.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is provided by PyInstaller)
sys.path.insert(0, str(ROOT))
from lutron_to_dmx_bridge import __version__  # noqa: E402

NAME = "Lutron To DMX Bridge"
ICONS = ROOT / "packaging" / "icons"

datas = [
    (str(ROOT / "lutron_to_dmx_bridge" / "web" / "static"), "lutron_to_dmx_bridge/web/static"),
    (str(ROOT / "lutron_to_dmx_bridge" / "drivers" / "sample_data"), "lutron_to_dmx_bridge/drivers/sample_data"),
    (str(ROOT / "lutron_to_dmx_bridge" / "assets"), "lutron_to_dmx_bridge/assets"),
    (str(ROOT / "LICENSE"), "licenses"),
    (str(ROOT / "NOTICE"), "licenses"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "licenses"),
]
binaries = []
hiddenimports = []
for pkg in ("lutron_to_dmx_bridge", "uvicorn", "websockets", "zeroconf", "ifaddr"):
    hiddenimports += collect_submodules(pkg)
# CA bundle the update check verifies GitHub's certificate against (the frozen Python has no system store)
datas += collect_data_files("certifi")
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
    icon=str(ICONS / "LutronToDMXBridge.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{NAME}.app",
        icon=str(ICONS / "LutronToDMXBridge.icns"),
        bundle_identifier="com.m3636.lutrontodmxbridge",
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            # macOS 15+ asks before an app may talk to devices on the local network (the Lutron
            # controller, Art-Net nodes); this is the text of that question.
            "NSLocalNetworkUsageDescription":
                "Lutron To DMX Bridge connects to your Lutron controller and to Art-Net / sACN lighting equipment on your network.",
            "NSBonjourServices": ["_lutron._tcp"],
        },
    )
