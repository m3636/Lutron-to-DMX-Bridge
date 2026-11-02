"""Generate the QLC+ test workspace (Lutron-DMX-Test.qxw) and the matching app config.

The show is built from the simulator's inventory (lutron_dmx/drivers/sample_data/simulator_inventory.json)
so QLC+ and the app agree on the patch: every Lutron zone is one 1-channel dimmer, patched on
consecutive DMX addresses 1..N in inventory order, which is exactly what the app's
"Re-assign all" button does with the "intensity" parameter.

    python qlcplus/generate_show.py                              # loopback: QLC+ and the app on one computer
    python qlcplus/generate_show.py --interface 192.168.1.20     # QLC+ output on this NIC, broadcast
    python qlcplus/generate_show.py --interface 192.168.1.20 --target 192.168.1.50   # unicast to the app's computer

Writes Lutron-DMX-Test.qxw and app-home/config.json next to this script (loopback), or into
qlcplus/custom/ when --interface/--target are given, so the committed loopback files stay untouched.

To build a show for your own installation, give it the RadioRA 2 export (the Main Repeater serves it at
http://<repeater-ip>/DbXmlInfo.xml):

    python qlcplus/generate_show.py --db-xml DbXmlInfo.xml
    python qlcplus/generate_show.py --db-xml DbXmlInfo.xml --room Kitchen   # one room only, patched from DMX 1

That patches one 1-channel fixture per output on the DMX addresses the app's "Re-assign all" button gives
(areas in export order, outputs in export order within an area), with a fader or button per output grouped by
room. It writes Lutron-DMX-Show.qxw and patch-list.csv into qlcplus/custom/ (git-ignored: the export holds
your room names and installer details, so keep both out of version control).
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

HERE = Path(__file__).resolve().parent
INVENTORY = HERE.parent / "lutron_dmx" / "drivers" / "sample_data" / "simulator_inventory.json"
QLC_VERSION = "4.12.7"
NO_FUNCTION = 4294967295  # QLC+ "invalid function id"


def dmx(percent: float) -> int:
    return round(percent * 255 / 100)


@dataclass
class Zone:
    index: int  # QLC+ fixture id == position in the patch
    id: str  # Lutron zone id
    name: str
    area: str
    address: int  # DMX address, 1-based (as the app shows it)
    kind: str = "dimmer"  # ZoneType value: dimmer, switch, shade or fan
    area_id: str = ""

    @property
    def short(self) -> str:
        """Name without the numeric prefix, with camel-case words split so slider captions can wrap."""
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"^\d+\s*", "", self.name))


def patch_order(areas: dict[str, str], raw_zones: list[tuple[str, str, str, str]]) -> list[Zone]:
    """Patch (id, name, area id, kind) tuples in the order the GUI's "Re-assign all" uses:
    areas in inventory order, zones in inventory order within an area."""
    zones: list[Zone] = []
    for area_id in areas:
        for zid, name, zone_area, kind in raw_zones:
            if zone_area == area_id:
                n = len(zones)
                zones.append(Zone(n, zid, name, areas[area_id], n + 1, kind, area_id))
    return zones


def load_zones() -> list[Zone]:
    inv = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return patch_order(
        {a["id"]: a["name"] for a in inv["areas"]},
        [(z["id"], z["name"], z["area_id"], z.get("type", "dimmer")) for z in inv["zones"]])


def load_zones_from_db_xml(path: Path) -> list[Zone]:
    """Zones of a real RadioRA 2 export, parsed by the same code the app uses so both agree on ids and order."""
    from lutron_dmx.drivers.ra2 import parse_db_xml

    inv = parse_db_xml(Path(path).read_bytes())
    return patch_order(
        {a.id: a.name for a in inv.areas},
        [(z.id, z.name, z.area_id or "", z.type.value) for z in inv.zones])


def restrict_to_room(zones: list[Zone], room: str) -> list[Zone]:
    """Only the zones of one room, re-patched from DMX 1. `room` is the area name, with or without its
    number prefix ("Kitchen" or "112 Kitchen"), case-insensitive; a part of a name is accepted if it is unambiguous."""
    wanted = room.strip().lower()
    areas = {z.area_id: z.area for z in zones}
    exact = [a for a, n in areas.items()
             if wanted in (n.lower(), re.sub(r"^\w*?\d+[a-z]?\s+", "", n.lower()))]
    hits = exact or [a for a, n in areas.items() if wanted in n.lower()]
    if len(hits) != 1:
        raise SystemExit(f"--room {room!r} matches {len(hits)} rooms; "
                         f"{'choose one of' if hits else 'rooms are'}: "
                         + ", ".join(areas[a] for a in (hits or areas)))
    return patch_order({hits[0]: areas[hits[0]]},
                       [(z.id, z.name, z.area_id, z.kind) for z in zones if z.area_id == hits[0]])


# ---------------------------------------------------------------------------
# Engine: fixtures and functions
# ---------------------------------------------------------------------------
@dataclass
class Func:
    id: int
    kind: str
    name: str
    xml: str
    path: str = ""


@dataclass
class Show:
    zones: list[Zone]
    funcs: list[Func] = field(default_factory=list)

    def next_id(self) -> int:
        return len(self.funcs)

    def scene(self, name: str, levels: dict[str, float], *, path: str, fade: int = 1500,
              hidden: bool = False) -> int:
        """`levels` maps zone id -> percent."""
        fid = self.next_id()
        by_id = {z.id: z for z in self.zones}
        vals = "".join(
            f'  <FixtureVal ID="{by_id[zid].index}">0,{dmx(p)}</FixtureVal>\n'
            for zid, p in sorted(levels.items(), key=lambda kv: by_id[kv[0]].index)
        )
        attrs = f'ID="{fid}" Type="Scene" Name={quoteattr(name)}'
        if hidden:
            attrs += ' Hidden="True"'
        if path:
            attrs += f" Path={quoteattr(path)}"
        xml = (f" <Function {attrs}>\n"
               f'  <Speed FadeIn="{fade}" FadeOut="{fade}" Duration="0"/>\n{vals} </Function>\n')
        self.funcs.append(Func(fid, "Scene", name, xml, path))
        return fid

    def chaser(self, name: str, steps: list[tuple[int, int, int, int]], *, path: str,
               order: str = "Loop") -> int:
        """`steps` is (scene id, fade in ms, hold ms, fade out ms)."""
        fid = self.next_id()
        body = "".join(
            f'  <Step Number="{i}" FadeIn="{fi}" Hold="{h}" FadeOut="{fo}">{sid}</Step>\n'
            for i, (sid, fi, h, fo) in enumerate(steps)
        )
        xml = (f' <Function ID="{fid}" Type="Chaser" Name={quoteattr(name)} Path={quoteattr(path)}>\n'
               '  <Speed FadeIn="0" FadeOut="0" Duration="0"/>\n'
               "  <Direction>Forward</Direction>\n"
               f"  <RunOrder>{order}</RunOrder>\n"
               '  <SpeedModes FadeIn="PerStep" FadeOut="PerStep" Duration="PerStep"/>\n'
               f"{body} </Function>\n")
        self.funcs.append(Func(fid, "Chaser", name, xml, path))
        return fid


def build_functions(zones: list[Zone]) -> tuple[Show, dict[str, list[tuple[str, int]]]]:
    """Returns the Show plus the VC button groups: group title -> [(caption, function id)]."""
    show = Show(zones)
    # Zone ids are stable in the simulator inventory; look them up by name fragment so the
    # scenes keep working if ids are renumbered.
    def zid(fragment: str) -> str:
        hits = [z.id for z in zones if fragment in z.name]
        assert len(hits) == 1, (fragment, hits)
        return hits[0]

    def area(name_fragment: str) -> list[str]:
        return [z.id for z in zones if name_fragment in z.area]

    living, stairs, sitting, exterior = (area(a) for a in ("Living", "Stairs", "Sitting", "Exterior"))
    L = {k: zid(k) for k in ("East Recessed", "Bar Recessed", "Center Recessed", "Floor Lamps", "West Recessed", "Fan Light", "Porch Recessed")}
    S = {k: zid(k) for k in ("Stair Pendants", "Stair Accent", "Art Wall", "Stair Recessed")}

    groups: dict[str, list[tuple[str, int]]] = {"Living Room": [], "Stairs": [], "Sitting": [],
                                                "Exterior": [], "Whole house": []}

    # Mirrors the keypad buttons of the real system (see simulator_inventory.json).
    # Levels are in the order East, Bar, Center, Floor lamps, West, Fan light, Porch.
    def living_scene(caption: str, levels: list[float]) -> None:
        groups["Living Room"].append(
            (caption, show.scene(f"Living: {caption}", dict(zip(L.values(), levels)), path="Living Room")))

    living_scene("Entertain", [60, 80, 40, 100, 60, 0, 30])
    living_scene("Relax", [30, 40, 20, 70, 30, 25, 0])
    living_scene("Bright", [100] * 7)
    groups["Living Room"].append(("Porch", show.scene("Living: Porch", {L["Porch Recessed"]: 100}, path="Living Room")))

    def stairs_scene(caption: str, levels: list[float]) -> None:
        groups["Stairs"].append(
            (caption, show.scene(f"Stairs: {caption}", dict(zip(S.values(), levels)), path="Stairs")))

    stairs_scene("Stairs On", [100, 100, 100, 100])
    stairs_scene("Stairs Dim", [40, 40, 40, 40])
    stairs_scene("Accent", [0, 100, 60, 0])
    stairs_scene("Night", [10, 10, 10, 10])

    groups["Sitting"].append(("Reading", show.scene("Sitting: Reading", {sitting[0]: 70}, path="Sitting")))
    groups["Sitting"].append(("Sitting On", show.scene("Sitting: On", {sitting[0]: 100}, path="Sitting")))

    groups["Exterior"].append(
        ("Exterior On", show.scene("Exterior: On", {z: 100 for z in exterior}, path="Exterior")))
    groups["Exterior"].append(
        ("Step lights", show.scene("Exterior: Step lights only", {zid("All Step Lights"): 100}, path="Exterior")))

    groups["Whole house"].append(
        ("All Full (snap)", show.scene("House: All 100% (snap)", {z.id: 100 for z in zones}, path="House", fade=0)))
    groups["Whole house"].append(
        ("All 50%", show.scene("House: All 50%", {z.id: 50 for z in zones}, path="House")))

    # --- chasers (stress the app the way a real console would) ---
    walk = [show.scene(f"Walk {z.address:02d} {z.short}", {z.id: 100}, path="Walk steps", fade=0, hidden=True)
            for z in zones]
    ramp = show.scene("Ramp: all 100%", {z.id: 100 for z in zones}, path="Walk steps", fade=0, hidden=True)

    chases = [
        ("Zone Walk", show.chaser("Chase: Zone Walk (one zone at a time)",
                                  [(s, 400, 600, 400) for s in walk], path="Chases")),
        ("Evening Cycle", show.chaser(
            "Chase: Evening Cycle (slow living-room mood changes)",
            [(groups["Living Room"][1][1], 4000, 4000, 0),
             (groups["Living Room"][0][1], 4000, 4000, 0),
             (groups["Living Room"][2][1], 4000, 4000, 0)], path="Chases")),
        ("Ramp 10s", show.chaser("Chase: Whole house ramp 0-100-0 (10 s each way)",
                                 [(ramp, 10000, 2000, 10000)], path="Chases")),
        ("Random Flicker", show.chaser("Chase: Random flicker (rate-limit stress test)",
                                       [(s, 100, 100, 100) for s in walk], path="Chases", order="Random")),
    ]
    groups["Chases"] = chases
    return show, groups


# ---------------------------------------------------------------------------
# Virtual console
# ---------------------------------------------------------------------------
APPEARANCE = """<Appearance>
 <FrameStyle>{style}</FrameStyle>
 <ForegroundColor>Default</ForegroundColor>
 <BackgroundColor>Default</BackgroundColor>
 <BackgroundImage>None</BackgroundImage>
 <Font>Default</Font>
