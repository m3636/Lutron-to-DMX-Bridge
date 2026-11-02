"""The QLC+ test workspace must stay in step with the simulator inventory and the app's patch."""

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lutron_dmx.config import AppConfig
from lutron_dmx.dmx.artnet import broadcast_bind_host
from lutron_dmx.dmx.engine import auto_assign, validate_mappings

QLC_DIR = Path(__file__).resolve().parent.parent / "qlcplus"
NS = "{http://www.qlcplus.org/Workspace}"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("generate_show", QLC_DIR / "generate_show.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses looks the module up by name
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def workspace():
    return ET.parse(QLC_DIR / "Lutron-DMX-Test.qxw").getroot()


def test_committed_files_are_current(gen):
    """Regenerating gives the committed files, so nobody edits one without the generator."""
    zones = gen.load_zones()
    assert gen.build_workspace(zones, interface="127.0.0.1", target=None) == \
        (QLC_DIR / "Lutron-DMX-Test.qxw").read_text(encoding="utf-8")
    assert json.loads((QLC_DIR / "app-home" / "config.json").read_text(encoding="utf-8")) == \
        gen.build_app_config(zones, loopback=True)


def test_patch_matches_the_apps_auto_assign(gen):
    zones = gen.load_zones()
    cfg = AppConfig.from_dict(json.loads((QLC_DIR / "app-home" / "config.json").read_text(encoding="utf-8")))
    assert cfg.dmx.enabled and cfg.dmx.source == "artnet" and cfg.dmx.universe == 0
    assert cfg.dmx.mappings == auto_assign([z.id for z in zones])
    assert validate_mappings(cfg.dmx.mappings) == []


def test_fixtures_are_patched_where_the_app_listens(gen, workspace):
    zones = gen.load_zones()
    fixtures = workspace.findall(f"{NS}Engine/{NS}Fixture")
    assert len(fixtures) == len(zones) == 14
    for fx, z in zip(fixtures, zones):
        assert int(fx.findtext(f"{NS}ID")) == z.index
        assert fx.findtext(f"{NS}Universe") == "0"
        # QLC+ addresses are 0-based, the app's are 1-based
        assert int(fx.findtext(f"{NS}Address")) + 1 == z.address
        assert fx.findtext(f"{NS}Channels") == "1"


def test_output_is_artnet_on_loopback(workspace):
    out = workspace.find(f"{NS}Engine/{NS}InputOutputMap/{NS}Universe/{NS}Output")
    assert out.get("Plugin") == "ArtNet" and out.get("UID") == "127.0.0.1"


def test_every_reference_resolves(workspace):
    fixture_ids = {fx.findtext(f"{NS}ID") for fx in workspace.findall(f"{NS}Engine/{NS}Fixture")}
    funcs = {f.get("ID"): f for f in workspace.findall(f"{NS}Engine/{NS}Function")}
    assert len(funcs) == len(workspace.findall(f"{NS}Engine/{NS}Function"))  # unique ids
    for f in funcs.values():
        if f.get("Type") == "Scene":
            for fv in f.findall(f"{NS}FixtureVal"):
                assert fv.get("ID") in fixture_ids
                nums = [int(n) for n in fv.text.split(",")]
                assert nums[0::2] == [0] and all(0 <= v <= 255 for v in nums[1::2])
        else:
            assert f.get("Type") == "Chaser"
            for step in f.findall(f"{NS}Step"):
                assert funcs[step.text].get("Type") == "Scene"
    vc = workspace.find(f"{NS}VirtualConsole")
    for btn in vc.iter(f"{NS}Button"):
        fid = btn.find(f"{NS}Function").get("ID")
        assert fid == "4294967295" or fid in funcs
    sliders = list(vc.iter(f"{NS}Slider"))
    assert sorted(int(s.find(f"{NS}Level/{NS}Channel").get("Fixture")) for s in sliders) == list(range(14))


def test_every_zone_has_a_slider_captioned_with_its_address(gen, workspace):
    zones = {z.index: z for z in gen.load_zones()}
    for s in workspace.iter(f"{NS}Slider"):
        z = zones[int(s.find(f"{NS}Level/{NS}Channel").get("Fixture"))]
        assert re.match(rf"{z.address:02d} ", s.get("Caption"))


def test_loopback_bind_is_honoured_on_every_platform(monkeypatch):
    for plat in ("linux", "darwin", "win32"):
        monkeypatch.setattr("sys.platform", plat)
        assert broadcast_bind_host("127.0.0.1") == "127.0.0.1"
        assert broadcast_bind_host("0.0.0.0") == ""
    monkeypatch.setattr("sys.platform", "linux")
    assert broadcast_bind_host("192.168.1.5") == ""


# --- show built from a RadioRA 2 DbXmlInfo.xml export ---------------------------------------------
SAMPLE_DB_XML = Path(__file__).resolve().parent.parent / "lutron_dmx" / "drivers" / "sample_data" / "ra2_sample_dbxml.xml"


@pytest.fixture(scope="module")
def site_zones(gen):
    return gen.load_zones_from_db_xml(SAMPLE_DB_XML)


@pytest.fixture(scope="module")
def site_workspace(gen, site_zones):
    return ET.fromstring(gen.build_workspace(site_zones, interface="127.0.0.1", target=None, site=True))


def test_site_patch_is_what_the_apps_reassign_all_gives(site_zones):
    from lutron_dmx.drivers.ra2 import parse_db_xml

    inv = parse_db_xml(SAMPLE_DB_XML.read_bytes())
    # The GUI patches areas in inventory order, zones in inventory order within each area.
    gui_order = [z.id for a in inv.areas for z in inv.zones if z.area_id == a.id]
    assert [z.id for z in site_zones] == gui_order
    mappings = auto_assign(gui_order)
    assert [(z.id, z.address) for z in site_zones] == [(m.zone_id, m.address) for m in mappings]
    assert {z.id: z.kind for z in site_zones}["21"] == "switch"  # NON_DIM


def test_site_fixtures_follow_the_patch(site_zones, site_workspace):
    fixtures = site_workspace.findall(f"{NS}Engine/{NS}Fixture")
    assert len(fixtures) == len(site_zones)
    for fx, z in zip(fixtures, site_zones):
        assert int(fx.findtext(f"{NS}ID")) == z.index
        assert int(fx.findtext(f"{NS}Address")) + 1 == z.address
        assert fx.findtext(f"{NS}Channels") == "1"


def test_site_console_has_a_fader_per_dimmer_and_a_button_per_switch(site_zones, site_workspace):
    vc = site_workspace.find(f"{NS}VirtualConsole")
    funcs = {f.get("ID"): f for f in site_workspace.findall(f"{NS}Engine/{NS}Function")}
    assert len(funcs) == len(site_workspace.findall(f"{NS}Engine/{NS}Function"))
    sliders = {int(s.find(f"{NS}Level/{NS}Channel").get("Fixture")): s.get("Caption") for s in vc.iter(f"{NS}Slider")}
    dimmers = {z.index for z in site_zones if z.kind != "switch"}
    assert set(sliders) == dimmers
    by_index = {z.index: z for z in site_zones}
    for idx, caption in sliders.items():
        assert caption.startswith(f"{by_index[idx].address:02d} ")
    # a switched output gets a button that turns on exactly that fixture
    for z in (z for z in site_zones if z.kind == "switch"):
        scenes = [f for f in funcs.values() if f.get("Type") == "Scene"
                  and [v.get("ID") for v in f.findall(f"{NS}FixtureVal")] == [str(z.index)]
                  and f.find(f"{NS}FixtureVal").text == "0,255" and f.get("Hidden") != "True"]
        assert len(scenes) == 1
        assert any(b.find(f"{NS}Function").get("ID") == scenes[0].get("ID") for b in vc.iter(f"{NS}Button"))
    for b in vc.iter(f"{NS}Button"):
        fid = b.find(f"{NS}Function").get("ID")
        assert fid == "4294967295" or fid in funcs
    for f in funcs.values():
        if f.get("Type") == "Chaser":
            assert all(funcs[st.text].get("Type") == "Scene" for st in f.findall(f"{NS}Step"))


def test_site_room_scene_covers_the_whole_room_and_chases_skip_switches(site_zones, site_workspace):
    funcs = {f.get("Name"): f for f in site_workspace.findall(f"{NS}Engine/{NS}Function")}
    stairs = [z.index for z in site_zones if "Stairs" in z.area]
    vals = funcs["Main Stairs: All On"].findall(f"{NS}FixtureVal")
    assert [int(v.get("ID")) for v in vals] == stairs and all(v.text == "0,255" for v in vals)
    switch_ids = {str(z.index) for z in site_zones if z.kind == "switch"}
    for name in ("House: Dimmers 50%", "Ramp: all dimmers 100%"):
        assert not switch_ids & {v.get("ID") for v in funcs[name].findall(f"{NS}FixtureVal")}
    assert not any(n.startswith("Walk") and {v.get("ID") for v in f.findall(f"{NS}FixtureVal")} & switch_ids
                   for n, f in funcs.items())


def test_db_xml_run_leaves_the_committed_files_alone(gen, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generate_show.py", "--db-xml", str(SAMPLE_DB_XML), "--out-dir", str(tmp_path)])
    gen.main()
    ET.parse(tmp_path / "Lutron-DMX-Show.qxw")
    rows = (tmp_path / "patch-list.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("dmx_address,") and rows[1].startswith("1,20,Exterior,")
    assert not (tmp_path / "app-home").exists()


def test_single_room_show_is_patched_from_one(gen, site_zones):
    zones = gen.restrict_to_room(site_zones, "stairs")
    assert [z.address for z in zones] == [1, 2, 3, 4] and [z.index for z in zones] == [0, 1, 2, 3]
    assert [z.id for z in zones] == [z.id for z in site_zones if "Stairs" in z.area]
    ws = ET.fromstring(gen.build_workspace(zones, interface="127.0.0.1", target=None, site=True))
    assert len(ws.findall(f"{NS}Engine/{NS}Fixture")) == 4
    assert gen.restrict_to_room(site_zones, "Main Stairs") == zones


def test_room_name_must_be_unambiguous(gen, site_zones):
    for bad in ("nothing", "o"):  # no match / several rooms contain it
        with pytest.raises(SystemExit):
            gen.restrict_to_room(site_zones, bad)
