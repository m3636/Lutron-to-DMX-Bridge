"""Keeps other web pages from driving the app through the user's own browser.

The web GUI has no login: it is meant to be reached from the computer it runs on. Two things stop a web
page opened in the same browser from using it:

* **Host check (loopback binds only).**  A DNS-rebinding page makes the browser send requests to
  127.0.0.1 under the attacker's own host name.  When the app listens on loopback, only requests whose
  ``Host`` is a loopback name (or the configured host) are answered.  If the user chose to listen on a
  LAN address, anyone who can reach the port can already use the app, so the host is not checked.
* **Origin check (everything that changes something).**  A page on another site can submit a request to
  this app.  Browsers attach an ``Origin`` header to those, so a POST/PUT/PATCH/DELETE or a WebSocket
  upgrade whose ``Origin`` is not this app's own address is refused.  Requests with no ``Origin``
  (curl, scripts, the app's own tools) are allowed.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def host_name(host_header: str) -> str:
    """The host name from a ``Host`` header value, without port or IPv6 brackets, lower-cased."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_loopback_bind(bind_host: str | None) -> bool:
    return (bind_host or "127.0.0.1").strip().lower() in LOOPBACK_NAMES | {"[::1]"}


class HostOriginGuard:
    """Pure ASGI middleware (it also covers WebSocket handshakes)."""

    def __init__(self, app, bind_host: str | None = None) -> None:
        self.app = app
        self.loopback_only = is_loopback_bind(bind_host)
        self.allowed = set(LOOPBACK_NAMES)
        if bind_host:
            self.allowed.add(bind_host.strip().lower().strip("[]"))

    def _verdict(self, scope) -> str | None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if self.loopback_only and host_name(host) not in self.allowed:
            return "Unexpected Host header. Open the app at http://127.0.0.1:<port>/."
        origin = headers.get("origin")
        changes_state = scope["type"] == "websocket" or scope.get("method", "GET") not in SAFE_METHODS
        if origin is not None and changes_state:
            if origin.lower() == "null" or urlsplit(origin).netloc.lower() != host.strip().lower():
                return "Cross-site request refused."
        return None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        problem = self._verdict(scope)
        if problem is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await receive()  # the connect message
            await send({"type": "websocket.close", "code": 1008})
        else:
            body = json.dumps({"detail": problem}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
