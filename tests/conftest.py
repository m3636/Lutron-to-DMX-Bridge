"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def no_update_checks(monkeypatch):
    """The app asks GitHub for new releases on its own; tests must never do that."""
    monkeypatch.setenv("LUTRON_DMX_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def loopback_test_client(monkeypatch):
    """The app only answers loopback Host names by default, so test clients talk to 127.0.0.1."""
    from starlette.testclient import TestClient

    original = TestClient.__init__

    def init(self, app, base_url="http://127.0.0.1", *args, **kwargs):
        original(self, app, base_url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)

    original_ws = TestClient.websocket_connect

    def websocket_connect(self, url, *args, **kwargs):  # it ignores base_url and says "testserver"
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("host", "127.0.0.1")
        return original_ws(self, url, *args, headers=headers, **kwargs)

    monkeypatch.setattr(TestClient, "websocket_connect", websocket_connect)
