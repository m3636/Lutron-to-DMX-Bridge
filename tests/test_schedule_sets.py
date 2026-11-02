"""Schedule sets ("Performance mode"): manager logic on the simulator and the RA2 driver, plus the web API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lutron_to_dmx_bridge.config import ActiveScheduleSet, AppConfig, DriverConfig, ScheduleRef
from lutron_to_dmx_bridge.controller import Controller
from lutron_to_dmx_bridge.drivers import DriverError
from lutron_to_dmx_bridge.drivers.ra2 import RA2Driver
from lutron_to_dmx_bridge.drivers.simulator import SimulatorDriver
from lutron_to_dmx_bridge.models import Schedule
from lutron_to_dmx_bridge.schedule_sets import ScheduleSetError, resolve_schedule
from lutron_to_dmx_bridge.web.app import create_app
from test_ra2 import FakeRepeater, make_config

# simulator schedules: 70:1 "Sunset Exterior On" (enabled), 70:2 "Midnight All Off" (enabled),
# 70:3 "Morning Step Lights Off" (already disabled)


class FlakySimulator(SimulatorDriver):
    """Simulator whose set_schedule_enabled fails for some ids or silently does nothing for others."""

    def __init__(self, *a, fail=(), ignore=(), **kw):
        super().__init__(*a, **kw)
        self.fail, self.ignore = set(fail), set(ignore)

    async def set_schedule_enabled(self, schedule_id, enabled):
        if schedule_id in self.fail:
            raise DriverError("repeater said no")
        if schedule_id in self.ignore:
            return
        await super().set_schedule_enabled(schedule_id, enabled)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(tmp_path))
    return tmp_path


async def make_ctl(driver=None, config=None):
    driver = driver or SimulatorDriver(DriverConfig(kind="simulator"))
    await driver.connect()
    return Controller(config or AppConfig(), driver=driver)


def enabled(ctl):
    return {s.id: s.enabled for s in ctl.driver.schedules()}


# -- editing -----------------------------------------------------------------------------
async def test_save_validates_and_persists(home):
    ctl = await make_ctl()
    m = ctl.schedule_sets
    s = m.save("Performance mode", ["70:1", "70:2"], [])
    assert [(r.id, r.name) for r in s.disable] == [("70:1", "Sunset Exterior On"), ("70:2", "Midnight All Off")]
    saved = json.loads((home / "config.json").read_text())["schedule_sets"][0]
    assert saved["name"] == "Performance mode" and saved["disable"][0]["id"] == "70:1"

    with pytest.raises(ScheduleSetError) as e:
        m.save("performance MODE", ["70:1"], [])
    assert e.value.status == 409
    with pytest.raises(ScheduleSetError) as e:
        m.save("X", ["nope"], [])
    assert e.value.status == 404
    with pytest.raises(ScheduleSetError) as e:
        m.save("X", ["70:1"], ["70:1"])
    assert e.value.status == 422
    with pytest.raises(ScheduleSetError) as e:
        m.save("X", [], [])
    assert e.value.status == 422
    with pytest.raises(ScheduleSetError) as e:
        m.save("  ", ["70:1"], [])
    assert e.value.status == 422

    renamed = m.save("Show", ["70:1"], ["70:3"], set_id=s.id)
    assert renamed.id == s.id and len(m.sets) == 1 and renamed.enable[0].id == "70:3"
    m.delete(s.id)
    assert m.sets == []
    with pytest.raises(KeyError):
        m.get(s.id)


def test_config_roundtrip(home):
    cfg = AppConfig()
    cfg.active_schedule_set = ActiveScheduleSet("a", "Show", "2026-10-21T20:00:00+00:00", [], ["warn"])
    cfg.save()
    again = AppConfig.load()
    assert again.active_schedule_set.warnings == ["warn"] and again.schedule_sets == []
    # configs written before this feature still load
    (home / "config.json").write_text(json.dumps({"web_port": 9000}))
    old = AppConfig.load()
    assert old.schedule_sets == [] and old.active_schedule_set is None


# -- activate / restore on the simulator ---------------------------------------------------
async def test_activate_records_prior_state_and_restore_is_exact(home):
    ctl = await make_ctl()
    m = ctl.schedule_sets
    # 70:3 is already disabled, so the set must leave it alone and restore must not enable it
    s = m.save("Performance mode", ["70:1", "70:3"], [])
    res = await m.activate(s.id)
    assert res["ok"] and res["active"]
    assert res["changed"] == ["Sunset Exterior On"] and res["unchanged"] == ["Morning Step Lights Off"]
    assert enabled(ctl) == {"70:1": False, "70:2": True, "70:3": False}
    on_disk = json.loads((home / "config.json").read_text())["active_schedule_set"]
    assert on_disk["set_name"] == "Performance mode"
    assert on_disk["entries"] == [{"id": "70:1", "name": "Sunset Exterior On", "previous": True, "desired": False}]
    assert m.status()["count"] == 1

    res = await m.deactivate()
    assert res["ok"] and not res["active"] and res["restored"] == ["Sunset Exterior On"]
    assert enabled(ctl) == {"70:1": True, "70:2": True, "70:3": False}
    assert json.loads((home / "config.json").read_text())["active_schedule_set"] is None
    assert m.status() is None


async def test_enable_list_is_reverted_too():
    ctl = await make_ctl()
    m = ctl.schedule_sets
    s = m.save("Show", ["70:2"], ["70:3"])
    await m.activate(s.id)
    assert enabled(ctl) == {"70:1": True, "70:2": False, "70:3": True}
    await m.deactivate()
    assert enabled(ctl) == {"70:1": True, "70:2": True, "70:3": False}


async def test_active_state_survives_restart(home):
    ctl = await make_ctl()
    s = ctl.schedule_sets.save("Show", ["70:1", "70:2"], [])
    await ctl.schedule_sets.activate(s.id)
    # new process: config reloaded from disk, driver reconnects and reports what the system holds
    ctl2 = await make_ctl(config=AppConfig.load())
    ctl2.driver.schedules()[0].enabled = False
    ctl2.driver.schedules()[1].enabled = False
    assert ctl2.schedule_sets.status()["set_name"] == "Show"
    res = await ctl2.schedule_sets.deactivate()
    assert res["ok"] and enabled(ctl2) == {"70:1": True, "70:2": True, "70:3": False}


async def test_only_one_set_active_and_active_set_cannot_be_deleted():
    ctl = await make_ctl()
    m = ctl.schedule_sets
    a, b = m.save("A", ["70:1"], []), m.save("B", ["70:2"], [])
    await m.activate(a.id)
    with pytest.raises(ScheduleSetError) as e:
        await m.activate(b.id)
    assert e.value.status == 409
    with pytest.raises(ScheduleSetError) as e:
        m.delete(a.id)
    assert e.value.status == 409
    m.save("A renamed", ["70:1"], [], set_id=a.id)
    assert m.status()["set_name"] == "A renamed"
    m.forget_active()  # drops the record without touching the schedules
    assert m.status() is None
    assert enabled(ctl)["70:1"] is False
    with pytest.raises(ScheduleSetError) as e:
        await m.deactivate()
    assert e.value.status == 404


async def test_partial_failure_is_reported_and_restore_can_be_retried():
    drv = FlakySimulator(DriverConfig(kind="simulator"), fail={"70:2"})
    ctl = await make_ctl(drv)
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1", "70:2"], [])
    res = await m.activate(s.id)
    assert not res["ok"] and res["active"] and res["changed"] == ["Sunset Exterior On"]
    assert res["failures"] == ["Midnight All Off: repeater said no"]
    assert any("Midnight All Off" in w for w in m.status()["warnings"])
    assert enabled(ctl)["70:1"] is False and enabled(ctl)["70:2"] is True

    # restore: 70:1 restores; 70:2 still fails on the way back and stays on the record
    drv.fail = {"70:2"}
    drv.schedules()[1].enabled = False  # pretend it did get turned off
    res = await m.deactivate()
    assert not res["ok"] and res["active"] and res["restored"] == ["Sunset Exterior On"]
    assert [e.id for e in ctl.config.active_schedule_set.entries] == ["70:2"]
    drv.fail = set()
    res = await m.deactivate()
    assert res["ok"] and not res["active"]
    assert enabled(ctl) == {"70:1": True, "70:2": True, "70:3": False}


async def test_total_failure_does_not_leave_the_set_active():
    ctl = await make_ctl(FlakySimulator(DriverConfig(kind="simulator"), fail={"70:1", "70:2"}))
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1", "70:2"], [])
    with pytest.raises(ScheduleSetError) as e:
        await m.activate(s.id)
    assert e.value.status == 502 and "Sunset Exterior On" in str(e.value)
    assert m.status() is None and enabled(ctl)["70:1"] is True


async def test_change_the_system_ignores_is_a_failure():
    ctl = await make_ctl(FlakySimulator(DriverConfig(kind="simulator"), ignore={"70:2"}))
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1", "70:2"], [])
    res = await m.activate(s.id)
    assert res["failures"] == ["Midnight All Off: the system still reports it as enabled"]


async def test_not_connected_changes_nothing():
    ctl = await make_ctl()
    s = ctl.schedule_sets.save("Show", ["70:1"], [])
    await ctl.schedule_sets.activate(s.id)
    await ctl.driver.close()
    ctl.driver._connected = False
    with pytest.raises(ScheduleSetError) as e:
        await ctl.schedule_sets.deactivate()
    assert e.value.status == 502
    assert ctl.schedule_sets.status() is not None  # the record is kept for when the link is back


# -- renamed / disappeared schedules ---------------------------------------------------------
def sched(i, n):
    return Schedule(i, n)


def test_resolve_schedule_cases():
    live = [sched("1:1", "Sunset"), sched("1:2", "Dawn")]
    assert resolve_schedule(ScheduleRef("1:1", "Sunset"), live)[0].id == "1:1"
    assert resolve_schedule(ScheduleRef("1:1", "Old name"), live)[0].id == "1:1"  # renamed
    assert resolve_schedule(ScheduleRef("1:9", "Dawn"), live)[0].id == "1:2"  # renumbered
    s, why = resolve_schedule(ScheduleRef("1:9", "Gone"), live)
    assert s is None and "not found" in why
    s, why = resolve_schedule(ScheduleRef("1:1", "Dawn"), live)  # id says Sunset, name says Dawn
    assert s is None and "ambiguous" in why
    s, why = resolve_schedule(ScheduleRef("1:9", "Dawn"), live + [sched("1:3", "Dawn")])
    assert s is None and "several" in why


async def test_missing_and_renamed_schedules_during_activate():
    ctl = await make_ctl()
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1", "70:2", "70:3"], [])
    drv = ctl.driver
    drv.schedules()[0].name = "Sunset Exterior (renamed)"  # 70:1 renamed in the Lutron software
    drv._inventory.schedules.pop(1)  # 70:2 deleted
    res = await m.activate(s.id)
    assert res["changed"] == ["Sunset Exterior (renamed)"]
    assert len(res["missing"]) == 1 and "Midnight All Off" in res["missing"][0]
    assert not res["ok"]  # the skip is surfaced, not hidden
    assert "Skipped" in m.status()["warnings"][0]
    assert (await m.deactivate())["ok"]
    assert enabled(ctl)["70:1"] is True


async def test_set_whose_schedules_are_all_gone_cannot_be_activated():
    ctl = await make_ctl()
    s = ctl.schedule_sets.save("Show", ["70:1"], [])
    ctl.driver._inventory.schedules.clear()
    with pytest.raises(ScheduleSetError) as e:
        await ctl.schedule_sets.activate(s.id)
    assert e.value.status == 409 and ctl.schedule_sets.status() is None


async def test_restore_skips_a_schedule_that_vanished_meanwhile():
    ctl = await make_ctl()
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1", "70:2"], [])
    await m.activate(s.id)
    ctl.driver._inventory.schedules.pop(0)
    res = await m.deactivate()
    assert res["restored"] == ["Midnight All Off"] and len(res["missing"]) == 1
    assert not res["active"] and enabled(ctl)["70:2"] is True


# -- RA2 driver against the fake repeater ----------------------------------------------------
@pytest.fixture
async def repeater():
    r = FakeRepeater()
    r.levels = {"40": 75.0}
    r.timeclock[("70", "3")] = 2
    await r.start()
    yield r
    await r.stop()


@pytest.fixture
async def ra2(repeater):
    d = RA2Driver(make_config(repeater.port), connect_timeout=2)
    d.schedule_query_timeout = 0.5
    ctl = await make_ctl(d)
    yield ctl, repeater
    await d.close()


async def test_ra2_activate_and_restore_send_timeclock_commands(ra2):
    ctl, rep = ra2
    m = ctl.schedule_sets
    s = m.save("Show", ["70:1"], ["70:3"])
    res = await m.activate(s.id)
    assert res["ok"] and sorted(res["changed"]) == ["Morning Step Lights Off", "Sunset Exterior On"]
    assert "#TIMECLOCK,70,6,1,2" in rep.commands and "#TIMECLOCK,70,6,3,1" in rep.commands
    assert rep.timeclock[("70", "1")] == 2 and rep.timeclock[("70", "3")] == 1

    res = await m.deactivate()
    assert res["ok"] and not res["active"]
    assert rep.timeclock[("70", "1")] == 1 and rep.timeclock[("70", "3")] == 2


async def test_ra2_prior_state_comes_from_the_repeater_not_the_cache(ra2):
    ctl, rep = ra2
    m = ctl.schedule_sets
    s = m.save("Show", ["70:2"], [])
    # someone disabled 70:2 in the Lutron app after we connected; our cache still says enabled
    rep.timeclock[("70", "2")] = 2
    assert enabled(ctl)["70:2"] is True
    res = await m.activate(s.id)
    assert res["changed"] == [] and res["unchanged"] == ["Midnight All Off"]
    await m.deactivate()
    assert rep.timeclock[("70", "2")] == 2  # restored to what it was, not blindly enabled


async def test_ra2_repeater_that_does_not_answer_queries_is_flagged_unconfirmed(ra2):
    ctl, rep = ra2
    s = ctl.schedule_sets.save("Show", ["70:1"], [])
    orig = rep.reply
    rep.reply = lambda cmd: "GNET> \r\n" if cmd.startswith("?TIMECLOCK") else orig(cmd)
    res = await ctl.schedule_sets.activate(s.id)
    assert res["unconfirmed"] == ["Sunset Exterior On"] and res["ok"]
    assert any("No confirmation" in w for w in ctl.schedule_sets.status()["warnings"])


# -- web API ---------------------------------------------------------------------------------
@pytest.fixture
def api():
    ctl = Controller(AppConfig(), driver=SimulatorDriver(DriverConfig(kind="simulator")))
    with TestClient(create_app(ctl)) as client:
        client.portal.call(ctl.driver.connect)
        yield client, ctl


def test_api_crud_activate_deactivate(api):
    client, ctl = api
    r = client.post("/api/schedule-sets", json={"name": "Performance mode", "disable": ["70:1", "70:2"]})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert [x["name"] for x in r.json()["disable"]] == ["Sunset Exterior On", "Midnight All Off"]
    assert client.post("/api/schedule-sets", json={"name": "performance mode", "disable": ["70:1"]}).status_code == 409
    assert client.post("/api/schedule-sets", json={"name": "Bad", "disable": ["nope"]}).status_code == 404
    assert client.post("/api/schedule-sets", json={"name": "Bad"}).status_code == 422
    assert client.get("/api/schedule-sets").json()["active"] is None

    r = client.put(f"/api/schedule-sets/{sid}", json={"name": "Show", "disable": ["70:1"], "enable": ["70:3"]})
    assert r.status_code == 200 and r.json()["name"] == "Show"
    assert client.put("/api/schedule-sets/zzz", json={"name": "x", "disable": ["70:1"]}).status_code == 404

    r = client.post(f"/api/schedule-sets/{sid}/activate")
    assert r.status_code == 200 and r.json()["ok"] is True
    inv = {s["id"]: s["enabled"] for s in client.get("/api/inventory").json()["schedules"]}
    assert inv == {"70:1": False, "70:2": True, "70:3": True}
    st = client.get("/api/status").json()["schedule_set"]
    assert st["set_name"] == "Show" and st["count"] == 2
    listing = client.get("/api/schedule-sets").json()
    assert listing["active"]["set_id"] == sid and listing["sets"][0]["active"] is True
    assert client.post(f"/api/schedule-sets/{sid}/activate").status_code == 409
    assert client.delete(f"/api/schedule-sets/{sid}").status_code == 409

    r = client.post("/api/schedule-sets/active/deactivate")
    assert r.status_code == 200 and r.json()["ok"] and not r.json()["active"]
    inv = {s["id"]: s["enabled"] for s in client.get("/api/inventory").json()["schedules"]}
    assert inv == {"70:1": True, "70:2": True, "70:3": False}
    assert client.get("/api/status").json()["schedule_set"] is None
    assert client.post("/api/schedule-sets/active/deactivate").status_code == 404
    assert client.post("/api/schedule-sets/zzz/activate").status_code == 404
    assert client.delete(f"/api/schedule-sets/{sid}").status_code == 200
    assert client.delete(f"/api/schedule-sets/{sid}").status_code == 404


def test_api_reports_missing_schedules_in_a_set(api):
    client, ctl = api
    sid = client.post("/api/schedule-sets", json={"name": "Show", "disable": ["70:1", "70:2"]}).json()["id"]
    ctl.driver._inventory.schedules.pop(1)
    got = client.get("/api/schedule-sets").json()["sets"][0]
    assert [(r["id"], r["missing"]) for r in got["disable"]] == [("70:1", False), ("70:2", True)]
    # saving the set again keeps the vanished entry rather than silently dropping it
    r = client.put(f"/api/schedule-sets/{sid}", json={"name": "Show", "disable": ["70:1", "70:2"]})
    assert r.status_code == 200 and len(r.json()["disable"]) == 2


def test_api_total_failure_is_a_502(api):
    client, ctl = api
    sid = client.post("/api/schedule-sets", json={"name": "Show", "disable": ["70:1"]}).json()["id"]

    async def boom(schedule_id, enabled):
        raise DriverError("repeater said no")

    ctl.driver.set_schedule_enabled = boom
    r = client.post(f"/api/schedule-sets/{sid}/activate")
    assert r.status_code == 502 and "repeater said no" in r.json()["detail"]
    assert client.get("/api/status").json()["schedule_set"] is None


def test_api_forget_active(api):
    client, ctl = api
    sid = client.post("/api/schedule-sets", json={"name": "Show", "disable": ["70:1"]}).json()["id"]
    client.post(f"/api/schedule-sets/{sid}/activate")
    assert client.post("/api/schedule-sets/active/forget").status_code == 200
    assert client.get("/api/status").json()["schedule_set"] is None
    assert client.post("/api/schedule-sets/active/forget").status_code == 404
    assert enabled(ctl)["70:1"] is False


def test_frontend_assets_know_the_endpoints(api):
    client, _ = api
    assert "schedule-sets" in client.get("/static/app.js").text
    assert 'id="set-banner"' in client.get("/").text
