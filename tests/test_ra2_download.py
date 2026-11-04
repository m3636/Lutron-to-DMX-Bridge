"""The RA2 driver's DbXmlInfo.xml download: slow and flaky repeater web server, retries, cached copy."""

import asyncio
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lutron_to_dmx_bridge.config import AppConfig, DriverConfig
from lutron_to_dmx_bridge.controller import Controller
from lutron_to_dmx_bridge.drivers import DriverError
from lutron_to_dmx_bridge.drivers.base import LutronDriver
from lutron_to_dmx_bridge.drivers.ra2 import RA2Driver
from lutron_to_dmx_bridge.drivers.simulator import SAMPLE_DATA_DIR

from test_ra2 import FakeRepeater

BODY = (SAMPLE_DATA_DIR / "ra2_sample_dbxml.xml").read_bytes()


class FakeWebServer:
    """The repeater's web server.  `script` is consumed one entry per request:
    "ok", "stall" (no reply for `stall_s`), "trickle" (the reply in small slow pieces),
    "cut" (promises the whole file, sends half), or "500"."""

    def __init__(self, script=("ok",), stall_s=1.0):
        self.script, self.stall_s = list(script), stall_s
        self.requests = 0
        self.active = self.max_active = 0
        self.user_agents: list[str] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with owner._lock:
                    owner.requests += 1
                    owner.active += 1
                    owner.max_active = max(owner.max_active, owner.active)
                    owner.user_agents.append(self.headers.get("User-Agent", ""))
                    mode = owner.script.pop(0) if len(owner.script) > 1 else owner.script[0]
                try:
                    if mode == "stall":
                        time.sleep(owner.stall_s)
                    if mode == "500":
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/xml")
                    self.send_header("Content-Length", str(len(BODY)))
                    self.end_headers()
                    if mode == "cut":
                        self.wfile.write(BODY[: len(BODY) // 2])
                        return
                    if mode == "trickle":
                        step = len(BODY) // 5 + 1
                        for i in range(0, len(BODY), step):
                            if i:
                                time.sleep(0.15)
                            self.wfile.write(BODY[i:i + step])
                            self.wfile.flush()
                        return
                    self.wfile.write(BODY)
                except OSError:
                    pass
                finally:
                    with owner._lock:
                        owner.active -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def web():
    servers = []

    def make(*a, **kw):
        servers.append(FakeWebServer(*a, **kw))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


@pytest.fixture
async def repeater():
    r = FakeRepeater()
    await r.start()
    yield r
    await r.stop()


def make_driver(repeater, web, tmp_path, **kw):
    cfg = DriverConfig(kind="ra2", host="127.0.0.1", port=repeater.port)
    opts = dict(connect_timeout=2, http_port=web.port, cache_dir=tmp_path, read_timeout=0.4,
                download_pause=(0.0,), cache_grace=0.5)
    opts.update(kw)
    return RA2Driver(cfg, **opts)


async def test_download_is_cached_and_identified(repeater, web, tmp_path):
    w = web()
    d = make_driver(repeater, w, tmp_path)
    await d.connect()
    try:
        assert d.inventory_source == "download" and len(d.zones()) == 12
        assert (tmp_path / "dbxml-127.0.0.1.xml").read_bytes() == BODY
        assert w.user_agents == ["Lutron-To-DMX-Bridge"]
    finally:
        await d.close()


async def test_slow_trickle_longer_than_read_timeout_still_succeeds(repeater, web, tmp_path):
    """The timeout is per read, not for the whole download: ~0.75 s in total here, 0.4 s per read."""
    d = make_driver(repeater, web(["trickle"]), tmp_path)
    await d.connect()
    try:
        assert d.inventory_source == "download"
    finally:
        await d.close()


@pytest.mark.parametrize("first", ["stall", "cut", "500"])
async def test_a_failed_first_attempt_is_retried(repeater, web, tmp_path, first):
    w = web([first, "ok"], stall_s=1.0)
    d = make_driver(repeater, w, tmp_path)
    await d.connect()
    try:
        assert d.inventory_source == "download" and w.requests == 2
    finally:
        await d.close()


async def test_truncated_reply_is_never_cached(repeater, web, tmp_path):
    d = make_driver(repeater, web(["cut"]), tmp_path, download_attempts=2)
    with pytest.raises(DriverError):
        await d.connect()
    assert not list(tmp_path.glob("*.xml"))


async def test_failure_without_cache_is_one_clear_error(repeater, web, tmp_path):
    w = web(["stall"], stall_s=1.0)
    d = make_driver(repeater, w, tmp_path)
    with pytest.raises(DriverError) as err:
        await d.connect()
    msg = str(err.value)
    assert "tried 3 times" in msg and "timed out" in msg and "integration report file" in msg
    assert w.requests == 3


async def test_failure_with_cache_starts_from_the_copy(repeater, web, tmp_path, caplog):
    (tmp_path / "dbxml-127.0.0.1.xml").write_bytes(BODY)
    d = make_driver(repeater, web(["500"]), tmp_path)
    with caplog.at_level(logging.WARNING):
        await d.connect()
    try:
        assert d.inventory_source == "cache" and len(d.zones()) == 12 and d.connected
        assert "Starting from the copy saved earlier" in caplog.text
    finally:
        await d.close()


async def test_slow_download_with_cache_does_not_hold_up_the_connect(repeater, web, tmp_path):
    (tmp_path / "dbxml-127.0.0.1.xml").write_bytes(BODY)
    d = make_driver(repeater, web(["stall"], stall_s=0.8), tmp_path, read_timeout=5, cache_grace=0.1)
    t0 = time.monotonic()
    await d.connect()
    try:
        assert time.monotonic() - t0 < 0.6 and d.inventory_source == "cache"
        # the download carries on in the background and leaves the cache current
        assert d._download_task is not None
        await asyncio.wait_for(d._download_task, 3)
    finally:
        await d.close()


async def test_unreadable_cache_is_ignored(repeater, web, tmp_path):
    (tmp_path / "dbxml-127.0.0.1.xml").write_text("<Project><oops")
    d = make_driver(repeater, web(), tmp_path)
    await d.connect()
    try:
        assert d.inventory_source == "download"
    finally:
        await d.close()


async def test_downloads_never_overlap(repeater, web, tmp_path):
    """Two drivers (two Save clicks, a second tab) must not hit the repeater at the same time."""
    w = web(["trickle"])
    a = make_driver(repeater, w, tmp_path / "a")
    b = make_driver(repeater, w, tmp_path / "b")
    try:
        await asyncio.gather(a.connect(), b.connect())
        assert w.requests == 2 and w.max_active == 1
    finally:
        await a.close()
        await b.close()


async def test_close_cancels_a_pending_download(repeater, web, tmp_path):
    w = web(["stall"], stall_s=1.0)
    d = make_driver(repeater, w, tmp_path, read_timeout=5, download_attempts=3)
    task = asyncio.create_task(d.connect())
    await asyncio.sleep(0.2)
    await d.close()
    with pytest.raises((asyncio.CancelledError, DriverError)):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(1.2)
    assert w.requests == 1  # no retry after the close


# -- Controller ----------------------------------------------------------------
class SlowDriver(LutronDriver):
    def __init__(self, delay, fail=None):
        super().__init__()
        self.delay, self.fail = delay, fail
        self.closed = self.up = False

    async def connect(self):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.up = True

    async def close(self):
        self.closed = True

    connected = property(lambda self: self.up)
    inventory = lambda self: None  # noqa: E731

    async def set_level(self, *a, **kw): ...
    async def refresh_levels(self): ...
    async def set_schedule_enabled(self, *a, **kw): ...
    async def backup(self): ...


async def test_driver_error_is_logged_on_one_line(caplog):
    cfg = AppConfig()
    cfg.dmx.enabled = False
    ctl = Controller(cfg, driver=SlowDriver(0, fail=DriverError("Could not download x: timed out")))
    with caplog.at_level(logging.ERROR):
        await ctl.start()
    recs = [r for r in caplog.records if "Driver connect failed" in r.getMessage()]
    assert len(recs) == 1 and recs[0].exc_info is None
    assert "timed out" in recs[0].getMessage() and ctl.last_error == "Could not download x: timed out"


async def test_unexpected_error_keeps_its_traceback(caplog):
    cfg = AppConfig()
    cfg.dmx.enabled = False
    ctl = Controller(cfg, driver=SlowDriver(0, fail=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        await ctl.start()
    assert any(r.exc_info for r in caplog.records)


async def test_new_settings_stop_the_first_connect(monkeypatch, tmp_path):
    """The first connect (from start-up) must not carry on and connect a driver nobody uses."""
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.dmx.enabled = False
    first = SlowDriver(0.5)
    ctl = Controller(cfg, driver=first)
    starting = asyncio.create_task(ctl.start())
    await asyncio.sleep(0.05)
    second = SlowDriver(0)
    monkeypatch.setattr("lutron_to_dmx_bridge.controller.create_driver", lambda c: second)
    await ctl.reconfigure_driver(DriverConfig(kind="simulator"))
    await asyncio.wait_for(starting, 2)
    await asyncio.sleep(0.6)
    assert first.closed and not first.up
    assert second.up and ctl.driver is second and ctl.last_error is None
