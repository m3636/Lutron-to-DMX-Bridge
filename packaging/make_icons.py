"""Regenerate every icon from the one master logo, packaging/icons/logo-source.png (needs Pillow).

    python -m pip install pillow && python packaging/make_icons.py

Writes the macOS .icns, the Windows .ico, the log window's icon, and the web GUI's logo/favicon.
The results are committed, so this is only needed when the logo changes.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "packaging" / "icons"
SIZE = 1024


def master() -> Image.Image:
    """The logo cropped to its artwork and centred on a square transparent canvas with a small margin."""
    src = Image.open(ICONS / "logo-source.png").convert("RGBA")
    art = src.crop(src.getbbox())
    side = round(SIZE * 0.92)
    art.thumbnail((side, side), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(art, ((SIZE - art.width) // 2, (SIZE - art.height) // 2), art)
    return canvas


def small(img: Image.Image, px: int) -> Image.Image:
    return img.resize((px, px), Image.LANCZOS)


if __name__ == "__main__":
    img = master()
    small(img, 256).save(ROOT / "lutron_dmx" / "assets" / "icon.png")
    static = ROOT / "lutron_dmx" / "web" / "static"
    small(img, 128).save(static / "logo.png", optimize=True)  # top-left logo and favicon
    small(img, 180).save(static / "apple-touch-icon.png", optimize=True)
    img.save(ICONS / "LutronDMX.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    img.save(ICONS / "LutronDMX.icns")
