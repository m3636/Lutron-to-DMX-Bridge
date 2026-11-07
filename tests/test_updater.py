"""Update check and installer, against a fake GitHub release server.

What cannot be tested here: replacing a running macOS .app or Windows exe on a real machine.  The pieces
around it are tested instead: the check, the download and checksum, unpacking, the plan, and the helper's
swap / rollback on temp folders with the relaunch stubbed.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import ssl
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lutron_to_dmx_bridge import updater as upd
from lutron_to_dmx_bridge.config import AppConfig, DriverConfig
from lutron_to_dmx_bridge.updater import InstallInfo, UpdateError, Updater

REPO = "owner/repo"
WIN = "win32"


# ---------------------------------------------------------------------------
# fake GitHub
# ---------------------------------------------------------------------------
class FakeGitHub:
    """Serves /repos/<repo>/releases/latest and the release downloads, and remembers every request."""

    def __init__(self) -> None:
        self.release: dict | None = None
        self.files: dict[str, bytes] = {}
        self.status = 200
        self.headers: dict[str, str] = {}  # extra headers on an error answer
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path == f"/repos/{REPO}/releases/latest":
                    if outer.status != 200 or outer.release is None:
                        self.send_response(outer.status or 404)
                        for k, v in outer.headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        return
                    body = json.dumps(outer.release).encode()
                elif self.path in outer.files:
                    body = outer.files[self.path]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def publish(self, version: str, platform: str = WIN, *, zip_bytes: bytes | None = None, sums: str | None = None,
                with_sums: bool = True, with_zip: bool = True, **extra) -> bytes:
        os_name = upd.PLATFORM_LABEL[platform]
        name = f"LutronToDMXBridge-{version}-{os_name}.zip"
        data = zip_bytes if zip_bytes is not None else make_release_zip(platform, version)
        digest = __import__("hashlib").sha256(data).hexdigest()
        listing = sums if sums is not None else f"{digest}  {name}\n"
        prefix = f"/{REPO}/releases/download/v{version}"
        assets = []
        if with_zip:
            self.files[f"{prefix}/{name}"] = data
            assets.append({"name": name, "size": len(data), "url": f"{self.base}/repos/{REPO}/releases/assets/1",
                           "browser_download_url": f"{self.base}{prefix}/{name}"})
        if with_sums:
            self.files[f"{prefix}/SHA256SUMS.txt"] = listing.encode()
            assets.append({"name": "SHA256SUMS.txt", "size": len(listing), "url": f"{self.base}/repos/{REPO}/releases/assets/2",
                           "browser_download_url": f"{self.base}{prefix}/SHA256SUMS.txt"})
        self.release = {"tag_name": f"v{version}", "html_url": f"{self.base}/{REPO}/releases/tag/v{version}",
                        "published_at": "2026-11-02T00:00:00Z", "draft": False, "prerelease": False,
                        "assets": assets, **extra}
        return data


def make_release_zip(platform: str, version: str, marker: str = "new") -> bytes:
    import io

    top = upd.app_top_dir(platform)
    exe = upd.executable_in(Path(top), platform).as_posix()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(exe, f"{marker}-{version}")
        zf.writestr(f"{top}/data/readme.txt", "hello")
    return buf.getvalue()


@pytest.fixture
def gh():
    g = FakeGitHub()
    yield g
    g.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LUTRON_TO_DMX_BRIDGE_HOME", str(h))
    return h


def make_updater(gh, home, tmp_path, *, current="0.3.0", kind="windows-exe", can_auto=True, **kw) -> Updater:
    app = tmp_path / "apps" / "Lutron To DMX Bridge"
    if kind == "windows-exe" and not app.exists():
        app.mkdir(parents=True)
        (app / "Lutron To DMX Bridge.exe").write_text("old")
    install = InstallInfo(kind, "Windows app", can_auto, "do it by hand", app)
    kw.setdefault("smoke_test", False)
    return Updater(AppConfig(), repo=REPO, api_base=gh.base, web_base=gh.base, current=current, install=install,
                   platform=WIN, **kw)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [("v1.2.3", (1, 2, 3)), ("0.10.0", (0, 10, 0)), (" 2.0.1 ", (2, 0, 1)),
                                           ("1.2", None), ("latest", None), ("v1.2.3-rc1", (1, 2, 3)), ("", None)])
def test_parse_version(text, expected):
    assert upd.parse_version(text) == expected


def test_is_newer_compares_numbers_not_strings():
    assert upd.is_newer("0.10.0", "0.9.0")
    assert upd.is_newer("v1.0.0", "0.99.99")
    assert not upd.is_newer("0.3.0", "0.3.0")
    assert not upd.is_newer("0.2.9", "0.3.0")
    assert not upd.is_newer("nightly", "0.3.0")


def test_parse_sha256sums():
    h1, h2 = "a" * 64, "B" * 64
    text = f"{h1}  LutronToDMXBridge-1.0.0-macOS.zip\n{h2} *LutronToDMXBridge-1.0.0-Windows.zip\nnot a line\n"
    assert upd.parse_sha256sums(text) == {"LutronToDMXBridge-1.0.0-macOS.zip": h1, "LutronToDMXBridge-1.0.0-Windows.zip": h2.lower()}


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------
async def test_check_finds_a_newer_release(gh, home, tmp_path):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    rel = await u.check()
    assert rel and rel.version == "0.4.0" and rel.tag == "v0.4.0"
    state = u.public_state()
    assert state["available"] and state["latest"] == "0.4.0" and not state["dismissed"]
    assert state["notes_url"].endswith("/releases/tag/v0.4.0") and state["check_error"] is None
    path, headers = gh.requests[0]
    assert path == f"/repos/{REPO}/releases/latest"
    assert headers["user-agent"].startswith("LutronToDMXBridge/") and "authorization" not in headers


async def test_same_or_older_release_is_not_an_update(gh, home, tmp_path):
    gh.publish("0.3.0")
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert u.latest.version == "0.3.0" and not u.public_state()["available"]


async def test_private_repository_is_quiet(gh, home, tmp_path, caplog):
    gh.status = 404
    u = make_updater(gh, home, tmp_path)
    with caplog.at_level("INFO"):
        assert await u.check() is None
        assert await u.check() is None
    state = u.public_state()
    assert not state["available"] and "private" in state["check_error"]
    # the reason is logged once, not on every check
    assert len([r for r in caplog.records if r.name == "lutron_to_dmx_bridge.updater"]) == 1
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


async def test_token_is_sent_when_configured(gh, home, tmp_path):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    u.config.updates.token = "ghp_secret"
    await u.check()
    assert gh.requests[0][1]["authorization"] == "Bearer ghp_secret"


async def test_bad_token_message(gh, home, tmp_path):
    gh.status = 401
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert "token" in u.check_error and "401" in u.check_error


async def test_forbidden_without_rate_limit_headers_is_a_refusal(gh, home, tmp_path):
    gh.status = 403
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert "refused" in u.check_error and "403" in u.check_error and "rate limit" not in u.check_error


async def test_rate_limit_is_named(gh, home, tmp_path):
    gh.status = 403
    gh.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"}
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert "rate limit" in u.check_error
    gh.status, gh.headers = 429, {}
    await u.check()
    assert "rate limit" in u.check_error


async def test_server_error_names_the_status(gh, home, tmp_path):
    gh.status = 502
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert "502" in u.check_error and "offline" not in u.check_error


async def test_offline_is_quiet(home, tmp_path, caplog):
    u = Updater(AppConfig(), repo=REPO, api_base="http://127.0.0.1:9", web_base="http://127.0.0.1:9",
                current="0.3.0", install=InstallInfo("windows-exe", "x", True), platform=WIN)
    with caplog.at_level("INFO"):
        assert await u.check() is None
    assert "offline" in u.check_error and not u.public_state()["available"]
    # the underlying reason is in the log, not only the friendly sentence
    assert any("ConnectionRefusedError" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_network_error_tells_the_causes_apart():
    import socket
    import urllib.error

    tls = upd.network_error(urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")))
    assert tls.kind == "tls" and "certificate" in str(tls) and "offline" not in str(tls)
    assert "CERTIFICATE_VERIFY_FAILED" in tls.detail
    assert upd.network_error(socket.timeout("timed out")).kind == "timeout"
    assert upd.network_error(urllib.error.URLError(socket.timeout("timed out"))).kind == "timeout"
    dns = upd.network_error(urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")))
    assert dns.kind == "network" and "DNS" in str(dns)
    refused = upd.network_error(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
    assert refused.kind == "network" and "ConnectionRefusedError" in refused.detail


@pytest.fixture
def https_server(tmp_path):
    """A local HTTPS server with a self-signed certificate: nothing in any trust store vouches for it."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # pragma: no cover - never reached: the handshake fails first
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_certificate_failure_is_reported_as_tls_and_logged(https_server, home, caplog):
    """The packaged app once had no CA certificates, and every HTTPS failure was reported as 'offline'."""
    u = Updater(AppConfig(), repo=REPO, api_base=https_server, web_base=https_server, current="0.3.0",
                install=InstallInfo("windows-exe", "x", True), platform=WIN)
    assert u._strict
    u._url_ok = lambda url: True  # strict mode only allows github.com; here the real TLS stack talks to a local server
    with caplog.at_level("INFO"):
        assert await u.check() is None
    assert "offline" not in u.check_error and "certificate" in u.check_error
    warn = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warn and "CERTIFICATE_VERIFY_FAILED" in warn[0].getMessage()


