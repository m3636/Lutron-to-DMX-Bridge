"""Export / import of the app's settings: format, credentials opt-in, validation, versions, backup, API and CLI."""

from __future__ import annotations

import base64
import copy
import json
import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from lutron_to_dmx_bridge import __version__, config_transfer as ct
from lutron_to_dmx_bridge.__main__ import main as cli_main
from lutron_to_dmx_bridge.config import (ActiveScheduleEntry, ActiveScheduleSet, AppConfig, DmxConfig,
                                         DmxFixtureMapping, DriverConfig, SceneConfig, ScheduleRef,
                                         ScheduleSetConfig)
from test_web import FakeDriver

CERTS = {"caseta.key": b"KEY-BYTES", "caseta.crt": b"CERT-BYTES", "caseta-bridge.crt": b"CA-BYTES"}
SECRET = "hunter2-secret"
TOKEN = "ghp_secret_token"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def make_config(tmp_path, kind="ra2") -> AppConfig:
    certdir = tmp_path / "certs"
    certdir.mkdir(exist_ok=True)
    for name, data in CERTS.items():
        (certdir / name).write_bytes(data)
    cfg = AppConfig(
        driver=DriverConfig(kind=kind, host="10.0.0.5", hostname="lutron-abc", username="installer",
                            password=SECRET, port=23, integration_report="/x/DbXmlInfo.xml",
                            leap_cert_dir=str(certdir)),
        scenes=[SceneConfig("s1", "Movie", {"z1": 10.0, "z2": 0.0}, 2.0), SceneConfig("s2", "Party", {"z1": 100.0})],
        dmx=DmxConfig(enabled=True, source="sacn", universe=3, bind_ip="192.168.1.9", serial_port="COM3",
                      max_rate_hz=8.0, max_commands_per_s=30.0, resolution_pct=2.0,
                      mappings=[DmxFixtureMapping("z1", 1, ["intensity", "on_off"]),
                                DmxFixtureMapping("z2", 10, ["intensity"], enabled=False)]),
        schedule_sets=[ScheduleSetConfig("p1", "Performance mode", [ScheduleRef("s1", "Sunset")], [])],
        active_schedule_set=ActiveScheduleSet("p1", "Performance mode", "2026-11-07T01:00:00+00:00",
                                              [ActiveScheduleEntry("s1", "Sunset", True, False)]),
        web_host="127.0.0.1", web_port=8123)
    cfg.updates.check = False
    cfg.updates.token = TOKEN
    cfg.updates.dismissed_version = "9.9.9"
    return cfg


def as_dict(cfg: AppConfig) -> dict:
    d = cfg.to_dict()
    d["updates"].pop("dismissed_version")
    return d


# ---------------------------------------------------------------- export
def test_export_leaves_credentials_out_by_default(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path))
    assert doc["format"] == ct.FORMAT and doc["schema_version"] == ct.SCHEMA_VERSION
    assert doc["app_version"] == __version__ and doc["includes_credentials"] is False
    text = json.dumps(doc)
    assert SECRET not in text and TOKEN not in text and "installer" not in text
    assert "files" not in doc and "dismissed_version" not in doc["config"]["updates"]
    # the rest of the settings is all there
    assert doc["config"]["dmx"]["mappings"][0] == {"zone_id": "z1", "address": 1,
                                                    "params": ["intensity", "on_off"], "enabled": True}
    assert [s["name"] for s in doc["config"]["scenes"]] == ["Movie", "Party"]
    assert doc["config"]["schedule_sets"][0]["name"] == "Performance mode"
    assert doc["config"]["driver"]["host"] == "10.0.0.5"


def test_export_with_credentials_includes_login_token_and_certificates(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path), include_credentials=True)
    assert doc["includes_credentials"] is True
    assert doc["config"]["driver"]["password"] == SECRET and doc["config"]["updates"]["token"] == TOKEN
    certs = doc["files"]["leap_certs"]
    assert {n: base64.b64decode(v) for n, v in certs.items()} == CERTS


