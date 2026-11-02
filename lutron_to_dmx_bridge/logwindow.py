"""The small desktop window that shows the live log while Lutron To DMX Bridge runs (tkinter, no extra installs)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .launcher import APP_NAME, QueueLogHandler, ServerThread, log_path, open_browser

log = logging.getLogger(__name__)

MAX_LINES = 5000
TICK_MS = 150
STOP_TIMEOUT_S = 10.0

# Terminal-style pane that looks the same in light and dark mode.
_BG, _FG = "#1e1e1e", "#d8d8d8"
_LEVEL_COLORS = {"ERROR": "#ff6b6b", "WARNING": "#ffb454", "DEBUG": "#8a8a8a"}
_STATUS_COLORS = {"ok": "#2e9e4f", "warn": "#d98a00", "error": "#d03030", "info": "#808080"}


def _mono_font() -> tuple[str, int]:
    return {"darwin": ("Menlo", 11), "win32": ("Consolas", 10)}.get(sys.platform, ("DejaVu Sans Mono", 10))


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class LogWindow:
    def __init__(self, thread: ServerThread, handler: QueueLogHandler | None, url: str,
                 open_on_ready: bool = True, show_error=None) -> None:
        self.thread = thread
        self.handler = handler
        self.url = url
        self.open_on_ready = open_on_ready
        self.show_error = show_error or (lambda msg, title=APP_NAME, parent=None: messagebox.showerror(title, msg, parent=parent))
        self._opened = False
        self._error_shown = False
        self._stopping_since: float | None = None
        self._debug = False

        self.root = root = tk.Tk()
        root.title(f"{APP_NAME} {__version__}")
        root.geometry("920x540")
        root.minsize(620, 320)
        self._icon = None
        self._set_icon()
        root.report_callback_exception = self._on_callback_error
        root.protocol("WM_DELETE_WINDOW", self.quit_app)
        if root.tk.call("tk", "windowingsystem") == "aqua":
            root.createcommand("::tk::mac::Quit", self.quit_app)
            root.createcommand("::tk::mac::ReopenApplication", self.show)
        self._build()
        root.after(TICK_MS, self._tick)

    # -- construction -----------------------------------------------------
    def _set_icon(self) -> None:
        try:
            png = Path(__file__).parent / "assets" / "icon.png"
            self._icon = tk.PhotoImage(master=self.root, file=str(png))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass  # cosmetic only

    def _build(self) -> None:
        root = self.root
        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=_STATUS_COLORS["info"], outline="")
        self.status = ttk.Label(top, text="Starting…", wraplength=640, justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        self.open_btn = ttk.Button(top, text="Open web GUI", command=self.open_gui)
        self.open_btn.pack(side="right", padx=(8, 0))
        ttk.Label(root, foreground="#808080", padding=(10, 0),
                  text=f"Web GUI: {self.url.split('#')[0]}   ·   Closing this window stops Lutron To DMX Bridge; minimise it to keep it running"
                  ).pack(anchor="w")

        mid = ttk.Frame(root, padding=(10, 6))
        mid.pack(fill="both", expand=True)
        self.text = tk.Text(mid, wrap="word", state="disabled", bg=_BG, fg=_FG, insertbackground=_FG,
                            font=_mono_font(), relief="flat", borderwidth=0, padx=8, pady=6, height=10,
                            selectbackground="#3a5a8a")
        scroll = ttk.Scrollbar(mid, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for level, color in _LEVEL_COLORS.items():
            self.text.tag_configure(level, foreground=color)
        self.text.tag_configure("CRITICAL", foreground="#ff6b6b", font=_mono_font() + ("bold",))

        bar = ttk.Frame(root, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        self.copy_btn = ttk.Button(bar, text="Copy log", command=self.copy_log)
        self.copy_btn.pack(side="left")
        ttk.Button(bar, text="Save log…", command=self.save_log).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(bar, text="Open log folder", command=lambda: open_folder(log_path().parent)).pack(side="left", padx=6)
        self.debug_btn = ttk.Checkbutton(bar, text="Show debug detail", command=self._toggle_debug)
        self.debug_btn.state(["!alternate"])  # starts unticked (ttk checkbuttons default to the "mixed" look)
        self.debug_btn.pack(side="left", padx=6)
        self.quit_btn = ttk.Button(bar, text="Quit", command=self.quit_app)
        self.quit_btn.pack(side="right")

    # -- running ------------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        # Tk images must be released on this thread, not whenever another thread's GC runs
        self._icon = None
        self.root.destroy()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def open_gui(self) -> None:
        open_browser(self.url.split("#")[0])

    # -- periodic work ------------------------------------------------------
    def _tick(self) -> None:
        try:
            self._drain()
            self._update_status()
            self._check_events()
        except Exception:
            log.exception("Log window update failed")
        finally:
            if self.root.winfo_exists():
                self.root.after(TICK_MS, self._tick)

    def _drain(self) -> None:
        if self.handler is None:
            return
        items = []
        for _ in range(500):
            try:
                items.append(self.handler.queue.get_nowait())
            except Exception:
                break
        if not items:
            return
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        for levelno, line in items:
            if levelno < logging.INFO and not self._debug:
                continue
            self.text.insert("end", line + "\n", (logging.getLevelName(levelno),))
        overflow = int(self.text.index("end-1c").split(".")[0]) - MAX_LINES
        if overflow > 0:
            self.text.delete("1.0", f"{overflow + 1}.0")
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _update_status(self) -> None:
        if self._stopping_since is not None:
            level, text = "info", "Stopping…"
        else:
            level, text = self.thread.status()
        self.status.configure(text=text)
        self.dot.itemconfigure(self._dot_id, fill=_STATUS_COLORS[level])

    def _check_events(self) -> None:
        if self.thread.exiting_for_update and self.thread.stopped.is_set() and self._stopping_since is None:
            log.info("Closing for the update.")
            self._stopping_since = time.monotonic()
            self.close()
            return
        if self._stopping_since is not None:
            if self.thread.stopped.is_set() or time.monotonic() - self._stopping_since > STOP_TIMEOUT_S:
                if not self.thread.stopped.is_set():
                    log.warning("The server did not stop in time; closing anyway.")
                self.close()
            return
        if self.thread.ready.is_set() and not self._opened:
            self._opened = True
            if self.thread.error is None and self.open_on_ready:
                open_browser(self.url)
        if self.thread.error is not None and not self._error_shown:
            self._error_shown = True
            self.show()
            self.show_error(f"{APP_NAME} stopped unexpectedly:\n\n{self.thread.error}\n\n"
                            f"The full details are in the log (Save log… or Open log folder):\n{log_path()}",
                            parent=self.root)

    # -- buttons ------------------------------------------------------------
    def _text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self._text())
        self.root.update_idletasks()
        self.copy_btn.configure(text="Copied!")
        self.root.after(1500, lambda: self.copy_btn.configure(text="Copy log"))

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save log", defaultextension=".txt",
            initialfile=f"lutron-to-dmx-bridge-log-{datetime.now():%Y%m%d-%H%M%S}.txt")
        if path:
            try:
                Path(path).write_text(self._text() + "\n", encoding="utf-8")
            except OSError as exc:
                self.show_error(f"Could not save the log:\n{exc}", parent=self.root)

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _toggle_debug(self) -> None:
        self._debug = self.debug_btn.instate(["selected"])
        logging.getLogger().setLevel(logging.DEBUG if self._debug else logging.INFO)
        log.info("Debug detail %s", "on" if self._debug else "off")

    def quit_app(self) -> None:
        if self._stopping_since is not None:
            return
        if self.thread.is_alive() and self.thread.error is None:
            if not messagebox.askokcancel(
                    f"Stop {APP_NAME}?",
                    "Quitting stops the web GUI and DMX control. Your lights stay as they are.\n\n"
                    "To keep it running, minimise this window instead.",
                    parent=self.root, icon="warning"):
                return
        self._stopping_since = time.monotonic()
        self.quit_btn.configure(state="disabled")
        log.info("Stopping…")
        self.thread.request_stop()

    def _on_callback_error(self, exc_type, exc, tb) -> None:
        log.error("Unexpected error in the log window", exc_info=(exc_type, exc, tb))