def test_tls_context_includes_the_bundled_ca_certificates(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    assert upd._tls_context().cert_store_stats()["x509_ca"] > 0


async def test_prerelease_and_junk_tags_are_ignored(gh, home, tmp_path):
    gh.publish("0.4.0", prerelease=True)
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert u.latest is None and "stable" in u.check_error
    gh.publish("0.4.0")
    gh.release["tag_name"] = "nightly"
    await u.check()
    assert u.latest is None and "version number" in u.check_error


async def test_release_page_link_must_stay_on_the_repository(gh, home, tmp_path):
    gh.publish("0.4.0")
    gh.release["html_url"] = "https://evil.example/phish"
    u = make_updater(gh, home, tmp_path)
    await u.check()
    assert u.latest.url == f"{gh.base}/{REPO}/releases/tag/v0.4.0"


async def test_dismiss_is_per_version(gh, home, tmp_path):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    await u.check()
    u.dismiss("0.4.0")
    assert u.public_state()["dismissed"]
    assert AppConfig.load().updates.dismissed_version == "0.4.0"
    gh.publish("0.4.1")
    await u.check()
    assert u.public_state()["latest"] == "0.4.1" and not u.public_state()["dismissed"]


def test_automatic_checks_can_be_turned_off(home, monkeypatch):
    monkeypatch.delenv(upd.NO_CHECK_ENV)
    cfg = AppConfig()
    u = Updater(cfg, install=InstallInfo("source", "x", False))
    assert u.auto_check
    cfg.updates.check = False
    assert not u.auto_check
    cfg.updates.check = True
    monkeypatch.setenv(upd.NO_CHECK_ENV, "1")
    assert not u.auto_check


async def test_background_loop_skips_checks_when_disabled(gh, home, tmp_path):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path, first_check_delay=0, check_interval=0.05)
    u.config.updates.check = False
    task = asyncio.create_task(u.run())
    await asyncio.sleep(0.3)
    task.cancel()
    await task
    assert gh.requests == []