def test_export_does_not_modify_the_live_config(tmp_path, home):
    cfg = make_config(tmp_path)
    before = cfg.to_dict()
    ct.build_export(cfg)
    assert cfg.to_dict() == before


def test_missing_cert_folder_is_not_an_error(tmp_path, home):
    cfg = make_config(tmp_path)
    cfg.driver.leap_cert_dir = str(tmp_path / "gone")
    assert "files" not in ct.build_export(cfg, include_credentials=True)


# ---------------------------------------------------------------- round trip
def test_round_trip_with_credentials_rebuilds_everything(tmp_path, home):
    original = make_config(tmp_path)
    doc = json.loads(json.dumps(ct.build_export(original, include_credentials=True)))
    fresh = AppConfig()
    parsed = ct.parse_document(doc)
    assert parsed.sections == ["driver", "dmx", "scenes", "schedule_sets", "updates", "web"]
    res = ct.import_into_config(fresh, parsed, list(ct.SECTIONS), restore_active_set=True)
    assert res.driver_changed and res.dmx_changed and res.active_set_restored
    got, want = as_dict(fresh), as_dict(original)
    restored = home / "leap" / "restored"
    want["driver"]["leap_cert_dir"] = str(restored)  # certificates come back into the app's own folder
    assert got == want
    assert {p.name: p.read_bytes() for p in restored.iterdir()} == CERTS
    assert AppConfig.load().to_dict() == fresh.to_dict()  # and it was saved
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(restored / "caseta.key").st_mode) == 0o600


def test_import_without_credentials_keeps_the_current_login_token_and_certificates(tmp_path, home):
    exported = ct.build_export(make_config(tmp_path))
    current = AppConfig(driver=DriverConfig(kind="ra2", host="10.9.9.9", username="mine", password="mine-pw",
                                            leap_cert_dir="/my/certs"))
    current.updates.token = "my-token"
    res = ct.import_into_config(current, ct.parse_document(exported))
    assert current.driver.host == "10.0.0.5" and current.driver.port == 23
    assert (current.driver.username, current.driver.password) == ("mine", "mine-pw")
    assert current.driver.leap_cert_dir == "/my/certs"
    assert current.updates.token == "my-token" and current.updates.check is False
    assert res.applied == ["driver", "dmx", "scenes", "schedule_sets", "updates"]  # the web address is opt-in
    assert current.web_port == 8080


def test_hand_edited_file_with_password_but_no_credentials_flag_is_not_trusted(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path), include_credentials=True)
    doc["includes_credentials"] = False
    parsed = ct.parse_document(doc)
    assert not parsed.login_included and not parsed.token_included and not parsed.cert_files


# ---------------------------------------------------------------- sections, replacement, backup
def test_only_selected_sections_change_and_whole_sections_are_replaced(tmp_path, home):
    current = make_config(tmp_path)
    current.scenes = [SceneConfig("old", "Old scene", {"z9": 1.0})]
    current.dmx = DmxConfig()
    exported = AppConfig(scenes=[SceneConfig("n1", "New", {"z1": 5.0})])
    parsed = ct.parse_document(ct.build_export(exported))
    res = ct.import_into_config(current, parsed, ["scenes"])
    assert res.applied == ["scenes"] and not res.driver_changed and not res.dmx_changed
    assert [s.name for s in current.scenes] == ["New"]  # replaced, not merged
    assert current.dmx == DmxConfig() and current.driver.host == "10.0.0.5"


def test_selecting_a_section_the_file_lacks_or_nothing_is_refused(tmp_path, home):
    doc = ct.build_export(AppConfig())
    doc["config"].pop("scenes")
    parsed = ct.parse_document(doc)
    with pytest.raises(ct.ConfigImportError, match="no Custom scenes"):
        ct.select_sections(parsed, ["scenes"])
    with pytest.raises(ct.ConfigImportError, match="Unknown section"):
        ct.select_sections(parsed, ["nope"])
    with pytest.raises(ct.ConfigImportError, match="at least one"):
        ct.select_sections(parsed, [])


