"""Host and Origin checks: other web pages must not be able to drive the app through the browser."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lutron_to_dmx_bridge.config import AppConfig, DriverConfig
from lutron_to_dmx_bridge.controller import Controller
from lutron_to_dmx_bridge.web.app import create_app
from lutron_to_dmx_bridge.web.guard import host_name, is_loopback_bind


def make_client(bind_host=None):
    cfg = AppConfig(driver=DriverConfig(kind="simulator"))
    ctl = Controller(cfg)
    return TestClient(create_app(ctl, bind_host=bind_host))


def test_host_name_parsing():
    assert host_name("127.0.0.1:8080") == "127.0.0.1"
    assert host_name("LocalHost") == "localhost"
    assert host_name("[::1]:8080") == "::1"
    assert host_name("evil.example:80") == "evil.example"
    assert is_loopback_bind(None) and is_loopback_bind("localhost") and is_loopback_bind("::1")
    assert not is_loopback_bind("0.0.0.0") and not is_loopback_bind("192.168.1.5")


def test_loopback_hosts_are_answered():
    with make_client() as c:
        for host in ("127.0.0.1:8080", "localhost:8080", "127.0.0.1"):
            assert c.get("/api/status", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "192.168.1.5:8080", ""])
def test_rebinding_host_is_refused(host):
    with make_client() as c:
        r = c.get("/api/status", headers={"host": host})
        assert r.status_code == 403 and "Host" in r.json()["detail"]


def test_cross_site_post_is_refused_but_same_origin_and_no_origin_work():
    with make_client() as c:
        url = "/api/alerts/clear"
        assert c.post(url, headers={"origin": "https://evil.example"}).status_code == 403
        assert c.post(url, headers={"origin": "null"}).status_code == 403
        assert c.post(url, headers={"origin": "http://localhost:9999"}).status_code == 403
        assert c.post(url, headers={"origin": "http://127.0.0.1"}).status_code == 200  # same as Host
        assert c.post(url).status_code == 200  # curl, scripts: no Origin header
        # Reading is never blocked by Origin
        assert c.get("/api/status", headers={"origin": "https://evil.example"}).status_code == 200


def test_cross_site_websocket_is_refused():
    with make_client() as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
                pass


def test_lan_bind_skips_the_host_check_but_not_the_origin_check():
    with make_client(bind_host="0.0.0.0") as c:
        assert c.get("/api/status", headers={"host": "192.168.1.5:8080"}).status_code == 200
        r = c.post("/api/alerts/clear", headers={"host": "192.168.1.5:8080", "origin": "https://evil.example"})
        assert r.status_code == 403
        r = c.post("/api/alerts/clear", headers={"host": "192.168.1.5:8080", "origin": "http://192.168.1.5:8080"})
        assert r.status_code == 200