async def test_background_loop_checks_when_enabled(gh, home, tmp_path, monkeypatch):
    monkeypatch.delenv(upd.NO_CHECK_ENV)
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path, first_check_delay=0, check_interval=0.05)
    task = asyncio.create_task(u.run())
    end = asyncio.get_running_loop().time() + 15  # a slow CI machine needs longer than a fixed sleep
    while len(gh.requests) < 2 and asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.05)
    task.cancel()
    await task
    assert u.public_state()["available"] and len(gh.requests) >= 2


# ---------------------------------------------------------------------------
# network guards
# ---------------------------------------------------------------------------
def test_redirects_never_leave_github_or_carry_the_token(home):
    u = Updater(AppConfig(), install=InstallInfo("source", "x", False))  # strict (real GitHub) rules
    handler = upd._GuardedRedirects(u._url_ok)
    req = urllib.request.Request("https://api.github.com/repos/x/y/releases/assets/1",
                                 headers={"Authorization": "Bearer t", "Accept": "application/octet-stream"})
    new = handler.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/blob?sig=1")
    assert new is not None and "Authorization" not in {k.title() for k in new.headers} | set(new.unredirected_hdrs)
    for bad in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "file:///etc/passwd"):
        with pytest.raises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, bad)


def test_asset_urls_must_be_this_repositorys_release_downloads(home, tmp_path):
    u = Updater(AppConfig(), install=InstallInfo("source", "x", False))
    ok = f"https://github.com/{upd.REPO}/releases/download/v1/a.zip"
    assert u._asset_source({"api_url": "", "download_url": ok}) == ok
    for bad in ("https://github.com/someone/else/releases/download/v1/a.zip", "https://evil.example/a.zip",
                f"http://github.com/{upd.REPO}/releases/download/v1/a.zip", ""):
        with pytest.raises(UpdateError):
            u._asset_source({"api_url": "", "download_url": bad})
    u.config.updates.token = "t"  # with a token the API URL is used (private repositories)
    api = f"https://api.github.com/repos/{upd.REPO}/releases/assets/9"
    assert u._asset_source({"api_url": api, "download_url": ok}) == api


# ---------------------------------------------------------------------------
# download + verify
# ---------------------------------------------------------------------------
async def test_download_verifies_checksum(gh, home, tmp_path):
    data = gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    rel = await u.check()
    work = tmp_path / "work"
    work.mkdir()
    path = await asyncio.to_thread(u._download_verified, rel, work)
    assert path.read_bytes() == data and not list(work.glob("*.part"))


async def test_corrupted_download_is_rejected(gh, home, tmp_path):
    gh.publish("0.4.0", sums=f"{'0' * 64}  LutronToDMXBridge-0.4.0-Windows.zip\n")
    u = make_updater(gh, home, tmp_path)
    rel = await u.check()
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(UpdateError, match="checksum"):
        await asyncio.to_thread(u._download_verified, rel, work)
    assert not list(work.iterdir())


@pytest.mark.parametrize("kwargs,message", [
    ({"with_sums": False}, "SHA256SUMS.txt"),
    ({"with_zip": False}, "Windows.zip"),
    ({"sums": f"{'a' * 64}  something-else.zip\n"}, "does not list"),
])
async def test_incomplete_release_is_not_installed(gh, home, tmp_path, kwargs, message):
    gh.publish("0.4.0", **kwargs)
    u = make_updater(gh, home, tmp_path)
    rel = await u.check()
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(UpdateError, match=message):
        await asyncio.to_thread(u._download_verified, rel, work)


async def test_download_from_outside_the_repository_is_refused(gh, home, tmp_path):
    gh.publish("0.4.0")
    gh.release["assets"][0]["browser_download_url"] = "http://elsewhere.example/a.zip"
    u = make_updater(gh, home, tmp_path)
    rel = await u.check()
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(UpdateError, match="outside"):
        await asyncio.to_thread(u._download_verified, rel, work)