def test_current_settings_are_backed_up_first_and_the_backup_undoes_the_import(tmp_path, home):
    current = make_config(tmp_path)
    before = current.to_dict()
    parsed = ct.parse_document(ct.build_export(AppConfig(scenes=[SceneConfig("n", "Other", {})])))
    res = ct.import_into_config(current, parsed, ["scenes"])
    backup = home / "backups"
    files = list(backup.glob("config-before-import-*.json"))
    assert [str(f) for f in files] == [res.backup]
    text = files[0].read_text()
    assert SECRET in text  # a restorable copy, so it keeps the login (private permissions below)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    # importing the backup puts everything back
    ct.import_into_config(current, ct.parse_document(json.loads(text)), ["scenes", "driver", "dmx"])
    assert [s.name for s in current.scenes] == ["Movie", "Party"]
    assert current.driver.password == SECRET and as_dict(current)["dmx"] == before["dmx"]


def test_backups_in_the_same_second_do_not_overwrite_each_other(tmp_path, home):
    cfg = make_config(tmp_path)
    paths = {ct.write_pre_import_backup(cfg) for _ in range(3)}
    assert len(paths) == 3 and all(p.exists() for p in paths)


def test_a_rejected_file_changes_nothing_and_makes_no_backup(tmp_path, home):
    current = make_config(tmp_path)
    before = current.to_dict()
    doc = ct.build_export(AppConfig())
    doc["config"]["dmx"]["universe"] = 99999
    with pytest.raises(ct.ConfigImportError):
        ct.parse_document(doc)
    assert current.to_dict() == before and not (home / "backups").exists()


# ---------------------------------------------------------------- versions and formats
def test_file_from_a_newer_schema_is_refused_with_advice():
    doc = ct.build_export(AppConfig())
    doc["schema_version"] = ct.SCHEMA_VERSION + 1
    doc["app_version"] = "9.0.0"
    with pytest.raises(ct.ConfigImportError, match=r"newer version.*9\.0\.0.*Update the app"):
        ct.parse_document(doc)