</Appearance>"""


def indent(text: str, n: int) -> str:
    pad = " " * n
    return "".join(pad + line if line.strip() else line for line in text.splitlines(keepends=True))


def window(x: int, y: int, w: int, h: int) -> str:
    return f'<WindowState Visible="True" X="{x}" Y="{y}" Width="{w}" Height="{h}"/>\n'


def frame(caption: str, x: int, y: int, w: int, h: int, children: str) -> str:
    return (f"<Frame Caption={quoteattr(caption)}>\n"
            + indent(APPEARANCE.format(style="Sunken"), 1) + "\n"
            + indent(window(x, y, w, h), 1)
            + " <AllowChildren>True</AllowChildren>\n <AllowResize>True</AllowResize>\n"
            " <ShowHeader>True</ShowHeader>\n <ShowEnableButton>False</ShowEnableButton>\n"
            " <Collapsed>False</Collapsed>\n <Disabled>False</Disabled>\n"
            + indent(children, 1) + "</Frame>\n")


def slider(caption: str, fixture: int, x: int, y: int, w: int, h: int) -> str:
    return (f"<Slider Caption={quoteattr(caption)} WidgetStyle=\"Slider\" InvertedAppearance=\"false\">\n"
            + indent(window(x, y, w, h), 1)
            + indent(APPEARANCE.format(style="None"), 1) + "\n"
            ' <SliderMode ValueDisplayStyle="Percentage" ClickAndGoType="None" Monitor="false">Level</SliderMode>\n'
            f' <Level LowLimit="0" HighLimit="255" Value="0">\n  <Channel Fixture="{fixture}">0</Channel>\n </Level>\n'
            "</Slider>\n")


def button(caption: str, function: int, x: int, y: int, w: int, h: int, action: str = "Toggle",
           fade_out: int = 0) -> str:
    action_attr = f' FadeOut="{fade_out}"' if action == "StopAll" and fade_out else ""
    return (f'<Button Caption={quoteattr(caption)} Icon="">\n'
            + indent(window(x, y, w, h), 1)
            + indent(APPEARANCE.format(style="None"), 1) + "\n"
            f' <Function ID="{function}"/>\n <Action{action_attr}>{action}</Action>\n'
            ' <Intensity Adjust="False">100</Intensity>\n'
            "</Button>\n")


def label(caption: str, x: int, y: int, w: int, h: int) -> str:
    return (f"<Label Caption={quoteattr(caption)}>\n"
            + indent(window(x, y, w, h), 1)
            + indent(APPEARANCE.format(style="None"), 1) + "\n</Label>\n")


def virtual_console(zones: list[Zone], groups: dict[str, list[tuple[str, int]]]) -> str:
    SW, SH, GAP, PAD = 92, 250, 4, 10  # slider width / height / gap / frame padding
    top = 20  # leaves room for a frame header
    children, x = "", 10
    areas: dict[str, list[Zone]] = {}
    for z in zones:
        areas.setdefault(z.area, []).append(z)
    for name, members in areas.items():
        inner = "".join(
            slider(f"{z.address:02d} {z.short}", z.index, PAD + i * (SW + GAP), top + PAD, SW, SH)
            for i, z in enumerate(members))
        w = PAD * 2 + len(members) * (SW + GAP) - GAP
        h = top + PAD * 2 + SH
        children += frame(name, x, 10, w, h, inner)
        x += w + 10
    total_w = x

    y = 10 + top + PAD * 2 + SH + 20
    BW, BH = 104, 40
    # Simple 3-column layout of button groups under the sliders.
    cols = [10, 10 + (PAD * 2 + 4 * (BW + GAP) - GAP) + 10, 10 + 2 * (PAD * 2 + 4 * (BW + GAP) - GAP) + 20]
    col_y = [y, y, y]
    order = ["Living Room", "Stairs", "Sitting", "Exterior", "Whole house", "Chases"]
    place = {"Living Room": 0, "Sitting": 0, "Stairs": 1, "Exterior": 1, "Whole house": 2, "Chases": 2}
    for title in order:
        items = groups[title]
        c = place[title]
        w = PAD * 2 + 4 * (BW + GAP) - GAP
        rows = (len(items) + 3) // 4
        inner = "".join(
            button(cap, fid, PAD + (i % 4) * (BW + GAP), top + PAD + (i // 4) * (BH + GAP), BW, BH)
            for i, (cap, fid) in enumerate(items))
        h = top + PAD * 2 + rows * (BH + GAP) - GAP
        children += frame(title, cols[c], col_y[c], w, h, inner)
        col_y[c] += h + 10

    # Global buttons: stop everything (fades running scenes/chases out) and blackout.
    gx = cols[2]
    inner = (button("Stop All (fade 1.5 s)", NO_FUNCTION, PAD, top + PAD, 2 * BW + GAP, BH, "StopAll", 1500)
             + button("Blackout", NO_FUNCTION, PAD + 2 * (BW + GAP), top + PAD, 2 * BW + GAP, BH, "Blackout"))
    children += frame("Master", gx, col_y[2], PAD * 2 + 4 * (BW + GAP) - GAP, top + PAD * 2 + BH, inner)

    width = max(total_w, cols[2] + PAD * 2 + 4 * (BW + GAP)) + 20
    height = max(col_y) + top + PAD * 2 + BH + 40
    return ("<VirtualConsole>\n"
            + indent(f'<Frame Caption="">\n'
                     + indent(APPEARANCE.format(style="Sunken"), 1) + "\n"
                     + indent(children, 1)
                     + "</Frame>\n", 1)
            + f' <Properties>\n  <Size Width="{width}" Height="{height}"/>\n'
              '  <GrandMaster ChannelMode="Intensity" ValueMode="Reduce" SliderMode="Normal"/>\n'
              " </Properties>\n</VirtualConsole>\n")


# ---------------------------------------------------------------------------
# Show for a real installation (built from a DbXmlInfo.xml export)
# ---------------------------------------------------------------------------
@dataclass
class SiteControls:
    """What the virtual console needs to know about a generated site show."""

    house: list[tuple[str, int]]  # (caption, function id)
    chases: list[tuple[str, int]]
    room_all_on: dict[str, int]  # area id -> scene id (only rooms with more than one output)
    switch_on: dict[str, int]  # zone id -> scene id that turns that switched output on


def group_by_room(zones: list[Zone]) -> dict[str, list[Zone]]:
    rooms: dict[str, list[Zone]] = {}
    for z in zones:
        rooms.setdefault(z.area_id, []).append(z)
    return rooms


def build_site_functions(zones: list[Zone]) -> tuple[Show, SiteControls]:
    show = Show(zones)
    rooms = group_by_room(zones)
    dimmers = [z for z in zones if z.kind != "switch"]
    ctl = SiteControls(house=[], chases=[], room_all_on={}, switch_on={})

    for area_id, members in rooms.items():
        name = members[0].area
        if len(members) > 1:
            ctl.room_all_on[area_id] = show.scene(f"{name}: All On", {z.id: 100 for z in members}, path=name)
        for z in members:
            if z.kind == "switch":
                ctl.switch_on[z.id] = show.scene(f"{name}: {z.short} on", {z.id: 100}, path=name)

    ctl.house.append(("All On", show.scene("House: All On", {z.id: 100 for z in zones}, path="House")))
    ctl.house.append(("Dimmers 50%", show.scene("House: Dimmers 50%", {z.id: 50 for z in dimmers}, path="House")))
    ctl.house.append(("Dimmers 25%", show.scene("House: Dimmers 25%", {z.id: 25 for z in dimmers}, path="House")))
    exterior = [z for z in zones if "exterior" in z.area.lower()]
    if exterior:
        ctl.house.append(("Exterior On", show.scene("House: Exterior On", {z.id: 100 for z in exterior},
                                                    path="House")))

    # Test chases drive dimmers only, so ventilation fans and other switched loads are not buzzed.
    walk = [show.scene(f"Walk {z.address:03d} {z.short}", {z.id: 100}, path="Walk steps", fade=0, hidden=True)
            for z in dimmers]
    ramp = show.scene("Ramp: all dimmers 100%", {z.id: 100 for z in dimmers}, path="Walk steps", fade=0, hidden=True)
    ctl.chases.append(("Zone Walk", show.chaser("Chase: Zone Walk (one dimmer at a time)",
                                                [(s, 400, 600, 400) for s in walk], path="Chases")))
    ctl.chases.append(("Ramp 10s", show.chaser("Chase: Whole house ramp 0-100-0 (10 s each way)",
                                               [(ramp, 10000, 2000, 10000)], path="Chases")))
    return show, ctl


def site_virtual_console(zones: list[Zone], ctl: SiteControls) -> str:
    SW, SH, BW, BH, GAP, PAD, TOP = 92, 220, 92, 40, 4, 10, 20  # TOP leaves room for a frame header
    MAX_W = 1700  # frames wrap to a new row beyond this

    def button_frame(title: str, items: list[tuple[str, int]], cols: int):
        rows = (len(items) + cols - 1) // cols
        w, h = PAD * 2 + cols * (BW + GAP) - GAP, TOP + PAD * 2 + rows * (BH + GAP) - GAP

        def make(x: int, y: int) -> str:
            inner = "".join(
                button(cap, fid, PAD + (i % cols) * (BW + GAP), TOP + PAD + (i // cols) * (BH + GAP), BW, BH)
                for i, (cap, fid) in enumerate(items))
            return frame(title, x, y, w, h, inner)
        return w, h, make

    def master_frame():
        w, h = PAD * 2 + 2 * (BW + GAP) - GAP, TOP + PAD * 2 + BH

        def make(x: int, y: int) -> str:
            inner = (button("Stop All (fade 1.5 s)", NO_FUNCTION, PAD, TOP + PAD, BW, BH, "StopAll", 1500)
                     + button("Blackout", NO_FUNCTION, PAD + BW + GAP, TOP + PAD, BW, BH, "Blackout"))
            return frame("Master", x, y, w, h, inner)
        return w, h, make

    def room_frame(area_id: str, members: list[Zone]):
        sliders = [z for z in members if z.kind != "switch"]
        buttons: list[tuple[str, int]] = []
        if area_id in ctl.room_all_on:
            buttons.append(("All On", ctl.room_all_on[area_id]))
        buttons += [(f"{z.address:02d} {z.short}", ctl.switch_on[z.id]) for z in members if z.kind == "switch"]
        cols = max(len(sliders), min(len(buttons), 4), 1)
        brows = (len(buttons) + cols - 1) // cols
        w = PAD * 2 + cols * (SW + GAP) - GAP
        h = TOP + PAD * 2 + (SH if sliders else 0) + brows * (BH + GAP) - (GAP if brows else 0)

        def make(x: int, y: int) -> str:
            inner = "".join(
                slider(f"{z.address:02d} {z.short}", z.index, PAD + i * (SW + GAP), TOP + PAD, SW, SH)
                for i, z in enumerate(sliders))
            by = TOP + PAD + (SH + GAP if sliders else 0)
            inner += "".join(
                button(cap, fid, PAD + (i % cols) * (SW + GAP), by + (i // cols) * (BH + GAP), BW, BH)
                for i, (cap, fid) in enumerate(buttons))
            return frame(members[0].area, x, y, w, h, inner)
        return w, h, make

    controls = [button_frame("House", ctl.house, 4), button_frame("Test chases", ctl.chases, 2), master_frame()]
    rooms = [room_frame(a, m) for a, m in group_by_room(zones).items()]

    children, x, y, row_h, max_x = "", 10, 10, 0, 0
    # The control frames get a row of their own; the rooms then flow, wrapping at MAX_W.
    for frames in (controls, rooms):
        if row_h:
            x, y, row_h = 10, y + row_h + 10, 0
        for w, h, make in frames:
            if x > 10 and x + w > MAX_W:
                x, y, row_h = 10, y + row_h + 10, 0
            children += make(x, y)
            x += w + 10
            row_h = max(row_h, h)
            max_x = max(max_x, x)
    width, height = max_x + 10, y + row_h + 20
    return ("<VirtualConsole>\n"
            + indent('<Frame Caption="">\n'
                     + indent(APPEARANCE.format(style="Sunken"), 1) + "\n"
                     + indent(children, 1)
                     + "</Frame>\n", 1)
            + f' <Properties>\n  <Size Width="{width}" Height="{height}"/>\n'
              '  <GrandMaster ChannelMode="Intensity" ValueMode="Reduce" SliderMode="Normal"/>\n'
              " </Properties>\n</VirtualConsole>\n")


def build_patch_list(zones: list[Zone]) -> str:
    """CSV of the patch, to check the app's DMX tab against."""
    rows = ["dmx_address,lutron_integration_id,room,output,type"]
    for z in zones:
        rows.append(",".join(f'"{v}"' if "," in v or '"' in v else v for v in
                             (str(z.address), z.id, z.area, z.name, z.kind)))
    return "\n".join(rows) + "\n"



# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
def build_workspace(zones: list[Zone], *, interface: str, target: str | None, autostart: int | None = None,
                    site: bool = False) -> str:
    """`site` builds the generic show for a real installation instead of the simulator test show."""
    if site:
        show, ctl = build_site_functions(zones)
    else:
        show, groups = build_functions(zones)
    # Without parameters QLC+ sends to the interface's broadcast address (127.0.0.1 for loopback).
    params = f'<PluginParameters outputIP="{target}"/>' if target else ""
    out = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE Workspace>\n"
           '<Workspace xmlns="http://www.qlcplus.org/Workspace" CurrentWindow="VirtualConsole">\n'
           " <Creator>\n  <Name>Q Light Controller Plus</Name>\n"
           f"  <Version>{QLC_VERSION}</Version>\n  <Author>lutron-dmx</Author>\n </Creator>\n"]
    autostart_attr = f' Autostart="{autostart}"' if autostart is not None else ""
    out.append(f" <Engine{autostart_attr}>\n")
    out.append("  <InputOutputMap>\n"
               '   <Universe Name="Lutron DMX (Art-Net universe 0)" ID="0">\n'
               f'    <Output Plugin="ArtNet" UID="{interface}" Line="0">{params}</Output>\n'
               "   </Universe>\n  </InputOutputMap>\n")
    for z in zones:
        out.append(
            "  <Fixture>\n   <Manufacturer>Generic</Manufacturer>\n   <Model>Generic</Model>\n"
            f"   <Mode>Generic</Mode>\n   <ID>{z.index}</ID>\n   <Name>{escape(z.name)}</Name>\n"
            f"   <Universe>0</Universe>\n   <Address>{z.address - 1}</Address>\n   <Channels>1</Channels>\n"
            "  </Fixture>\n")
    for f in show.funcs:
        out.append(indent(f.xml, 1))
    out.append(" </Engine>\n")
    out.append(indent(site_virtual_console(zones, ctl) if site else virtual_console(zones, groups), 1))
    out.append(" <SimpleDesk>\n  <Engine/>\n </SimpleDesk>\n</Workspace>\n")
    return "".join(out)