# ---------------------------------------------------------------------------
# unpacking
# ---------------------------------------------------------------------------
def test_unpack_release(tmp_path):
    z = tmp_path / "r.zip"
    z.write_bytes(make_release_zip(WIN, "0.4.0"))
    staged = upd.unpack_release(z, tmp_path / "out", WIN)
    assert staged.name == "Lutron To DMX Bridge" and (staged / "Lutron To DMX Bridge.exe").read_text() == "new-0.4.0"
    assert (staged / "data" / "readme.txt").exists()


@pytest.mark.parametrize("names,message", [
    (["Lutron To DMX Bridge/Lutron To DMX Bridge.exe", "Lutron To DMX Bridge/../../evil.txt"], "unexpected file"),
    (["/etc/passwd", "Lutron To DMX Bridge/Lutron To DMX Bridge.exe"], "unexpected file"),
    (["Other/Lutron To DMX Bridge.exe"], "unexpected file"),
    (["Lutron To DMX Bridge/readme.txt"], "missing"),
])
def test_unpack_rejects_odd_archives(tmp_path, names, message):
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    with pytest.raises(UpdateError, match=message):
        upd.unpack_release(z, tmp_path / "out", WIN)
    assert not (tmp_path / "evil.txt").exists()


def test_unpack_accepts_the_resource_fork_folder_ditto_adds(tmp_path):
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Lutron To DMX Bridge/Lutron To DMX Bridge.exe", "x")
        zf.writestr("__MACOSX/Lutron To DMX Bridge/._Lutron To DMX Bridge.exe", "x")
    assert upd.unpack_release(z, tmp_path / "out", WIN).name == "Lutron To DMX Bridge"


def test_unpack_rejects_a_non_zip(tmp_path):
    z = tmp_path / "r.zip"
    z.write_text("nope")
    with pytest.raises(UpdateError, match="not a valid zip"):
        upd.unpack_release(z, tmp_path / "out", WIN)


# ---------------------------------------------------------------------------
# the new copy's own start-up check
# ---------------------------------------------------------------------------
def fake_exe(path: Path, script: str) -> Path:
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as the fake app")
class TestSelfCheck:
    def test_pass(self, tmp_path):
        exe = fake_exe(tmp_path / "app", 'printf "Lutron To DMX Bridge 0.4.0 on Python 3.12\\nok    server\\nPASS\\n" > "$2"\n')
        upd.run_self_check(exe, "0.4.0", tmp_path)

    def test_wrong_version(self, tmp_path):
        exe = fake_exe(tmp_path / "app", 'printf "Lutron To DMX Bridge 0.3.0 on Python\\nPASS\\n" > "$2"\n')
        with pytest.raises(UpdateError, match="failed its start-up check"):
            upd.run_self_check(exe, "0.4.0", tmp_path)

    def test_failed_check_reports_why(self, tmp_path):
        exe = fake_exe(tmp_path / "app", 'printf "Lutron To DMX Bridge 0.4.0 on Python\\nFAIL  tkinter import: boom\\nFAILED\\n" > "$2"\nexit 1\n')
        with pytest.raises(UpdateError, match="tkinter import: boom"):
            upd.run_self_check(exe, "0.4.0", tmp_path)

    def test_no_report(self, tmp_path):
        with pytest.raises(UpdateError, match="no report"):
            upd.run_self_check(fake_exe(tmp_path / "app", "exit 0\n"), "0.4.0", tmp_path)

    def test_hang(self, tmp_path):
        with pytest.raises(UpdateError, match="in time"):
            upd.run_self_check(fake_exe(tmp_path / "app", "sleep 5\n"), "0.4.0", tmp_path, timeout=0.5)

    def test_cannot_start(self, tmp_path):
        with pytest.raises(UpdateError, match="could not be started"):
            upd.run_self_check(tmp_path / "missing", "0.4.0", tmp_path)


# ---------------------------------------------------------------------------
# applying (app on Windows layout; the helper is stubbed)
# ---------------------------------------------------------------------------
@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(upd, "spawn_helper", lambda exe, plan, platform: calls.append((exe, plan, platform)))
    monkeypatch.setattr(upd, "EXIT_DELAY_S", 0.0)
    return calls