def test_older_schema_is_upgraded_through_the_migrations(monkeypatch):
    doc = ct.build_export(AppConfig(scenes=[SceneConfig("a", "A", {"z": 1.0})]))

    def v1_to_v2(d: dict) -> dict:  # pretend v2 renamed "scenes" to "custom_scenes"
        d = copy.deepcopy(d)
        d["schema_version"] = 2
        d["config"]["scenes"] = d["config"].pop("custom_scenes", d["config"].get("scenes"))
        return d

    old = copy.deepcopy(doc)
    old["config"]["custom_scenes"] = old["config"].pop("scenes")
    monkeypatch.setattr(ct, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(ct, "MIGRATIONS", {1: v1_to_v2})
    parsed = ct.parse_document(old)
    assert [s.name for s in parsed.config.scenes] == ["A"] and parsed.schema_version == 1


def test_older_schema_without_a_migration_is_refused(monkeypatch):
    doc = ct.build_export(AppConfig())
    monkeypatch.setattr(ct, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(ct, "MIGRATIONS", {})
    with pytest.raises(ct.ConfigImportError, match="cannot upgrade"):
        ct.parse_document(doc)


@pytest.mark.parametrize("bad", [None, 0, -1, "1", True, 1.5])
def test_schema_version_must_be_a_positive_integer(bad):
    doc = ct.build_export(AppConfig())
    doc["schema_version"] = bad
    with pytest.raises(ct.ConfigImportError, match="schema_version"):
        ct.parse_document(doc)


@pytest.mark.parametrize("raw", [[], "x", 5, None, {}, {"format": "something-else"}, {"driver": {}}])
def test_other_files_are_not_mistaken_for_an_export(raw):
    with pytest.raises(ct.ConfigImportError):
        ct.parse_document(raw)


def test_newer_app_version_imports_with_a_warning_and_unknown_settings_are_ignored():
    doc = ct.build_export(AppConfig(scenes=[SceneConfig("a", "A", {})]))
    doc["app_version"] = "99.0.0"
    doc["config"]["future_thing"] = {"x": 1}
    doc["config"]["dmx"]["future_flag"] = True
    parsed = ct.parse_document(doc)
    text = " ".join(parsed.warnings)
    assert "99.0.0" in text and "future_thing" in text and "future_flag" in text
    assert parsed.info()["newer_app"] is True


def test_app_backup_file_is_accepted_but_its_masked_password_never_applied(tmp_path, home):
    cfg = make_config(tmp_path)
    legacy = {"format": ct.BACKUP_FORMAT, "version": 1, "created": "2026-01-01T00:00:00",
              "lutron": {"inventory": {}}, "app_config": cfg.to_dict()}
    legacy["app_config"]["driver"]["password"] = "********"
    parsed = ct.parse_document(legacy)
    assert not parsed.login_included and not parsed.token_included
    assert any("system backup" in w for w in parsed.warnings)
    current = AppConfig(driver=DriverConfig(kind="ra2", host="1.1.1.1", username="u", password="keep"))
    ct.import_into_config(current, parsed)
    assert current.driver.password == "keep" and current.driver.host == "10.0.0.5"
    assert current.updates.token == ""  # the token that old backups held in the clear is not trusted either


# ---------------------------------------------------------------- validation
def broken(mutate) -> dict:
    doc = ct.build_export(make_config_for_validation())
    mutate(doc["config"])
    return doc


def make_config_for_validation() -> AppConfig:
    return AppConfig(
        driver=DriverConfig(kind="ra2", host="10.0.0.5"),
        scenes=[SceneConfig("s1", "A", {"z1": 1.0})],
        dmx=DmxConfig(mappings=[DmxFixtureMapping("z1", 1, ["intensity"])]),
        schedule_sets=[ScheduleSetConfig("p", "P", [ScheduleRef("s1", "S")], [])])


@pytest.mark.parametrize("mutate,expect", [
    (lambda c: c["driver"].update(kind="zigbee"), "driver.kind"),
    (lambda c: c["driver"].update(host=""), "host / IP"),
    (lambda c: c["driver"].update(port=70000), "driver.port"),
    (lambda c: c["driver"].update(port="23"), "driver.port"),
    (lambda c: c["dmx"].update(source="midi"), "dmx.source"),
    (lambda c: c["dmx"].update(universe=-1), "dmx.universe"),
    (lambda c: c["dmx"].update(max_rate_hz=0), "dmx.max_rate_hz"),
    (lambda c: c["dmx"].update(enabled="yes"), "dmx.enabled"),
    (lambda c: c["dmx"]["mappings"][0].update(address=513), "dmx.mappings.0.address"),
    (lambda c: c["dmx"]["mappings"][0].update(address=512, params=["intensity", "on_off"]), "past channel 512"),
    (lambda c: c["dmx"]["mappings"][0].update(params=["colour"]), "unsupported DMX parameter"),
    (lambda c: c["dmx"]["mappings"][0].update(params=[]), "dmx.mappings.0.params"),
    (lambda c: c["dmx"]["mappings"][0].update(zone_id=""), "dmx.mappings.0.zone_id"),
    (lambda c: c["dmx"]["mappings"][0].update(address=True), "dmx.mappings.0.address"),
    (lambda c: c["scenes"][0]["levels"].update(z1=101), "levels must be 0-100"),
    (lambda c: c["scenes"][0].update(name=""), "scenes.0.name"),
    (lambda c: c["scenes"].append(dict(c["scenes"][0])), "share the same id"),
    (lambda c: c.update(scenes={"a": 1}), "scenes"),
    (lambda c: c["schedule_sets"].append(dict(c["schedule_sets"][0])), "share the same id"),
    (lambda c: c["schedule_sets"].append({**c["schedule_sets"][0], "id": "q", "name": "p"}), "share the same name"),
    (lambda c: c["schedule_sets"][0].update(enable=[{"id": "s1", "name": "S"}]), "both disables and enables"),
    (lambda c: c.update(web_port=0, web_host="127.0.0.1"), "web_port"),
    (lambda c: c.update(web_port="80", web_host="127.0.0.1"), "web_port"),
    (lambda c: c.update(updates={"check": "maybe"}), "updates.check"),
    (lambda c: c.update(active_schedule_set={"set_id": "p"}), "active_schedule_set"),
])
def test_invalid_settings_are_rejected_with_the_path_of_the_problem(mutate, expect):
    with pytest.raises(ct.ConfigImportError) as exc:
        ct.parse_document(broken(mutate))
    assert expect in str(exc.value)


def test_all_problems_are_reported_not_just_the_first():
    def mutate(c):
        c["driver"]["port"] = 0
        c["dmx"]["universe"] = -5
        c["scenes"][0]["name"] = ""
    with pytest.raises(ct.ConfigImportError) as exc:
        ct.parse_document(broken(mutate))
    assert len(exc.value.problems) == 3


def test_file_without_any_settings_is_refused():
    doc = ct.build_export(AppConfig())
    doc["config"] = {}
    with pytest.raises(ct.ConfigImportError, match="does not contain any settings"):
        ct.parse_document(doc)


def test_overlapping_dmx_channels_are_flagged():
    cfg = AppConfig(dmx=DmxConfig(mappings=[DmxFixtureMapping("a", 1, ["intensity", "on_off"]),
                                            DmxFixtureMapping("b", 2, ["intensity"])]))
    assert any("more than one zone" in w for w in ct.parse_document(ct.build_export(cfg)).warnings)


# ---------------------------------------------------------------- certificate files
def test_certificate_names_cannot_escape_the_target_folder(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path), include_credentials=True)
    evil = base64.b64encode(b"pwned").decode()
    doc["files"]["leap_certs"]["../../escape.txt"] = evil
    doc["files"]["leap_certs"]["/etc/passwd"] = evil
    parsed = ct.parse_document(doc)
    assert set(parsed.cert_files) == set(CERTS)
    assert sum("unexpected file" in w for w in parsed.warnings) == 2
    ct.import_into_config(AppConfig(), parsed, ["driver"])
    assert not (home / "escape.txt").exists() and not (tmp_path / "escape.txt").exists()


def test_bad_or_oversized_certificate_data_is_rejected(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path), include_credentials=True)
    doc["files"]["leap_certs"]["caseta.key"] = "not base64!!"
    with pytest.raises(ct.ConfigImportError, match="not valid base64"):
        ct.parse_document(doc)
    doc["files"]["leap_certs"]["caseta.key"] = base64.b64encode(b"x" * (ct.MAX_CERT_FILE_BYTES + 1)).decode()
    with pytest.raises(ct.ConfigImportError, match="too large"):
        ct.parse_document(doc)


