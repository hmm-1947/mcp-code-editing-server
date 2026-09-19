"""
MCP Server + ngrok - GUI Launcher
----------------------------------
A desktop GUI to start/stop the Joshua MCP server and ngrok, tail their
logs live, view the ngrok public URL, and control auto-start on login.

Closing the window (X) or minimizing sends it to the system tray.
Quitting is only possible from the tray icon's "Quit" menu item.

Requires: pip install pystray pillow requests
"""

import json
import os
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path
from queue import Queue, Empty

import tkinter as tk
from tkinter import ttk, font as tkfont

import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw

try:
    import requests
except ImportError:
    requests = None

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
PROJECT_DIR = Path(r"D:\projects\mcpserver\joshua-mcp")
VENV_PYTHON = PROJECT_DIR / "venv" / "Scripts" / "python.exe"
SERVER_SCRIPT = PROJECT_DIR / "server.py"
NGROK_EXE = "ngrok"
NGROK_PORT = 8001
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

LOG_DIR = PROJECT_DIR / "tray_logs"
LOG_ARCHIVE_DIR = LOG_DIR / "archive"
SERVER_LOG = LOG_DIR / "server.log"
NGROK_LOG = LOG_DIR / "ngrok.log"

APP_NAME = "CodyMCPServer"

# When frozen by PyInstaller, sys.executable IS the app's own exe path.
# When running as a plain .py script (dev mode), fall back to the VBS launcher.
if getattr(sys, "frozen", False):
    APP_EXECUTABLE_CMD = f'"{sys.executable}"'
else:
    _VBS_LAUNCHER = PROJECT_DIR / "launch_mcp_gui.vbs"
    APP_EXECUTABLE_CMD = f'wscript.exe "{_VBS_LAUNCHER}"'

ICON_PATH = PROJECT_DIR / "assets" / "icon.ico"

LOG_DIR.mkdir(exist_ok=True)
LOG_ARCHIVE_DIR.mkdir(exist_ok=True)

CREATE_NO_WINDOW = 0x08000000

# ----------------------------------------------------------------------
# THEME
# ----------------------------------------------------------------------
ACCENT = "#b5de71"
ACCENT_DARK = "#8fc24f"
BG = "#1e1f22"
BG_PANEL = "#26282b"
BG_LOG = "#141517"
FG = "#e7e9ea"
FG_DIM = "#9aa0a6"
BORDER = "#34363a"
RED = "#e06c75"