async def wait_phase(u: Updater, *phases: str, timeout: float = 10.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while u.phase not in phases:
        assert asyncio.get_running_loop().time() < end, f"stuck in {u.phase}: {u.message}"
        await asyncio.sleep(0.02)


async def test_apply_downloads_stages_and_hands_over(gh, home, tmp_path, spawned):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    exits = []
    u.request_exit = lambda: exits.append(1)
    await u.check()
    assert await u.apply("0.4.0", [], False) == {"started": True}
    await wait_phase(u, "restarting", "error")
    assert u.phase == "restarting", u.message
    await asyncio.sleep(0.1)
    assert exits == [1]

    (exe, plan_path, platform), = spawned
    staged_exe = Path(exe)
    assert staged_exe.read_text() == "new-0.4.0" and platform == WIN
    plan = json.loads(Path(plan_path).read_text())
    assert plan["pid"] == os.getpid() and plan["to_version"] == "0.4.0" and plan["from_version"] == "0.3.0"
    assert plan["target"] == str(u.install.target) and plan["staged"] == str(staged_exe.parent)
    assert plan["backup"].endswith(str(Path("previous") / "0.3.0" / "Lutron To DMX Bridge"))
    assert plan["relaunch"] == [str(u.install.target / "Lutron To DMX Bridge.exe"), "--no-browser"]
    # the running app was not touched yet, and the settings folder only gained the updates folder
    assert (u.install.target / "Lutron To DMX Bridge.exe").read_text() == "old"
    assert not (home / "config.json").exists()


async def test_apply_asks_before_interrupting_a_show(gh, home, tmp_path, spawned):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    await u.check()
    res = await u.apply("0.4.0", ["Performance mode is active."], False)
    assert res == {"needs_confirm": True, "reasons": ["Performance mode is active."]}
    assert u.phase == "idle" and not spawned
    assert (await u.apply("0.4.0", ["Performance mode is active."], True)) == {"started": True}
    await wait_phase(u, "restarting", "error")
    assert u.phase == "restarting"


async def test_apply_refuses_a_version_that_is_not_the_latest(gh, home, tmp_path, spawned):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    await u.check()
    with pytest.raises(UpdateError, match="not the latest"):
        await u.apply("9.9.9", [], True)
    with pytest.raises(UpdateError, match="not the latest"):
        await make_updater(gh, home, tmp_path).apply("0.4.0", [], True)  # nothing checked yet


async def test_apply_refuses_when_the_install_cannot_update_itself(gh, home, tmp_path, spawned):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path, can_auto=False)
    await u.check()
    with pytest.raises(UpdateError, match="by hand"):
        await u.apply("0.4.0", [], True)


async def test_bad_checksum_leaves_everything_alone(gh, home, tmp_path, spawned):
    gh.publish("0.4.0", sums=f"{'0' * 64}  LutronToDMXBridge-0.4.0-Windows.zip\n")
    u = make_updater(gh, home, tmp_path)
    exits = []
    u.request_exit = lambda: exits.append(1)
    await u.check()
    await u.apply("0.4.0", [], True)
    await wait_phase(u, "error", "restarting")
    assert u.phase == "error" and "checksum" in u.message
    assert not spawned and not exits and not u._busy
    assert (u.install.target / "Lutron To DMX Bridge.exe").read_text() == "old"
    # and it can be tried again
    gh.publish("0.4.0")
    await u.apply("0.4.0", [], True)
    await wait_phase(u, "restarting", "error")
    assert u.phase == "restarting"


async def test_second_apply_while_busy_is_refused(gh, home, tmp_path, spawned):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    await u.check()
    await u.apply("0.4.0", [], True)
    with pytest.raises(UpdateError, match="already in progress"):
        await u.apply("0.4.0", [], True)
    await wait_phase(u, "restarting", "error")


async def test_failed_self_check_blocks_the_update(gh, home, tmp_path, spawned, monkeypatch):
    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path, smoke_test=True)

    def boom(*a, **k):
        raise UpdateError("The new version failed its start-up check, so it was not installed.")

    monkeypatch.setattr(upd, "run_self_check", boom)
    await u.check()
    await u.apply("0.4.0", [], True)
    await wait_phase(u, "error", "restarting")
    assert u.phase == "error" and not spawned


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------
def make_plan(tmp_path, pid=None, *, staged_exe=True) -> dict:
    target = tmp_path / "apps" / "Lutron To DMX Bridge"
    target.mkdir(parents=True, exist_ok=True)
    (target / "Lutron To DMX Bridge.exe").write_text("old")
    (target / "keep.txt").write_text("old data")
    staged = tmp_path / "staging" / "Lutron To DMX Bridge"
    staged.mkdir(parents=True)
    if staged_exe:
        (staged / "Lutron To DMX Bridge.exe").write_text("new")
        (staged / "extra.txt").write_text("new data")
    if pid is None:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        pid = p.pid  # a process that has already exited
    plan = {"pid": pid, "platform": WIN, "from_version": "0.3.0", "to_version": "0.4.0", "target": str(target),
            "staged": str(staged), "backup": str(tmp_path / "updates" / "previous" / "0.3.0" / "Lutron To DMX Bridge"),
            "result": str(tmp_path / "update-result.json"), "log": str(tmp_path / "logs" / "update.log"),
            "relaunch": ["relaunch", "--no-browser"], "wait_s": 5}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    plan["_path"] = str(path)
    return plan


def test_helper_swaps_in_the_new_version_and_keeps_the_old_one(tmp_path):
    plan = make_plan(tmp_path)
    launched = []
    assert upd.run_helper(plan["_path"], launch=launched.append) == 0
    target = Path(plan["target"])
    assert (target / "Lutron To DMX Bridge.exe").read_text() == "new" and (target / "extra.txt").exists()
    assert not (target / "keep.txt").exists()
    backup = Path(plan["backup"])
    assert (backup / "Lutron To DMX Bridge.exe").read_text() == "old" and (backup / "keep.txt").exists()
    assert launched == [["relaunch", "--no-browser"]]
    result = json.loads(Path(plan["result"]).read_text())
    assert result["ok"] and result["from_version"] == "0.3.0" and result["to_version"] == "0.4.0"
    assert Path(plan["log"]).exists()