def test_certificates_in_a_file_without_the_credentials_flag_are_ignored(tmp_path, home):
    doc = ct.build_export(make_config(tmp_path), include_credentials=True)
    doc["includes_credentials"] = False
    assert ct.parse_document(doc).cert_files == {}


# ---------------------------------------------------------------- active schedule set
def _sets_doc(with_active: bool, set_ids=("p1",)) -> dict:
    cfg = AppConfig(schedule_sets=[ScheduleSetConfig(i, f"Set {i}", [ScheduleRef("s1", "S")], []) for i in set_ids])
    if with_active:
        cfg.active_schedule_set = ActiveScheduleSet("p1", "Set p1", "t", [ActiveScheduleEntry("s1", "S", True, False)])
    return ct.build_export(cfg)


def test_active_set_is_only_brought_back_when_asked_and_nothing_is_active(home):
    parsed = ct.parse_document(_sets_doc(True))
    plain = AppConfig()
    ct.import_into_config(plain, parsed, ["schedule_sets"])
    assert plain.active_schedule_set is None and [s.id for s in plain.schedule_sets] == ["p1"]
    again = AppConfig()
    assert ct.active_set_status(again, parsed, ["schedule_sets"]) == (None, True)
    res = ct.import_into_config(again, parsed, ["schedule_sets"], restore_active_set=True)
    assert res.active_set_restored and again.active_schedule_set.entries[0].previous is True