# ----------------------------------------------------------------------
# Process / log manager (core logic, independent of the UI)
# ----------------------------------------------------------------------
class ProcessManager:
    def __init__(self, log_callback):
        self._lock = threading.Lock()
        self.server_proc = None
        self.ngrok_proc = None
        self.server_log_fh = None
        self.ngrok_log_fh = None
        self.running = False
        self.log_callback = log_callback  # called as log_callback(stream_name, line)
        self.clear_callback = None  # called with no args right before a fresh start

    @staticmethod
    def _timestamp():
        return time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _filename_timestamp():
        return time.strftime("%Y-%m-%d_%H-%M-%S")

    def _archive_log(self, log_path: Path):
        if log_path.exists() and log_path.stat().st_size > 0:
            stamped = LOG_ARCHIVE_DIR / f"{log_path.stem}_{self._filename_timestamp()}{log_path.suffix}"
            try:
                log_path.rename(stamped)
            except OSError:
                pass

    def _open_log_handles(self):
        if self.server_log_fh:
            self.server_log_fh.close()
        if self.ngrok_log_fh:
            self.ngrok_log_fh.close()

        self._archive_log(SERVER_LOG)
        self._archive_log(NGROK_LOG)

        self.server_log_fh = open(SERVER_LOG, "a", buffering=1, encoding="utf-8")
        self.ngrok_log_fh = open(NGROK_LOG, "a", buffering=1, encoding="utf-8")

    def _tail_thread(self, path: Path, stream_name: str, stop_flag):
        """Follow a growing log file from the start and push new lines to the UI queue."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                while not stop_flag["stop"]:
                    line = f.readline()
                    if line:
                        self.log_callback(stream_name, line.rstrip("\n"))
                    else:
                        time.sleep(0.3)
        except FileNotFoundError:
            pass

    def start_all(self):
        with self._lock:
            if self.running:
                return
            self._open_log_handles()

            if self.clear_callback:
                self.clear_callback()

            self.server_log_fh.write(f"\n--- started {self._timestamp()} ---\n")
            self.server_proc = subprocess.Popen(
                [str(VENV_PYTHON), str(SERVER_SCRIPT)],
                cwd=str(PROJECT_DIR),
                stdout=self.server_log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )

        time.sleep(1)  # let server bind the port before ngrok attaches

        with self._lock:
            self.ngrok_log_fh.write(f"\n--- started {self._timestamp()} ---\n")
            self.ngrok_proc = subprocess.Popen(
                [NGROK_EXE, "http", str(NGROK_PORT), "--log=stdout", "--log-format=term", "--log-level=warn"],
                cwd=str(PROJECT_DIR),
                stdout=self.ngrok_log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            self.running = True

        # start tailing fresh log files
        self._stop_flags = {"server": {"stop": False}, "ngrok": {"stop": False}}
        threading.Thread(
            target=self._tail_thread, args=(SERVER_LOG, "server", self._stop_flags["server"]), daemon=True
        ).start()
        threading.Thread(
            target=self._tail_thread, args=(NGROK_LOG, "ngrok", self._stop_flags["ngrok"]), daemon=True
        ).start()

    def stop_all(self):
        with self._lock:
            if hasattr(self, "_stop_flags"):
                for flag in self._stop_flags.values():
                    flag["stop"] = True

            for proc in (self.ngrok_proc, self.server_proc):
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self.ngrok_proc = None
            self.server_proc = None
            self.running = False

    def restart_all(self):
        self.stop_all()
        time.sleep(1)
        self.start_all()

    def shutdown(self):
        self.stop_all()
        if self.server_log_fh:
            self.server_log_fh.close()
        if self.ngrok_log_fh:
            self.ngrok_log_fh.close()

    def get_ngrok_url(self):
        if requests is None:
            return None
        try:
            resp = requests.get(NGROK_API, timeout=1.5)
            tunnels = resp.json().get("tunnels", [])
            for t in tunnels:
                if t.get("proto") == "https":
                    return t.get("public_url")
            if tunnels:
                return tunnels[0].get("public_url")
        except Exception:
            return None
        return None


# ----------------------------------------------------------------------
# Windows "Run on startup" registry helper
# ----------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, APP_EXECUTABLE_CMD)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Cody MCP Server")
        self.root.geometry("760x560")
        self.root.minsize(600, 420)
        self.root.configure(bg=BG)
        try:
            if ICON_PATH.exists():
                self.root.iconbitmap(str(ICON_PATH))
        except Exception:
            pass  # icon file missing/invalid -- fall back to default

        self.log_queue = Queue()
        self.log_buffers = {"server": [], "ngrok": []}
        self.current_stream = tk.StringVar(value="server")
        self.status_text = tk.StringVar(value="Stopped")
        self.ngrok_url_text = tk.StringVar(value="ngrok URL: —")
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())

        self.pm = ProcessManager(log_callback=self._on_log_line)
        self.pm.clear_callback = self._on_logs_cleared

        self._build_style()
        self._build_ui()
        self._build_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self._on_minimize)

        self._poll_log_queue()
        self._poll_status()

    # -------------------- styling --------------------
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 15))
        style.configure("Status.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI Semibold", 11))
        style.configure("Url.TLabel", background=BG_PANEL, foreground=ACCENT, font=("Consolas", 10))

        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#111",
            font=("Segoe UI Semibold", 10),
            borderwidth=0,
            focuscolor=ACCENT,
            padding=(14, 8),
        )
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#4a4d3f")])

        style.configure(
            "Stop.TButton",
            background=BG_PANEL,
            foreground=FG,
            font=("Segoe UI Semibold", 10),
            borderwidth=1,
            padding=(14, 8),
        )
        style.map("Stop.TButton", background=[("active", "#33353a")], foreground=[("disabled", FG_DIM)])

        style.configure(
            "TCombobox",
            fieldbackground=BG_PANEL,
            background=BG_PANEL,
            foreground=FG,
            arrowcolor=FG,
            borderwidth=0,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG_PANEL)])

        style.configure(
            "TCheckbutton",
            background=BG,
            foreground=FG,
            font=("Segoe UI", 10),
        )
        style.map("TCheckbutton", background=[("active", BG)])

    # -------------------- UI layout --------------------
    def _build_ui(self):
        pad = 16

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=pad, pady=(pad, 8))

        ttk.Label(header, text="Cody MCP Server", style="Title.TLabel").pack(side="left")

        self.status_dot = tk.Canvas(header, width=12, height=12, bg=BG, highlightthickness=0)
        self.status_dot.pack(side="right", padx=(0, 6), pady=4)
        self._draw_dot(RED)
        ttk.Label(header, textvariable=self.status_text, style="Status.TLabel").pack(side="right", padx=(0, 8))

        # controls row
        controls = ttk.Frame(self.root)
        controls.pack(fill="x", padx=pad, pady=(0, 10))

        self.start_btn = ttk.Button(controls, text="▶  Start Server", style="Accent.TButton", command=self.on_start)
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(controls, text="■  Stop", style="Stop.TButton", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.restart_btn = ttk.Button(controls, text="⟳  Restart", style="Stop.TButton", command=self.on_restart)
        self.restart_btn.pack(side="left", padx=(8, 0))

        # log stream selector
        sel_frame = ttk.Frame(controls)
        sel_frame.pack(side="right")
        ttk.Label(sel_frame, text="Log:", style="Dim.TLabel").pack(side="left", padx=(0, 6))
        self.log_selector = ttk.Combobox(
            sel_frame,
            textvariable=self.current_stream,
            values=["server", "ngrok"],
            state="readonly",
            width=10,
        )
        self.log_selector.pack(side="left")
        self.log_selector.bind("<<ComboboxSelected>>", lambda e: self._refresh_log_view())

        # log panel
        log_panel = tk.Frame(self.root, bg=BG_LOG, highlightbackground=BORDER, highlightthickness=1)
        log_panel.pack(fill="both", expand=True, padx=pad, pady=(0, 10))

        self.log_text = tk.Text(
            log_panel,
            bg=BG_LOG,
            fg=FG,
            insertbackground=FG,
            font=("Consolas", 9),
            wrap="word",
            borderwidth=0,
            padx=10,
            pady=8,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(log_panel, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_text.tag_configure("dim", foreground=FG_DIM)
        self.log_text.tag_configure("accent", foreground=ACCENT)

        # footer: ngrok url + autostart checkbox
        footer = tk.Frame(self.root, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
        footer.pack(fill="x", padx=pad, pady=(0, pad))

        inner = ttk.Frame(footer, style="Panel.TFrame")
        inner.pack(fill="x", padx=12, pady=10)
        inner.configure(style="Panel.TFrame")

        ngrok_label_frame = tk.Frame(footer, bg=BG_PANEL)
        ngrok_label_frame.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(ngrok_label_frame, text="Public URL", bg=BG_PANEL, fg=FG_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        self.url_label = tk.Label(
            ngrok_label_frame, textvariable=self.ngrok_url_text, bg=BG_PANEL, fg=ACCENT, font=("Consolas", 11, "bold")
        )
        self.url_label.pack(anchor="w")

        bottom_row = tk.Frame(footer, bg=BG_PANEL)
        bottom_row.pack(fill="x", padx=12, pady=(2, 10))

        self.copy_btn = tk.Button(
            bottom_row,
            text="Copy URL",
            command=self.copy_url,
            bg=BG_PANEL,
            fg=ACCENT,
            activebackground="#33353a",
            activeforeground=ACCENT,
            borderwidth=1,
            relief="solid",
            font=("Segoe UI", 9),
            padx=10,
        )
        self.copy_btn.pack(side="left")

        autostart_chk = ttk.Checkbutton(
            bottom_row,
            text="Start automatically when Windows starts",
            variable=self.autostart_var,
            command=self.on_toggle_autostart,
        )
        autostart_chk.pack(side="right")

        self._set_running_ui(False)

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 11, 11, fill=color, outline="")

    # -------------------- tray --------------------
    def _build_tray(self):
        img = self._make_icon_image()
        self.tray_icon = pystray.Icon(
            "cody_mcp_gui",
            img,
            "Cody MCP Server",
            menu=pystray.Menu(
                Item("Open", self._tray_open, default=True),
                Item("Start", self._tray_start, enabled=lambda item: not self.pm.running),
                Item("Stop", self._tray_stop, enabled=lambda item: self.pm.running),
                Item("Restart", self._tray_restart, enabled=lambda item: self.pm.running),
                pystray.Menu.SEPARATOR,
                Item("Quit", self._tray_quit),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    @staticmethod
    def _make_icon_image():
        # Use the real app icon if present, else fall back to a generated dot.
        try:
            if ICON_PATH.exists():
                return Image.open(str(ICON_PATH)).convert("RGBA")
        except Exception:
            pass
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(181, 222, 113, 255))
        d.ellipse((20, 20, 44, 44), fill=(30, 31, 34, 255))
        return img

    def _tray_open(self, icon=None, item=None):
        self.root.after(0, self.show_from_tray)

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self.on_start)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self.on_stop)

    def _tray_restart(self, icon=None, item=None):
        self.root.after(0, self.on_restart)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self.quit_app)

    # -------------------- window show/hide --------------------
    def hide_to_tray(self):
        self.root.withdraw()

    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_minimize(self, event):
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(10, self.hide_to_tray)

    # -------------------- actions --------------------
    def on_start(self):
        if self.pm.running:
            return
        self._append_system_line("Starting server + ngrok...")
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        self.pm.start_all()

    def on_stop(self):
        if not self.pm.running:
            return
        self._append_system_line("Stopping server + ngrok...")
        threading.Thread(target=self.pm.stop_all, daemon=True).start()

    def on_restart(self):
        self._append_system_line("Restarting server + ngrok...")
        threading.Thread(target=self.pm.restart_all, daemon=True).start()

    def on_toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except Exception as e:
            self._append_system_line(f"Could not update startup setting: {e}")
            self.autostart_var.set(is_autostart_enabled())

    def copy_url(self):
        url = self.ngrok_url_text.get().replace("ngrok URL: ", "")
        if url and url != "—":
            self.root.clipboard_clear()
            self.root.clipboard_append(url)

    def quit_app(self):
        self._append_system_line("Shutting down...")
        threading.Thread(target=self._quit_worker, daemon=True).start()

    def _quit_worker(self):
        self.pm.shutdown()
        self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

    # -------------------- log handling --------------------
    def _on_log_line(self, stream_name, line):
        self.log_queue.put((stream_name, line))

    def _on_logs_cleared(self):
        # called from a worker thread; queue a sentinel handled on the Tk main thread
        self.log_queue.put(("__clear__", None))

    def _append_system_line(self, text):
        self.log_queue.put(("server" if self.current_stream.get() == "server" else "ngrok", f"* {text}"))

    def _poll_log_queue(self):
        try:
            while True:
                stream_name, line = self.log_queue.get_nowait()
                if stream_name == "__clear__":
                    self._clear_all_logs()
                    continue
                buf = self.log_buffers[stream_name]
                buf.append(line)
                if len(buf) > 2000:
                    del buf[: len(buf) - 2000]
                if self.current_stream.get() == stream_name:
                    self._append_log_line(line)
        except Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    def _clear_all_logs(self):
        self.log_buffers["server"].clear()
        self.log_buffers["ngrok"].clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log_line(self, line):
        self.log_text.configure(state="normal")
        tag = "dim" if line.startswith("---") else ("accent" if line.startswith("*") else None)
        if tag:
            self.log_text.insert("end", line + "\n", tag)
        else:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_log_view(self):
        stream = self.current_stream.get()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in self.log_buffers[stream]:
            tag = "dim" if line.startswith("---") else ("accent" if line.startswith("*") else None)
            if tag:
                self.log_text.insert("end", line + "\n", tag)
            else:
                self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -------------------- status polling --------------------
    def _poll_status(self):
        running = self.pm.running
        self._set_running_ui(running)

        current = self.ngrok_url_text.get()
        already_have_url = running and current.startswith("ngrok URL: http")

        if running and not already_have_url:
            url = self.pm.get_ngrok_url()
            self.ngrok_url_text.set(f"ngrok URL: {url}" if url else "ngrok URL: waiting for tunnel...")
        elif not running:
            self.ngrok_url_text.set("ngrok URL: —")

        # once we have the URL, poll rarely (just to notice a stop/crash);
        # otherwise poll frequently while waiting for the tunnel to come up
        next_delay = 15000 if already_have_url else 2000
        self.root.after(next_delay, self._poll_status)

    def _set_running_ui(self, running):
        if running:
            self.status_text.set("Running")
            self._draw_dot(ACCENT)
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.restart_btn.configure(state="normal")
        else:
            self.status_text.set("Stopped")
            self._draw_dot(RED)
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.restart_btn.configure(state="disabled")

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    # auto-start the server when the GUI itself launches
    app.on_start()
    app.run()


if __name__ == "__main__":
    main()
