"""Build the double-clickable app with PyInstaller on the current OS, then check that it starts.

    python -m pip install -e . pyinstaller
    python packaging/build.py            (add --no-check to skip the start-up check)

Output (zipped, ready to share):
    macOS    dist/LutronToDMXBridge-<version>-macOS.zip     containing "Lutron To DMX Bridge.app"
    Windows  dist/LutronToDMXBridge-<version>-Windows.zip   containing the "Lutron To DMX Bridge" folder with "Lutron To DMX Bridge.exe"

The Python you build with must have tkinter (the python.org installers do; Homebrew needs
`brew install python-tk@3.12`), or the packaged app could not show its log window.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lutron_to_dmx_bridge import __version__  # noqa: E402
DIST = ROOT / "dist"
NAME = "Lutron To DMX Bridge"
OS_NAME = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")


def check_tkinter() -> None:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        hint = "  brew install python-tk@3.12   (then recreate your virtualenv)\n" if sys.platform == "darwin" else ""
        sys.exit("This Python has no tkinter, so the app's log window would be missing.\n" + hint +
                 "Use a Python that includes tkinter, e.g. one from python.org.")


def build() -> Path:
    shutil.rmtree(DIST / NAME, ignore_errors=True)
    shutil.rmtree(DIST / f"{NAME}.app", ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(DIST), "--workpath", str(ROOT / "build"), str(ROOT / "packaging" / "LutronToDMXBridge.spec")]
    print(" ".join(cmd), flush=True)
    if subprocess.call(cmd, cwd=ROOT):
        sys.exit("PyInstaller failed")
    if sys.platform == "darwin":
        app = DIST / f"{NAME}.app"
        shutil.rmtree(DIST / NAME, ignore_errors=True)  # PyInstaller also leaves the unbundled folder
        return app
    return DIST / NAME


def executable(built: Path) -> Path:
    if sys.platform == "darwin":
        return built / "Contents" / "MacOS" / NAME
    return built / (NAME + (".exe" if sys.platform == "win32" else ""))


def make_zip(built: Path) -> Path:
    zip_path = DIST / f"LutronToDMXBridge-{__version__}-{OS_NAME}.zip"
    zip_path.unlink(missing_ok=True)
    if sys.platform == "darwin":
        # ditto keeps the executable bits and symlinks a .app needs; a plain zip would break it
        subprocess.check_call(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(built), str(zip_path)])
    else:
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=built.parent, base_dir=built.name)
    return zip_path


def start_up_check(built: Path) -> bool:
    """Run the packaged app's own --self-check (starts the simulator, fetches the GUI, stops)."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "self-check.txt"  # a windowed app has no stdout, so it writes a file
        proc = subprocess.run([str(executable(built)), "--self-check", str(report)], timeout=180, capture_output=True)
        text = report.read_text(encoding="utf-8") if report.exists() else "(no report written)"
    print(text)
    return proc.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-check", action="store_true", help="don't start the built app to check it works")
    args = parser.parse_args()
    check_tkinter()
    built = build()
    if not args.no_check and not start_up_check(built):
        sys.exit("The built app failed its start-up check (see above).")
    zip_path = make_zip(built)
    print(f"\nBuilt {built}\nZipped {zip_path}")


if __name__ == "__main__":
    main()