def build_app_config(zones: list[Zone], *, loopback: bool) -> dict:
    from lutron_dmx.config import AppConfig, DmxConfig, DmxFixtureMapping

    cfg = AppConfig(dmx=DmxConfig(
        enabled=True, source="artnet", universe=0,
        # Pinning the loopback address keeps QLC+ (which also owns UDP 6454) from stealing the packets.
        bind_ip="127.0.0.1" if loopback else "0.0.0.0",
        mappings=[DmxFixtureMapping(zone_id=z.id, address=z.address, params=["intensity"]) for z in zones]))
    return cfg.to_dict()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interface", default="127.0.0.1",
                    help="IP of the QLC+ computer's network interface used for Art-Net output (default: loopback)")
    ap.add_argument("--target", help="send unicast to this IP (the computer running lutron-dmx) instead of broadcast")
    ap.add_argument("--db-xml", type=Path,
                    help="build the show from this RadioRA 2 DbXmlInfo.xml export instead of the simulator inventory")
    ap.add_argument("--room", help="with --db-xml: only this room (e.g. Kitchen), patched from DMX 1")
    ap.add_argument("--out-dir", type=Path, help="where to write the files (see above for the default)")
    ap.add_argument("--autostart", type=int, help="function id to start when the workspace opens (for tests)")
    args = ap.parse_args()
    loopback = args.interface == "127.0.0.1" and not args.target
    out_dir = args.out_dir or (HERE if loopback and not args.db_xml else HERE / "custom")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.db_xml:
        zones = load_zones_from_db_xml(args.db_xml)
        suffix = ""
        if args.room:
            zones = restrict_to_room(zones, args.room)
            suffix = "-" + re.sub(r"[^A-Za-z0-9]+", "-", zones[0].area.split(" ", 1)[-1]).strip("-")
        qxw = out_dir / f"Lutron-DMX-Show{suffix}.qxw"
        qxw.write_text(
            build_workspace(zones, interface=args.interface, target=args.target, autostart=args.autostart,
                            site=True),
            encoding="utf-8", newline="\n")
        print(f"wrote {qxw}")
        patch = out_dir / f"patch-list{suffix}.csv"
        patch.write_text(build_patch_list(zones), encoding="utf-8", newline="\n")
        print(f"wrote {patch}")
        if args.room:
            print(f"{len(zones)} outputs on DMX 1-{len(zones)}. In the app's DMX tab map only these zones "
                  "(see the patch list): every other mapped zone would be sent 0.")
        else:
            print(f"{len(zones)} outputs on DMX 1-{len(zones)}. In the app's DMX tab use Re-assign all "
                  "(params: intensity, start 1) to get the same patch.")
        return
    zones = load_zones()
    qxw = out_dir / "Lutron-DMX-Test.qxw"
    qxw.write_text(
        build_workspace(zones, interface=args.interface, target=args.target, autostart=args.autostart),
        encoding="utf-8", newline="\n")
    print(f"wrote {qxw}")
    cfg_path = out_dir / "app-home" / "config.json"
    cfg_path.parent.mkdir(exist_ok=True)
    cfg_path.write_text(json.dumps(build_app_config(zones, loopback=loopback), indent=2) + "\n",
                        encoding="utf-8", newline="\n")
    print(f"wrote {cfg_path}")


if __name__ == "__main__":
    main()