def test_helper_rolls_back_when_the_copy_fails(tmp_path, monkeypatch):
    plan = make_plan(tmp_path)

    def broken_copy(src, dst, platform):
        dst.mkdir()
        (dst / "half.txt").write_text("partial")
        raise OSError("disk full")

    monkeypatch.setattr(upd, "_copy_tree", broken_copy)
    launched = []
    assert upd.run_helper(plan["_path"], launch=launched.append) == 1
    target = Path(plan["target"])
    assert (target / "Lutron To DMX Bridge.exe").read_text() == "old" and (target / "keep.txt").exists()
    assert not (target / "half.txt").exists()
    assert launched == [["relaunch", "--no-browser"]]  # the restored old app is started again
    result = json.loads(Path(plan["result"]).read_text())
    assert not result["ok"] and "disk full" in result["error"]


def test_helper_rejects_a_staged_copy_without_an_executable(tmp_path):
    plan = make_plan(tmp_path, staged_exe=False)
    launched = []
    assert upd.run_helper(plan["_path"], launch=launched.append) == 1
    assert (Path(plan["target"]) / "Lutron To DMX Bridge.exe").read_text() == "old"
    assert not json.loads(Path(plan["result"]).read_text())["ok"]
    assert launched


def test_helper_does_nothing_if_the_old_app_never_quits(tmp_path):
    plan = make_plan(tmp_path, pid=os.getpid())
    data = json.loads(Path(plan["_path"]).read_text())
    data["wait_s"] = 0.3
    Path(plan["_path"]).write_text(json.dumps(data))
    launched = []
    assert upd.run_helper(plan["_path"], launch=launched.append) == 1
    assert (Path(plan["target"]) / "Lutron To DMX Bridge.exe").read_text() == "old"
    assert not launched and not Path(plan["backup"]).exists()
    assert "did not close" in json.loads(Path(plan["result"]).read_text())["error"]


def test_only_one_previous_version_is_kept(tmp_path):
    plan = make_plan(tmp_path)
    old_backup = Path(plan["backup"]).parent.parent / "0.2.0" / "Lutron To DMX Bridge"
    old_backup.mkdir(parents=True)
    (old_backup / "x").write_text("ancient")
    assert upd.run_helper(plan["_path"], launch=lambda argv: None) == 0
    assert [p.name for p in Path(plan["backup"]).parent.parent.iterdir()] == ["0.3.0"]


def test_helper_is_reachable_through_the_launcher(tmp_path, monkeypatch):
    from lutron_to_dmx_bridge import launcher

    seen = []
    monkeypatch.setattr(upd, "run_helper", lambda p: seen.append(p) or 0)
    assert launcher.main(["--apply-update", "plan.json"]) == 0 and seen == ["plan.json"]


