"""Double-click launcher: runs the server, opens the web GUI, and shows a live log window.

    python -m lutron_dmx.launcher        (or the packaged "Lutron DMX" app / .exe)

What it does, in order:
  1. If Lutron DMX is already running (this port answers as us), just open the browser and exit.
  2. Otherwise start the web server (and DMX input) on a background thread.
  3. Open the web GUI in the default browser (on the Settings tab the very first time).
  4. Show a log window until it is closed; closing it stops the app.

Without a usable tkinter (e.g. Homebrew Python without ``python-tk``) it falls back to running in the
terminal, printing the same log.  Logs always go to a rotating file as well (see ``log_path``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .config import AppConfig, data_dir

APP_NAME = "Lutron DMX"
log = logging.getLogger("lutron_dmx.launcher")

#: Tried in order when the configured port is taken by something that isn't Lutron DMX.
FALLBACK_PORTS = tuple(range(8081, 8100))
#: How long to hold the browser back for the controller connection before opening the GUI anyway.
CONNECT_GRACE_S = 6.0
STOP_TIMEOUT_S = 10.0
CLI_COMMANDS = {"pair", "discover", "backup", "info"}


class LauncherError(Exception):
    """A problem worth showing to the user in plain words."""


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def log_path() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "lutron-dmx.log"


class QueueLogHandler(logging.Handler):
    """Passes formatted records to the log window; the window drains ``queue`` on its own thread."""

    def __init__(self) -> None:
        super().__init__()
        self.queue: queue.Queue[tuple[int, str]] = queue.Queue()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put((record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
_FILE_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def setup_logging(verbose: bool = False, window: bool = False) -> tuple[list[logging.Handler], QueueLogHandler | None]:
    """Attach the rotating file (and the terminal and window, when there is one). Undo with ``teardown_logging``."""
    handlers: list[logging.Handler] = []
    try:
        fh = logging.handlers.RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(_FILE_FORMAT)
        handlers.append(fh)
    except OSError as exc:  # read-only home etc.: keep going without a file
        print(f"Could not open the log file: {exc}", file=sys.stderr)
    # A double-clicked build has no console: sys.stderr is None.
    if _stderr_is_terminal():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(_FORMAT)
        handlers.append(sh)
    qh = None
    if window:
        qh = QueueLogHandler()
        qh.setFormatter(_FORMAT)
        handlers.append(qh)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in handlers:
        root.addHandler(h)
    logging.captureWarnings(True)
    return handlers, qh


def _stderr_is_terminal() -> bool:
    """False for a double-clicked build (no stderr) or a pipe nobody is reading."""
    try:
        return bool(sys.stderr and sys.stderr.isatty())
    except Exception:
        return False


def teardown_logging(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for h in handlers:
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()


# ---------------------------------------------------------------------------
# single instance / port handling
# ---------------------------------------------------------------------------
def probe(port: int, host: str = "127.0.0.1", timeout: float = 4.0) -> str:
    """"ours" (Lutron DMX answers there), "free" (nothing listening) or "other" (something else is)."""
    if sys.platform == "win32":
        # Windows takes ~2 s to refuse a connection to a closed port, but refuses a second bind at once.
        with contextlib.suppress(OSError):
            bind_socket(host, port).close()
            return "free"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        return "free"
    except OSError:
        return "other"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=timeout) as resp:
            data = json.loads(resp.read(65536))
        if isinstance(data, dict) and "system_name" in data and "connected" in data:
            return "ours"
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "other"


def resolve_port(preferred: int, probe_host: str = "127.0.0.1") -> tuple[int, bool]:
    """Pick the port to use: (port, already_running).

    The configured port wins.  If something else holds it, take the first free fallback port, but if a
    copy of Lutron DMX is found on any of them, that copy is the one to reuse."""
    for port in (preferred, *[p for p in FALLBACK_PORTS if p != preferred]):
        state = probe(port, probe_host)
        if state == "ours":
            return port, True
        if state == "free":
            if port != preferred:
                log.warning("Port %d is in use by another program; using %d instead.", preferred, port)
            return port, False
    raise LauncherError(f"Port {preferred} (and {FALLBACK_PORTS[0]}-{FALLBACK_PORTS[-1]}) are all in use by "
                        "other programs. Close them, or set a different web_port in the config file.")


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind up front, so a taken port is an ordinary error here rather than uvicorn's sys.exit()."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":  # on Windows SO_REUSEADDR would let two programs share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def browse_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def needs_setup(config: AppConfig) -> bool:
    """True on a first run, before any controller has been chosen."""
    d = config.driver
    if d.kind in ("ra2", "leap"):
        return not d.host
    return not AppConfig.path().exists()


def start_url(base: str, config: AppConfig) -> str:
    return base + ("#settings" if needs_setup(config) else "")


def open_browser(url: str) -> None:
    try:
        if not webbrowser.open(url):
            log.warning("Could not open a browser automatically. Open %s yourself.", url)
    except Exception as exc:
        log.warning("Could not open a browser automatically (%s). Open %s yourself.", exc, url)


# ---------------------------------------------------------------------------
# the server, on a background thread
# ---------------------------------------------------------------------------
class ServerThread(threading.Thread):
    """Runs the controller and web server on their own event loop.

    ``ready`` is set once the GUI can be opened (or the thread has died, in which case ``error`` is set).
    ``request_stop()`` is safe from any thread."""

    def __init__(self, config: AppConfig, sock: socket.socket, verbose: bool = False, updater=None) -> None:
        super().__init__(name="lutron-dmx-server", daemon=True)
        self.config = config
        self.sock = sock
        self.verbose = verbose
        #: lutron_dmx.updater.Updater, or None (self-check, tests): then the app never touches the network
        self.updater = updater
        #: set when the server is stopping so an update can replace the app, not because the user quit
        self.exiting_for_update = False
        self.controller = None
        self.server = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error: BaseException | None = None

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # SystemExit too: uvicorn calls sys.exit() on some failures
            if not isinstance(exc, KeyboardInterrupt):
                self.error = exc
                log.exception("The server stopped unexpectedly")
        finally:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.ready.set()
            self.stopped.set()

    async def _serve(self) -> None:
        import uvicorn

        from .controller import Controller
        from .web.app import create_app

        discover = None
        if self.config.driver.kind == "simulator":
            from .discovery.fake import fake_discover as discover
        controller = self.controller = Controller(self.config)
        if self.updater is not None:
            self.updater.request_exit = self.request_exit_for_update
        app = create_app(controller, discover=discover, updater=self.updater, bind_host=self.config.web_host)
        # log_config=None: uvicorn's default config writes to stderr, which a windowed build doesn't have
        server = self.server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, log_level="debug" if self.verbose else "info", access_log=self.verbose))
        serve_task = asyncio.create_task(server.serve(sockets=[self.sock]))
        try:
            while not server.started:
                if serve_task.done():
                    serve_task.result()
                    raise LauncherError("The web server stopped before it was ready.")
                await asyncio.sleep(0.05)
            # Connect to the controller without holding the GUI back for long if it is slow or away.
            start_task = asyncio.create_task(controller.start())
            done, _ = await asyncio.wait({start_task}, timeout=CONNECT_GRACE_S)
            if not done:
                log.warning("Still trying to reach the Lutron controller; the GUI is open meanwhile.")
            else:
                start_task.result()
            log.info("Web GUI is ready at http://%s:%d/", browse_host(self.sock.getsockname()[0]), self.port)
            self.ready.set()
            await serve_task
            if not start_task.done():
                start_task.cancel()
        finally:
            if not serve_task.done():
                server.should_exit = True
                with contextlib.suppress(BaseException):
                    await serve_task
            await controller.stop()

    def request_stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True

    def request_exit_for_update(self) -> None:
        """The update helper is waiting for this process to end; the log window closes by itself."""
        self.exiting_for_update = True
        self.request_stop()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        self.request_stop()
        return self.stopped.wait(timeout)

    # -- for the status line -------------------------------------------
    def status(self) -> tuple[str, str]:
        """(level, text) where level is "ok", "warn", "error" or "info"."""
        if self.error is not None:
            return "error", f"Stopped: {self.error}"
        if self.stopped.is_set():
            return "info", "Stopped"
        if not self.ready.is_set():
            return "info", "Starting…"
        c = self.controller
        if c is None:
            return "info", "Starting…"
        level, text = self._controller_status(c)
        notice = self.update_notice()
        return (level, f"{text} · {notice}") if notice else (level, text)

    def update_notice(self) -> str:
        u = self.updater
        if u is None:
            return ""
        if self.exiting_for_update:
            return "Updating: the app will reopen in a moment"
        if u.phase in ("downloading", "verifying", "staging", "testing"):
            return f"Updating: {u.message}"
        rel = u.available()
        if rel is not None and u.config.updates.dismissed_version != rel.version:
            return f"Update {rel.version} available (open the web GUI to install)"
        return ""

    def _controller_status(self, c) -> tuple[str, str]:
        d = c.driver
        name = getattr(d, "system_name", "Lutron")
        simulated = self.config.driver.kind == "simulator"
        dmx = " · DMX input on" if c.dmx is not None else ""
        if simulated:
            return "warn", "Demo mode with simulated lights: open Settings in the web GUI to connect to your Lutron system" + dmx
        if c.last_error:
            return "warn", f"Web GUI running, but not connected to the Lutron controller: {c.last_error}"
        try:
            connected = bool(d.connected)
        except Exception:
            connected = False
        if connected:
            return "ok", f"Connected to {name}" + dmx
        return "warn", "Web GUI running; connecting to the Lutron controller…"


# ---------------------------------------------------------------------------
# error dialogs
# ---------------------------------------------------------------------------
def show_error(message: str, title: str = APP_NAME, parent=None) -> None:
    """Tell the user something went wrong, even when there is no console (a double-clicked build)."""
    log.error("%s", message)
    try:
        from tkinter import messagebox

        if parent is not None:
            messagebox.showerror(title, message, parent=parent)
            return
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            messagebox.showerror(title, message, parent=root)
        finally:
            root.destroy()
        return
    except Exception:
        pass
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        if sys.platform == "darwin":
            import subprocess

            script = f'display dialog {json.dumps(message)} with title {json.dumps(title)} buttons {{"OK"}} with icon stop'
            subprocess.run(["osascript", "-e", script], check=False, timeout=600)
            return
    except Exception:
        pass
    print(f"{title}: {message}", file=sys.stderr)


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# self-check (used by CI on the packaged app)
# ---------------------------------------------------------------------------
def self_check(out: str | None) -> int:
    """Start the simulator on a spare port, fetch the GUI and API, stop.  Never touches the real config."""
    lines: list[str] = []
    ok = True

    def note(msg: str) -> None:
        lines.append(msg)

    def need(cond: bool, msg: str) -> None:
        nonlocal ok
        note(("ok    " if cond else "FAIL  ") + msg)
        ok = ok and cond

    saved_home = os.environ.get("LUTRON_DMX_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LUTRON_DMX_HOME"] = tmp
        note(f"{APP_NAME} {__version__} on Python {sys.version.split()[0]} ({sys.platform}, frozen={getattr(sys, 'frozen', False)})")
        try:
            import tkinter

            note(f"tkinter {tkinter.TkVersion} importable")
            need(True, "tkinter")
        except Exception as exc:
            need(False, f"tkinter import: {exc}")
        thread = None
        try:
            config = AppConfig()
            config.driver.kind = "simulator"
            sock = bind_socket("127.0.0.1", 0)
            thread = ServerThread(config, sock)
            thread.start()
            need(thread.ready.wait(30) and thread.error is None, f"server started ({thread.error or 'no error'})")
            base = f"http://127.0.0.1:{thread.port}"
            need(probe(thread.port) == "ours", "instance probe recognises the server")
            with urllib.request.urlopen(base + "/", timeout=10) as r:
                need(b"<html" in r.read(4096).lower(), "web GUI page served")
            with urllib.request.urlopen(base + "/static/app.js", timeout=10) as r:
                need(r.status == 200, "static files bundled")
            with urllib.request.urlopen(base + "/api/inventory", timeout=10) as r:
                inv = json.loads(r.read())
            need(bool(inv.get("zones")), f"simulator inventory has {len(inv.get('zones', []))} lights")
            need(thread.stop(), "server stopped cleanly")
        except Exception as exc:
            need(False, f"{type(exc).__name__}: {exc}")
            if thread is not None:
                thread.stop(5)
        if saved_home is None:
            os.environ.pop("LUTRON_DMX_HOME", None)
        else:
            os.environ["LUTRON_DMX_HOME"] = saved_home
    note("PASS" if ok else "FAILED")
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text, end="")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _run_console(thread: ServerThread, url: str, open_it: bool) -> None:
    """No window: stay in the terminal until Ctrl+C."""
    thread.ready.wait()
    if thread.error is None:
        print(f"{APP_NAME} is running at {url}  (press Ctrl+C to stop)", flush=True)
        if open_it:
            open_browser(url)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("Stopping…", flush=True)
        thread.stop()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--apply-update":  # the new copy finishing an update (see updater.py)
        from .updater import run_helper

        return run_helper(argv[1])
    if argv and argv[0] in CLI_COMMANDS:  # the packaged app doubles as the command-line tool
        from .__main__ import main as cli_main

        cli_main(argv)
        return 0

    parser = argparse.ArgumentParser(prog="lutron-dmx-app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulator", action="store_true", help="use the built-in simulated system")
    parser.add_argument("--port", type=int, help="web GUI port (default from config, 8080)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument("--no-window", action="store_true", help="no log window; run in the terminal")
    parser.add_argument("--self-check", nargs="?", const="", metavar="FILE",
                        help="start, fetch the GUI, stop; write the result to FILE (for CI)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    if args.self_check is not None:
        return self_check(args.self_check or None)

    use_window = not args.no_window and tk_available()
    handlers, qh = setup_logging(args.verbose, window=use_window)
    log.info("%s %s starting (log file: %s)", APP_NAME, __version__, log_path())
    try:
        return _launch(args, use_window, qh)
    finally:
        teardown_logging(handlers)
        logging.shutdown()


def _launch(args: argparse.Namespace, use_window: bool, qh: QueueLogHandler | None) -> int:
    try:
        config = AppConfig.load()
    except Exception as exc:
        show_error(f"The settings file could not be read:\n{AppConfig.path()}\n\n{exc}\n\n"
                   "Fix or rename that file and try again.")
        return 1
    if args.simulator:
        config.driver.kind = "simulator"
    host = config.web_host
    probe_host = browse_host(host)
    preferred = args.port or config.web_port

    sock = None
    for attempt in range(2):
        try:
            port, running = resolve_port(preferred, probe_host)
        except LauncherError as exc:
            show_error(str(exc))
            return 1
        if running:
            url = f"http://{probe_host}:{port}/"
            log.info("%s is already running at %s; opening it in the browser.", APP_NAME, url)
            if not args.no_browser:
                open_browser(url)
            return 0
        try:
            sock = bind_socket(host, port)
            break
        except OSError as exc:  # lost a race with another launch, or the port is reserved
            log.warning("Could not use port %d: %s", port, exc)
            if attempt:
                show_error(f"Could not start the web server on port {port}:\n{exc}")
                return 1
    assert sock is not None

    port = sock.getsockname()[1]
    base = f"http://{probe_host}:{port}/"
    url = base if args.simulator else start_url(base, config)
    from .updater import Updater

    thread = ServerThread(config, sock, verbose=args.verbose, updater=Updater(config))
    thread.start()

    if use_window:
        try:
            from .logwindow import LogWindow

            window = LogWindow(thread, qh, url=url, open_on_ready=not args.no_browser, show_error=show_error)
        except Exception as exc:  # no display, broken Tcl/Tk install, ...
            log.warning("Could not open the log window (%s); continuing in the terminal.", exc)
            use_window = False
        else:
            window.run()
            return 1 if thread.error is not None else 0
    if not use_window:
        if qh is None and sys.stderr is None:
            log.info("No console and no window available; see the log file.")
        _run_console(thread, url, not args.no_browser)
    if thread.error is not None:
        show_error(f"{APP_NAME} stopped unexpectedly:\n{thread.error}\n\nDetails are in {log_path()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