def test_import_is_blocked_while_a_set_that_is_not_in_the_file_is_active(home):
    parsed = ct.parse_document(_sets_doc(False))
    current = AppConfig(schedule_sets=[ScheduleSetConfig("other", "Other", [ScheduleRef("s9", "X")], [])],
                        active_schedule_set=ActiveScheduleSet("other", "Other", "t", []))
    with pytest.raises(ct.ConfigImportError, match="active right now") as exc:
        ct.import_into_config(current, parsed, ["schedule_sets"])
    assert exc.value.status == 409
    assert [s.id for s in current.schedule_sets] == ["other"] and not (home / "backups").exists()
    # leaving the section out is fine
    ct.import_into_config(current, ct.parse_document(ct.build_export(AppConfig(scenes=[]))), ["scenes"])


def test_the_active_set_survives_an_import_that_still_contains_it_and_follows_a_rename(home):
    parsed = ct.parse_document(_sets_doc(False))
    current = AppConfig(schedule_sets=[ScheduleSetConfig("p1", "Old name", [ScheduleRef("s1", "S")], [])],
                        active_schedule_set=ActiveScheduleSet("p1", "Old name", "t", [ActiveScheduleEntry("s1", "S", True, False)]))
    ct.import_into_config(current, parsed, ["schedule_sets"], restore_active_set=True)
    assert current.active_schedule_set.set_name == "Set p1" and current.active_schedule_set.entries[0].previous is True


# ---------------------------------------------------------------- preview text
def test_preview_lists_changes_and_never_shows_secrets(tmp_path, home):
    exported = make_config(tmp_path)
    parsed = ct.parse_document(ct.build_export(exported, include_credentials=True))
    current = AppConfig(driver=DriverConfig(kind="ra2", host="10.0.0.99", username="x", password="y"),
                        scenes=[SceneConfig("s1", "Movie", {"z1": 99.0}), SceneConfig("gone", "Gone", {})])
    previews = {p.id: p for p in ct.describe_changes(current, parsed, list(ct.SECTIONS), {"z1": "Pendants"})}
    text = json.dumps([p.to_dict() for p in previews.values()])
    assert SECRET not in text and TOKEN not in text and "installer" not in text
    assert "address: 10.0.0.99 → 10.0.0.5" in previews["driver"].changes
    assert "Integration login: changed" in previews["driver"].changes
    assert any(l.startswith("Add 1 scene") and "Party" in l for l in previews["scenes"].changes)
    assert any(l.startswith("Remove 1 scene") and "Gone" in l for l in previews["scenes"].changes)
    assert any(l.startswith("Change 1 scene") and "Movie" in l for l in previews["scenes"].changes)
    assert any("Pendants (ch 1-2)" in l for l in previews["dmx"].changes)
    assert previews["web"].changes[0] == "web port: 8080 → 8123" or "web port: 8080 → 8123" in previews["web"].changes
    same = ct.describe_changes(exported, parsed, ["scenes"])
    assert same[0].changes == [] and not same[0].changed


def test_warnings_name_unknown_zones_and_missing_pieces(tmp_path, home):
    exported = make_config(tmp_path, kind="leap")
    exported.driver.leap_cert_dir = str(tmp_path / "no-such-folder")
    parsed = ct.parse_document(ct.build_export(exported))
    cfg = AppConfig(driver=DriverConfig(kind="leap", host="10.0.0.5"))
    warns = dict(ct.import_warnings(cfg, parsed, ["driver", "scenes", "dmx", "schedule_sets", "web"],
                                    zone_ids={"z1"}, schedule_ids=set()))
    assert "pair again" in warns["driver"]
    assert "1 scene" in warns["scenes"] and "1 DMX mapping" in warns["dmx"]
    assert "not found" in warns["schedule_sets"] and "web" not in warns  # loopback web address: nothing to say
    lan = ct.build_export(AppConfig(web_host="0.0.0.0"))
    assert "0.0.0.0" in dict(ct.import_warnings(cfg, ct.parse_document(lan), ["web"]))["web"]