def test_pid_exited(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not upd.pid_exited(p.pid, 0.3)
    finally:
        p.kill()
        p.wait()
    assert upd.pid_exited(p.pid, 1)


def test_result_is_reported_once_after_restart(home, caplog):
    (home / upd.RESULT_NAME).write_text(json.dumps({"ok": True, "from_version": "0.3.0", "to_version": "0.4.0", "at": "t"}))
    with caplog.at_level("INFO"):
        u = Updater(AppConfig(), install=InstallInfo("source", "x", False))
    assert u.last_result["ok"] and "from 0.3.0 to 0.4.0" in caplog.text
    assert not (home / upd.RESULT_NAME).exists()
    assert Updater(AppConfig(), install=InstallInfo("source", "x", False)).last_result is None

    (home / upd.RESULT_NAME).write_text(json.dumps({"ok": False, "from_version": "0.3.0", "to_version": "0.4.0",
                                                    "error": "disk full", "at": "t2"}))
    with caplog.at_level("INFO"):
        u = Updater(AppConfig(), install=InstallInfo("source", "x", False))
    assert u.public_state()["last_result"]["error"] == "disk full"
    assert any(r.levelname == "ERROR" and "disk full" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# which kind of install is this?
# ---------------------------------------------------------------------------
def test_detect_macos_app(tmp_path):
    app = tmp_path / "Applications" / "Lutron To DMX Bridge.app"
    exe = app / "Contents" / "MacOS" / "Lutron To DMX Bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    info = upd.detect_install(str(exe), frozen=True, platform="darwin")
    assert info.kind == "macos-app" and info.can_auto and info.target == app


def test_detect_macos_translocated_app_asks_to_move_it(tmp_path):
    app = tmp_path / "AppTranslocation" / "ABC" / "d" / "Lutron To DMX Bridge.app"
    exe = app / "Contents" / "MacOS" / "Lutron To DMX Bridge"
    exe.parent.mkdir(parents=True)
    info = upd.detect_install(str(exe), frozen=True, platform="darwin")
    assert info.kind == "macos-app" and not info.can_auto and "Applications" in info.manual


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="root can write anywhere")
def test_detect_unwritable_install_falls_back_to_instructions(tmp_path):
    folder = tmp_path / "ro" / "Lutron To DMX Bridge"
    folder.mkdir(parents=True)
    (tmp_path / "ro").chmod(0o555)
    try:
        info = upd.detect_install(str(folder / "Lutron To DMX Bridge.exe"), frozen=True, platform="win32")
    finally:
        (tmp_path / "ro").chmod(0o755)
    assert info.kind == "windows-exe" and not info.can_auto and "by hand" in info.manual


def test_detect_windows_app(tmp_path):
    folder = tmp_path / "Lutron To DMX Bridge"
    folder.mkdir()
    info = upd.detect_install(str(folder / "Lutron To DMX Bridge.exe"), frozen=True, platform="win32")
    assert info.kind == "windows-exe" and info.can_auto and info.target == folder


def test_detect_git_checkout_and_plain_source(tmp_path):
    (tmp_path / ".git").mkdir()
    assert upd.detect_install(frozen=False, platform="linux", root=tmp_path).kind == "git"
    plain = tmp_path / "plain"
    plain.mkdir()
    info = upd.detect_install(frozen=False, platform="linux", root=plain)
    assert info.kind == "source" and not info.can_auto and info.manual


# ---------------------------------------------------------------------------
# developer checkout
# ---------------------------------------------------------------------------
def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A clone of a fake "origin" whose URL contains the repository name, like the real one."""
    origin = tmp_path / "owner" / "repo.git"
    origin.parent.mkdir()
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    (seed / "lutron_to_dmx_bridge").mkdir()
    (seed / "lutron_to_dmx_bridge" / "__init__.py").write_text('__version__ = "0.3.0"\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "v0.3.0")
    git(seed, "push", "origin", "HEAD:main")
    root = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(root))
    (seed / "lutron_to_dmx_bridge" / "__init__.py").write_text('__version__ = "0.4.0"\n')
    git(seed, "commit", "-am", "Release v0.4.0")
    git(seed, "tag", "v0.4.0")
    git(seed, "push", "origin", "HEAD:main", "v0.4.0")
    return root


def git_updater(checkout, gh, home, monkeypatch):
    gh.publish("0.4.0")
    u = Updater(AppConfig(), repo="owner/repo", api_base=gh.base, web_base=gh.base, current="0.3.0",
                install=InstallInfo("git", "git checkout", True, "", checkout), platform="linux")
    # (the fake release server's repository path differs; the check itself is tested above)
    u.latest = upd.ReleaseInfo("0.4.0", "v0.4.0", "https://example.invalid", "")
    installs = []
    monkeypatch.setattr(Updater, "_pip_install", staticmethod(lambda root: installs.append(root)))
    return u, installs


async def test_git_checkout_is_fast_forwarded_to_the_release(checkout, gh, home, monkeypatch):
    u, installs = git_updater(checkout, gh, home, monkeypatch)
    assert await u.apply("0.4.0", [], True) == {"started": True}
    await wait_phase(u, "installed", "error")
    assert u.phase == "installed", u.message
    assert 'version__ = "0.4.0"' in (checkout / "lutron_to_dmx_bridge" / "__init__.py").read_text()
    assert installs == [checkout]


@pytest.mark.parametrize("prepare,message", [
    (lambda c: (c / "stray.txt").write_text("x") or git(c, "add", "stray.txt"), "uncommitted"),
    (lambda c: git(c, "checkout", "-b", "feature"), "not main"),
    (lambda c: git(c, "remote", "set-url", "origin", "https://github.com/someone/else.git"), "is not owner/repo"),
])
async def test_git_checkout_is_left_alone_when_unsafe(checkout, gh, home, monkeypatch, prepare, message):
    u, installs = git_updater(checkout, gh, home, monkeypatch)
    prepare(checkout)
    await u.apply("0.4.0", [], True)
    await wait_phase(u, "installed", "error")
    assert u.phase == "error" and message in u.message and not installs
    assert 'version__ = "0.3.0"' in (checkout / "lutron_to_dmx_bridge" / "__init__.py").read_text()


async def test_git_checkout_with_diverged_history_is_not_merged(checkout, gh, home, monkeypatch):
    u, installs = git_updater(checkout, gh, home, monkeypatch)
    (checkout / "local.txt").write_text("mine")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "local work")
    await u.apply("0.4.0", [], True)
    await wait_phase(u, "installed", "error")
    assert u.phase == "error" and not installs


# ---------------------------------------------------------------------------
# the web API
# ---------------------------------------------------------------------------
@pytest.fixture
def web(gh, home, tmp_path, spawned):
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.web.app import create_app
    from tests.test_web import FakeDriver

    gh.publish("0.4.0")
    cfg = AppConfig(driver=DriverConfig(kind="ra2", host="10.0.0.5"))
    u = make_updater(gh, home, tmp_path)
    u.config = cfg
    ctl = Controller(cfg, driver=FakeDriver(cfg.driver))
    with TestClient(create_app(ctl, updater=u)) as client:
        yield client, ctl, u


def test_status_carries_the_update_state(web):
    client, _, u = web
    assert client.get("/api/status").json()["update"]["available"] is False  # nothing checked yet
    assert client.post("/api/update/check").json()["latest"] == "0.4.0"
    st = client.get("/api/status").json()["update"]
    assert st["available"] and st["latest"] == "0.4.0" and st["install"]["can_auto"] and st["warnings"] == []


def test_dismiss_and_settings_endpoints(web):
    client, ctl, _ = web
    client.post("/api/update/check")
    assert client.post("/api/update/dismiss", json={"version": "0.4.0"}).json()["dismissed"]
    assert AppConfig.load().updates.dismissed_version == "0.4.0"

    assert client.get("/api/settings/updates").json() == {"check": True, "token": ""}
    res = client.put("/api/settings/updates", json={"check": False, "token": "ghp_abc"}).json()
    assert res["check"] is False and res["token"] == "********"  # never sent back
    assert ctl.config.updates.token == "ghp_abc" and AppConfig.load().updates.token == "ghp_abc"
    client.put("/api/settings/updates", json={"check": False, "token": "********"})  # placeholder keeps the token
    assert AppConfig.load().updates.token == "ghp_abc"
    client.put("/api/settings/updates", json={"check": True, "token": ""})  # empty clears it
    assert AppConfig.load().updates.token == ""


def test_apply_endpoint_asks_when_a_schedule_set_is_active(web, spawned):
    client, ctl, u = web
    client.post("/api/update/check")
    from lutron_to_dmx_bridge.config import ActiveScheduleSet

    ctl.config.active_schedule_set = ActiveScheduleSet(set_id="x", set_name="Performance mode", activated_at="t")
    st = client.get("/api/status").json()["update"]
    assert "Performance mode" in st["warnings"][0]
    res = client.post("/api/update/apply", json={"version": "0.4.0"}).json()
    assert res["needs_confirm"] and "Performance mode" in res["reasons"][0]
    assert u.phase == "idle" and not spawned
    res = client.post("/api/update/apply", json={"version": "0.4.0", "confirm": True}).json()
    assert res["started"] is True


def test_apply_endpoint_reports_a_refusal_as_409(web):
    client, _, _ = web
    r = client.post("/api/update/apply", json={"version": "0.4.0"})  # not checked yet
    assert r.status_code == 409 and "not the latest" in r.json()["detail"]


def test_without_an_updater_the_app_stays_offline(home):
    from lutron_to_dmx_bridge.controller import Controller
    from lutron_to_dmx_bridge.web.app import create_app
    from tests.test_web import FakeDriver

    cfg = AppConfig()
    with TestClient(create_app(Controller(cfg, driver=FakeDriver()))) as client:
        assert client.get("/api/status").json()["update"] == {"auto_check": False, "available": False, "disabled": True}
        assert client.get("/api/update").status_code == 404
        assert client.post("/api/update/check").status_code == 404


def test_the_gui_has_the_banner_and_settings(web):
    client, _, _ = web
    html = client.get("/").text
    for el in ("update-banner", "btn-update-now", "btn-update-dismiss", "btn-update-check", "upd-check", "upd-token",
               "btn-update-settings-now", "update-action-why"):
        assert f'id="{el}"' in html
    js = client.get("/static/app.js").text
    assert "renderUpdate" in js and "renderUpdateAction" in js and "update/apply" in js


# ---------------------------------------------------------------------------
# the log window notice
# ---------------------------------------------------------------------------
async def test_log_window_notice(gh, home, tmp_path):
    from lutron_to_dmx_bridge.launcher import ServerThread, bind_socket

    gh.publish("0.4.0")
    u = make_updater(gh, home, tmp_path)
    sock = bind_socket("127.0.0.1", 0)
    thread = ServerThread(AppConfig(), sock, updater=u)
    try:
        assert thread.update_notice() == ""
        await u.check()
        assert "0.4.0 available" in thread.update_notice()
        u.dismiss("0.4.0")
        assert thread.update_notice() == ""
        u.phase, u.message = "downloading", "Downloading…"
        assert thread.update_notice().startswith("Updating")
        thread.exiting_for_update = True
        assert "reopen" in thread.update_notice()
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# platform details
# ---------------------------------------------------------------------------
def test_platform_layout_and_relaunch_commands():
    app = Path("/Applications/Lutron To DMX Bridge.app")
    assert upd.executable_in(app, "darwin") == app / "Contents" / "MacOS" / "Lutron To DMX Bridge"
    assert upd.relaunch_command(app, "darwin") == ["open", str(app), "--args", "--no-browser"]
    folder = Path("C:/Apps/Lutron To DMX Bridge")
    assert upd.executable_in(folder, "win32") == folder / "Lutron To DMX Bridge.exe"
    assert upd.relaunch_command(folder, "win32") == [str(folder / "Lutron To DMX Bridge.exe"), "--no-browser"]
    assert upd.app_top_dir("darwin") == "Lutron To DMX Bridge.app" and upd.app_top_dir("win32") == "Lutron To DMX Bridge"


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS's ditto")
def test_unpack_keeps_executable_bits_and_symlinks_on_macos(tmp_path):
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("Lutron To DMX Bridge.app/Contents/MacOS/Lutron To DMX Bridge")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, "#!/bin/sh\n")
        link = zipfile.ZipInfo("Lutron To DMX Bridge.app/Contents/Frameworks/lib.dylib")
        link.external_attr = (stat.S_IFLNK | 0o755) << 16
        zf.writestr(link, "../Resources/real.dylib")
        zf.writestr("Lutron To DMX Bridge.app/Contents/Resources/real.dylib", "x")
    staged = upd.unpack_release(z, tmp_path / "out", "darwin")
    assert os.access(upd.executable_in(staged, "darwin"), os.X_OK)
    assert (staged / "Contents" / "Frameworks" / "lib.dylib").is_symlink()
