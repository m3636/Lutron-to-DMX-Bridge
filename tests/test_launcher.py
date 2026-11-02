"""Tests for the double-click launcher: server thread, single instance, logging, and the log window."""

from __future__ import annotations

import http.server
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time

import pytest

from lutron_dmx import launcher
from lutron_dmx.config import AppConfig


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUTRON_DMX_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    """A real error dialog would block the test run forever; record instead."""
    shown = []
    monkeypatch.setattr(launcher, "show_error", lambda msg, *a, **k: shown.append(msg))
    return shown


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield root
    root.setLevel(level)
    root.handlers[:] = handlers


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server():
    """A real launcher server thread on a spare port, driving the simulator."""
    config = AppConfig()
    config.driver.kind = "simulator"
    thread = launcher.ServerThread(config, launcher.bind_socket("127.0.0.1", 0))
    thread.start()
    assert thread.ready.wait(30) and thread.error is None
    yield thread
    assert thread.stop()


@pytest.fixture
def other_server():
    """Something on a port that is not Lutron DMX (answers HTTP, but not our status JSON)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"hello": "world"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


# -- server thread ---------------------------------------------------------
def test_server_thread_serves_gui_and_stops_cleanly(running_server):
    import urllib.request

    port = running_server.port
    assert launcher.probe(port) == "ours"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
        status = json.loads(r.read())
    assert status["connected"] is True
    assert running_server.status()[0] == "warn"  # simulator: demo mode
    assert "Demo mode" in running_server.status()[1]
    running_server.request_stop()
    assert running_server.stopped.wait(10)
    assert launcher.probe(port) == "free"
    assert running_server.error is None
    assert running_server.status() == ("info", "Stopped")


def test_status_reports_controller_not_connected(running_server):
    running_server.config.driver.kind = "ra2"
    running_server.controller.last_error = "timed out"
    level, text = running_server.status()
    assert level == "warn" and "timed out" in text


def test_server_thread_reports_startup_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("lutron_dmx.web.app.create_app", boom)
    thread = launcher.ServerThread(AppConfig(), launcher.bind_socket("127.0.0.1", 0))
    thread.start()
    assert thread.stopped.wait(10)
    assert isinstance(thread.error, RuntimeError)
    assert thread.status()[0] == "error" and "kaboom" in thread.status()[1]


# -- ports / single instance -----------------------------------------------
def test_probe_distinguishes_free_ours_and_other(running_server, other_server):
    assert launcher.probe(running_server.port) == "ours"
    assert launcher.probe(other_server) == "other"
    assert launcher.probe(free_port()) == "free"


def test_resolve_port_free_ours_and_fallback(running_server, other_server):
    assert launcher.resolve_port(running_server.port) == (running_server.port, True)
    port = free_port()
    assert launcher.resolve_port(port) == (port, False)
    fallback, running = launcher.resolve_port(other_server)
    assert not running and fallback != other_server and fallback in launcher.FALLBACK_PORTS


def test_resolve_port_finds_our_copy_on_a_fallback_port(monkeypatch):
    seen = {8080: "other", 8081: "other", 8082: "ours"}
    monkeypatch.setattr(launcher, "probe", lambda port, host="127.0.0.1", timeout=1.5: seen.get(port, "free"))
    assert launcher.resolve_port(8080) == (8082, True)


def test_resolve_port_gives_up_when_everything_is_taken(monkeypatch):
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: "other")
    with pytest.raises(launcher.LauncherError):
        launcher.resolve_port(8080)


def test_bind_socket_fails_cleanly_when_port_is_taken():
    sock = launcher.bind_socket("127.0.0.1", 0)
    sock.listen()
    try:
        with pytest.raises(OSError):
            launcher.bind_socket("127.0.0.1", sock.getsockname()[1])
    finally:
        sock.close()


def test_second_launch_opens_the_running_instance_instead(running_server, monkeypatch):
    opened = []
    monkeypatch.setattr(launcher, "open_browser", opened.append)
    started = []
    monkeypatch.setattr(launcher, "ServerThread", lambda *a, **k: started.append(a))
    assert launcher.main(["--port", str(running_server.port)]) == 0
    assert opened == [f"http://127.0.0.1:{running_server.port}/"]
    assert not started


def test_launch_without_window_runs_until_stopped(monkeypatch):
    port = free_port()
    opened = []
    monkeypatch.setattr(launcher, "open_browser", opened.append)
    result = []
    t = threading.Thread(target=lambda: result.append(launcher.main(["--no-window", "--simulator", "--port", str(port)])),
                         daemon=True)
    t.start()
    deadline = time.time() + 30
    while launcher.probe(port) != "ours" and time.time() < deadline:
        time.sleep(0.1)
    assert launcher.probe(port) == "ours"
    # A second launch while the first runs must not start another server.
    assert launcher.main(["--no-window", "--port", str(port)]) == 0
    assert opened.count(f"http://127.0.0.1:{port}/") >= 1
    # No way to send Ctrl+C to a thread; ask the server to stop the way the window's Quit does.
    for th in threading.enumerate():
        if isinstance(th, launcher.ServerThread):
            th.request_stop()
    t.join(15)
    assert result == [0]
    assert launcher.probe(port) == "free"


# -- first run ---------------------------------------------------------------
def test_needs_setup_and_start_url(home):
    config = AppConfig()
    assert launcher.needs_setup(config)
    assert launcher.start_url("http://x/", config) == "http://x/#settings"
    config.save()
    assert not launcher.needs_setup(AppConfig.load())
    assert launcher.start_url("http://x/", AppConfig.load()) == "http://x/"
    config.driver.kind, config.driver.host = "ra2", ""
    assert launcher.needs_setup(config)
    config.driver.host = "10.0.0.5"
    assert not launcher.needs_setup(config)


def test_corrupt_config_shows_an_error_instead_of_crashing(home, no_dialogs):
    (home / "config.json").write_text("{not json", encoding="utf-8")
    shown = no_dialogs
    assert launcher.main(["--no-window"]) == 1
    assert shown and "config.json" in shown[0]


def test_browse_host():
    assert launcher.browse_host("0.0.0.0") == "127.0.0.1"
    assert launcher.browse_host("192.168.1.5") == "192.168.1.5"


# -- logging ---------------------------------------------------------------
def test_log_file_rotates_in_app_home_and_window_queue_fills(home, root_logger):
    handlers, qh = launcher.setup_logging(window=True)
    try:
        logging.getLogger("lutron_dmx.test").info("hello from the test")
        logging.getLogger("lutron_dmx.test").debug("not at info level")
    finally:
        launcher.teardown_logging(handlers)
    text = (home / "logs" / "lutron-dmx.log").read_text(encoding="utf-8")
    assert "hello from the test" in text and "not at info level" not in text
    levelno, line = qh.queue.get_nowait()
    assert levelno == logging.INFO and "hello from the test" in line
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)


def test_logging_survives_missing_stderr(home, root_logger, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)  # a double-clicked windowed build has no stderr
    handlers, _ = launcher.setup_logging()
    try:
        logging.getLogger("x").warning("still fine")
    finally:
        launcher.teardown_logging(handlers)
    assert "still fine" in (home / "logs" / "lutron-dmx.log").read_text(encoding="utf-8")


# -- self check (what CI runs against the packaged app) ------------------------
def test_self_check_passes(tmp_path, monkeypatch, capsys):
    out = tmp_path / "check.txt"
    code = launcher.self_check(str(out))
    report = out.read_text(encoding="utf-8")
    failures = [ln for ln in report.splitlines() if ln.startswith("FAIL")]
    if not launcher.tk_available():  # e.g. a Linux box without python3-tk
        failures = [ln for ln in failures if "tkinter" not in ln]
    assert not failures, report
    assert code == 0 or not launcher.tk_available()
    assert os.environ["LUTRON_DMX_HOME"] == str(tmp_path)  # restored afterwards


def test_cli_subcommands_pass_through(monkeypatch, capsys):
    assert launcher.main(["info"]) == 0
    assert "Config:" in capsys.readouterr().out


# -- the Tk window (needs tkinter and a display) -----------------------------
class StubThread:
    def __init__(self):
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.stop_requested = False
        self.exiting_for_update = False

    def is_alive(self):
        return not self.stopped.is_set()

    def status(self):
        return "ok", "Connected to Stub System"

    def request_stop(self):
        self.stop_requested = True
        self.stopped.set()


@pytest.fixture
def window(monkeypatch):
    """One LogWindow for the whole Tk test: on Windows, creating several Tk roots in a row can fail
    to find init.tcl, so everything the window does is checked in a single test."""
    tk = pytest.importorskip("tkinter")
    # On macOS, Tk aborts the whole process when there is no window server (e.g. a launchd runner),
    # so only try there when asked to.
    if sys.platform == "darwin" and not os.environ.get("LUTRON_DMX_TK_TESTS"):
        pytest.skip("set LUTRON_DMX_TK_TESTS=1 to run the Tk window test on macOS")
    from lutron_dmx.logwindow import LogWindow

    stub = StubThread()
    handler = launcher.QueueLogHandler()
    errors: list[str] = []
    try:
        w = LogWindow(stub, handler, "http://127.0.0.1:8080/#settings", open_on_ready=True,
                      show_error=lambda m, title="", parent=None: errors.append(m))
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    w.stub, w.handler, w.errors = stub, handler, errors
    yield w
    try:
        w.close()
    except tk.TclError:
        pass  # already closed by the test
    import gc

    gc.collect()  # release Tk objects here, not later in whichever thread triggers a collection


def pump(window, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        window.root.update()
        time.sleep(0.02)


def test_window_end_to_end(window, monkeypatch, root_logger):
    from tkinter import messagebox

    w, stub, handler = window, window.stub, window.handler
    opened = []
    monkeypatch.setattr("lutron_dmx.logwindow.open_browser", opened.append)

    # log lines and status; debug lines are hidden until asked for
    handler.queue.put((logging.INFO, "first line"))
    handler.queue.put((logging.ERROR, "bad thing"))
    handler.queue.put((logging.DEBUG, "hidden detail"))
    pump(w)
    text = w._text()
    assert "first line" in text and "bad thing" in text and "hidden detail" not in text
    assert w.status.cget("text") == "Connected to Stub System"
    assert opened == []  # server not ready yet

    # the browser opens once, on the Settings tab on a first run
    stub.ready.set()
    pump(w)
    pump(w)
    assert opened == ["http://127.0.0.1:8080/#settings"]
    w.open_gui()
    assert opened[-1] == "http://127.0.0.1:8080/"

    # debug toggle
    assert not w.debug_btn.instate(["selected"])
    w.debug_btn.invoke()
    assert w._debug and root_logger.level == logging.DEBUG
    handler.queue.put((logging.DEBUG, "now visible"))
    pump(w)
    assert "now visible" in w._text()
    w.debug_btn.invoke()

    # old lines are trimmed
    w.clear()
    assert w._text() == ""
    monkeypatch.setattr("lutron_dmx.logwindow.MAX_LINES", 50)
    for i in range(200):
        handler.queue.put((logging.INFO, f"line {i}"))
    pump(w)
    lines = w._text().splitlines()
    assert len(lines) <= 50 and lines[-1] == "line 199"

    # a server failure is reported in a dialog
    stub.error = RuntimeError("port exploded")
    pump(w)
    assert w.errors and "port exploded" in w.errors[0]
    stub.error = None

    # quitting asks first, then stops the server and closes the window
    answers = iter([False, True])
    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **k: next(answers))
    w.quit_app()  # user says "keep running"
    assert not stub.stop_requested
    w.quit_app()
    assert stub.stop_requested
    for _ in range(50):
        try:
            w.root.update()
        except Exception:
            break
        time.sleep(0.02)
    with pytest.raises(Exception):  # the root window is destroyed once the server has stopped
        w.root.winfo_exists()