# ---------------------------------------------------------------- web API
@pytest.fixture
def web(home, monkeypatch, tmp_path):
    from lutron_to_dmx_bridge import controller as controller_mod
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.web.app import create_app

    monkeypatch.setattr(controller_mod, "create_driver", lambda cfg: FakeDriver(cfg))
    cfg = make_config(tmp_path)
    driver = FakeDriver(cfg.driver)
    ctl = Controller(cfg, driver=driver)
    with TestClient(create_app(ctl)) as client:
        client.portal.call(driver.connect)
        yield client, ctl


def test_api_export_default_and_with_credentials(web):
    client, _ = web
    r = client.get("/api/config/export")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith('attachment; filename="lutron-to-dmx-bridge-config-')
    assert SECRET not in r.text and TOKEN not in r.text and r.json()["includes_credentials"] is False
    full = client.get("/api/config/export", params={"credentials": "true"}).json()
    assert full["config"]["driver"]["password"] == SECRET and full["includes_credentials"] is True


def test_api_backup_no_longer_leaks_the_update_token(web):
    client, _ = web
    body = client.get("/api/backup").text
    assert TOKEN not in body and SECRET not in body


def test_api_preview_changes_nothing(web):
    client, ctl = web
    doc = ct.build_export(AppConfig(driver=DriverConfig(kind="ra2", host="10.0.0.5"),  # same system as now
                                    scenes=[SceneConfig("n", "Fresh", {"z1": 1.0, "nope": 2.0})]))
    before = ctl.config.to_dict()
    r = client.post("/api/config/import/preview", json={"document": doc})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["file"]["app_version"] == __version__
    assert pv["selected"] == ["driver", "dmx", "scenes", "schedule_sets", "updates"]  # web address is opt-in
    scenes = next(s for s in pv["sections"] if s["id"] == "scenes")
    assert any("Add 1 scene" in c for c in scenes["changes"]) and any("Remove 2 scenes" in c for c in scenes["changes"])
    assert {"section": "scenes", "text": "1 scene(s) use zones that the connected Lutron system does not have."} in pv["warnings"]
    assert ctl.config.to_dict() == before and not list((ctl.config.path().parent / "backups").glob("config-*"))


