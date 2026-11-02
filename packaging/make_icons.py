"""Draw the app's logo and regenerate every icon from it (needs Pillow).

    python -m pip install pillow && python packaging/make_icons.py

The logo is original artwork drawn from the shapes below: a row of amber "fader" bars whose heights follow an
arch, standing on a bridge deck, on a dark rounded tile (levels crossing a bridge).  It uses no third-party
artwork and nothing from Lutron or any other brand.

Writes packaging/icons/logo.svg (the master), the macOS .icns, the Windows .ico, the log window's icon, and the
web GUI's logo/favicon.  The results are committed, so this is only needed when the logo changes.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "packaging" / "icons"
PKG = ROOT / "lutron_to_dmx_bridge"
SIZE = 1024  # the logo is designed on a 1024 x 1024 canvas
SS = 2  # supersampling factor for smooth edges

# --- geometry (1024 canvas) ---------------------------------------------------------------------
TILE = (40, 40, 984, 984)  # x0, y0, x1, y1 of the rounded tile
TILE_R = 210
TILE_TOP, TILE_BOTTOM = (0x24, 0x31, 0x5A), (0x0B, 0x10, 0x20)
BAR_TOP, BAR_BOTTOM = (0xFF, 0xD1, 0x66), (0xF0, 0x8A, 0x24)
DECK_COLOR = (0xE8, 0xEC, 0xF5)
DECK = (150, 722, 874, 782)  # x0, y0, x1, y1
DECK_R = 30
BARS = 7
BAR_W = 64
BAR_PITCH = 104
BAR_BASE = DECK[1] + 14  # bars stand on the deck (the deck is drawn over their feet)
BAR_MIN_H, BAR_MAX_H = 130, 460
CABLE_W = 16
CABLE_COLOR = DECK_COLOR
CABLE_LIFT = 38  # the cable arches this far above the bar tops, so the bars hang from it


def bars() -> list[tuple[float, float, float]]:
    """(x centre, y top, y bottom) of every bar; heights follow a parabola (the arch)."""
    out = []
    mid = (BARS - 1) / 2
    for i in range(BARS):
        t = (i - mid) / mid
        h = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * (1 - t * t)
        out.append((SIZE / 2 + (i - mid) * BAR_PITCH, BAR_BASE - h, BAR_BASE))
    return out


def cable() -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Quadratic Bezier (start, control, end) that arches over the bar tops: the bridge's main cable."""
    b = bars()
    (x0, top0, _), (x1, top1, _) = b[0], b[-1]
    top0, top1 = top0 - CABLE_LIFT, top1 - CABLE_LIFT
    peak = b[len(b) // 2][1] - CABLE_LIFT
    cy = (4 * peak - top0 - top1) / 2  # the curve's midpoint is (P0 + 2C + P2) / 4
    return (x0, top0), (SIZE / 2, cy), (x1, top1)


def svg() -> str:
    (p0x, p0y), (cx, cy), (p2x, p2y) = cable()
    rects = "\n".join(
        f'    <rect x="{x - BAR_W / 2:g}" y="{top:g}" width="{BAR_W}" height="{bottom - top:g}" rx="{BAR_W / 2}"/>'
        for x, top, bottom in bars())
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}">
  <title>Lutron To DMX Bridge</title>
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgb{TILE_TOP}"/><stop offset="1" stop-color="rgb{TILE_BOTTOM}"/>
    </linearGradient>
    <linearGradient id="bar" gradientUnits="userSpaceOnUse" x1="0" y1="{BAR_BASE - BAR_MAX_H}" x2="0" y2="{BAR_BASE}">
      <stop offset="0" stop-color="rgb{BAR_TOP}"/><stop offset="1" stop-color="rgb{BAR_BOTTOM}"/>
    </linearGradient>
  </defs>
  <rect x="{TILE[0]}" y="{TILE[1]}" width="{TILE[2] - TILE[0]}" height="{TILE[3] - TILE[1]}" rx="{TILE_R}" fill="url(#tile)"/>
  <g fill="url(#bar)">
{rects}
  </g>
  <path d="M{p0x:g} {p0y:g} Q{cx:g} {cy:g} {p2x:g} {p2y:g}" fill="none" stroke="rgb{CABLE_COLOR}" stroke-width="{CABLE_W}" stroke-linecap="round"/>
  <rect x="{DECK[0]}" y="{DECK[1]}" width="{DECK[2] - DECK[0]}" height="{DECK[3] - DECK[1]}" rx="{DECK_R}" fill="rgb{DECK_COLOR}"/>
</svg>
"""


def _gradient(size: int, y0: float, y1: float, top, bottom) -> Image.Image:
    """A vertical gradient filling the canvas; flat above y0 and below y1."""
    col = Image.new("RGB", (1, size))
    for y in range(size):
        t = min(1.0, max(0.0, (y - y0) / (y1 - y0)))
        col.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return col.resize((size, size))


def master() -> Image.Image:
    """The logo as a 1024 x 1024 RGBA image (same shapes as svg())."""
    n = SIZE * SS

    def s(v):
        return v * SS

    canvas = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    tile_mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle([s(v) for v in TILE], radius=s(TILE_R), fill=255)
    canvas.paste(_gradient(n, s(TILE[1]), s(TILE[3]), TILE_TOP, TILE_BOTTOM), (0, 0), tile_mask)

    bar_mask = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(bar_mask)
    for x, top, bottom in bars():
        d.rounded_rectangle([s(x - BAR_W / 2), s(top), s(x + BAR_W / 2), s(bottom)], radius=s(BAR_W / 2), fill=255)
    canvas.paste(_gradient(n, s(BAR_BASE - BAR_MAX_H), s(BAR_BASE), BAR_TOP, BAR_BOTTOM), (0, 0), bar_mask)

    d = ImageDraw.Draw(canvas)
    (p0x, p0y), (cx, cy), (p2x, p2y) = cable()
    r = s(CABLE_W / 2)
    steps = 600
    for k in range(steps + 1):
        t = k / steps
        x = (1 - t) ** 2 * p0x + 2 * (1 - t) * t * cx + t * t * p2x
        y = (1 - t) ** 2 * p0y + 2 * (1 - t) * t * cy + t * t * p2y
        d.ellipse([s(x) - r, s(y) - r, s(x) + r, s(y) + r], fill=CABLE_COLOR + (255,))
    d.rounded_rectangle([s(v) for v in DECK], radius=s(DECK_R), fill=DECK_COLOR + (255,))

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def small(img: Image.Image, px: int) -> Image.Image:
    return img.resize((px, px), Image.LANCZOS)


if __name__ == "__main__":
    (ICONS / "logo.svg").write_text(svg(), encoding="utf-8")
    img = master()
    img.save(ICONS / "logo-1024.png", optimize=True)
    small(img, 256).save(PKG / "assets" / "icon.png")
    static = PKG / "web" / "static"
    small(img, 128).save(static / "logo.png", optimize=True)  # top-left logo and favicon
    small(img, 180).save(static / "apple-touch-icon.png", optimize=True)
    img.save(ICONS / "LutronToDMXBridge.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    img.save(ICONS / "LutronToDMXBridge.icns")