def test_api_import_requires_confirmation_then_applies_and_reconnects(web):
    client, ctl = web
    old_driver = ctl.driver
    new_cfg = AppConfig(driver=DriverConfig(kind="ra2", host="10.0.0.77", username="u2", password="p2"),
                        scenes=[SceneConfig("n", "Fresh", {"z1": 1.0})],
                        dmx=DmxConfig(enabled=False, mappings=[DmxFixtureMapping("z1", 4, ["intensity"])]))
    body = {"document": ct.build_export(new_cfg, include_credentials=True)}
    r = client.post("/api/config/import", json=body)
    assert r.status_code == 422 and "confirm" in r.json()["detail"]
    assert ctl.config.driver.host == "10.0.0.5"

    r = client.post("/api/config/import", json={**body, "confirm": True,
                                               "sections": ["driver", "dmx", "scenes"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] == ["driver", "dmx", "scenes"] and out["driver_changed"] and os.path.exists(out["backup"])
    assert ctl.config.driver.host == "10.0.0.77" and ctl.config.driver.password == "p2"
    assert ctl.driver is not old_driver and ctl.driver.connected  # reconnected with the new settings
    assert [s.name for s in ctl.config.scenes] == ["Fresh"]
    assert ctl.config.dmx.mappings[0].address == 4
    assert client.get("/api/scenes").json()[0]["name"] == "Fresh"
    assert AppConfig.load().driver.host == "10.0.0.77"
    assert "installer" in open(out["backup"]).read()  # the settings before the import


def test_api_import_errors(web):
    client, ctl = web
    good = ct.build_export(AppConfig(scenes=[SceneConfig("n", "Fresh", {})]))
    good["config"].pop("dmx")
    newer = {**good, "schema_version": 99}
    r = client.post("/api/config/import", json={"document": newer, "confirm": True})
    assert r.status_code == 422 and "newer version" in r.json()["detail"]
    bad = copy.deepcopy(good)
    bad["config"]["scenes"][0]["name"] = ""
    r = client.post("/api/config/import/preview", json={"document": bad})
    assert r.status_code == 422 and "scenes.0.name" in r.json()["detail"]
    r = client.post("/api/config/import", json={"document": good, "confirm": True, "sections": ["dmx"]})
    assert r.status_code == 422 and "no DMX" in r.json()["detail"]
    r = client.post("/api/config/import", json={"document": [1], "confirm": True})
    assert r.status_code == 422
    active = {"document": _sets_doc(False, ("zz",)), "confirm": True}
    ctl.config.active_schedule_set = ActiveScheduleSet("p1", "Performance mode", "t", [])
    r = client.post("/api/config/import", json=active)
    assert r.status_code == 409 and "active right now" in r.json()["detail"]
    assert [s.name for s in ctl.config.scenes] == ["Movie", "Party"]  # every failed attempt changed nothing


def test_api_import_is_refused_from_another_site(web):
    client, _ = web
    r = client.post("/api/config/import", json={"document": {}, "confirm": True},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_gui_has_the_export_import_controls(web):
    client, _ = web
    page = client.get("/").text
    for el in ("transfer-card", "xfer-creds", "xfer-creds-warn", "btn-export", "import-file", "import-panel"):
        assert f'id="{el}"' in page
    js = client.get("/static/app.js").text
    assert "config/import/preview" in js and "credentials=true" in js


# ---------------------------------------------------------------- command line
def test_cli_export_then_import_round_trip(tmp_path, home, capsys, monkeypatch):
    monkeypatch.setattr("lutron_to_dmx_bridge.__main__._app_is_running", lambda cfg: False)
    make_config(tmp_path).save()
    out_file = tmp_path / "out" / "settings.json"
    cli_main(["export-config", str(out_file)])
    text = capsys.readouterr().out
    assert "not in the file" in text and SECRET not in out_file.read_text()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600

    cli_main(["export-config", str(tmp_path / "full.json"), "--include-credentials"])
    assert "WARNING" in capsys.readouterr().out
    assert SECRET in (tmp_path / "full.json").read_text()

    AppConfig().save()
    cli_main(["import-config", str(out_file), "--yes"])
    out = capsys.readouterr().out
    assert "Add 2 scenes" in out and "Imported:" in out and "Previous settings saved to" in out
    loaded = AppConfig.load()
    assert [s.name for s in loaded.scenes] == ["Movie", "Party"] and loaded.driver.host == "10.0.0.5"
    assert loaded.web_port == 8080  # web address not selected by default
    assert loaded.driver.password == "integration"  # not in the file, so the (default) current login stays


def test_cli_import_refuses_when_the_app_is_running_or_the_file_is_bad(tmp_path, home, capsys, monkeypatch):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(ct.build_export(make_config(tmp_path))))
    monkeypatch.setattr("lutron_to_dmx_bridge.__main__._app_is_running", lambda cfg: True)
    with pytest.raises(SystemExit) as exc:
        cli_main(["import-config", str(f), "--yes"])
    assert exc.value.code not in (0, None) and not (home / "config.json").exists()
    assert "Nothing was changed" not in capsys.readouterr().out
    monkeypatch.setattr("lutron_to_dmx_bridge.__main__._app_is_running", lambda cfg: False)
    cli_main(["import-config", str(f), "--yes", "--sections", "scenes"])
    assert [s.name for s in AppConfig.load().scenes] == ["Movie", "Party"]
    for body, msg in (("not json", "not valid JSON"), (json.dumps({"format": "x"}), "not a Lutron")):
        f.write_text(body)
        with pytest.raises(SystemExit) as exc:
            cli_main(["import-config", str(f), "--yes"])
        assert msg in str(exc.value)
    with pytest.raises(SystemExit):
        cli_main(["import-config", str(tmp_path / "missing.json"), "--yes"])


def test_cli_import_without_yes_does_not_prompt_when_not_interactive(tmp_path, home, monkeypatch):
    monkeypatch.setattr("lutron_to_dmx_bridge.__main__._app_is_running", lambda cfg: False)
    f = tmp_path / "s.json"
    f.write_text(json.dumps(ct.build_export(make_config(tmp_path))))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        cli_main(["import-config", str(f)])
    assert not (home / "config.json").exists()
