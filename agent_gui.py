import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import app_paths
from agent import activity, modelcache, openrouter, resources, settings

MODE_LABELS = {
    "openrouter": "Cloud only",
    "local": "Local only",
    "both": "Cloud + local",
}
MODE_HINTS = {
    "openrouter": "OpenRouter, Requesty, Gemini and NVIDIA NIM models only. Local models are never used.",
    "local": "LM Studio models on this machine only. No cloud calls are made.",
    "both": "Cloud models are tried first. The local model is the fallback.",
}
MAX_ROWS = 300
AMBER = "#e5c07b"
OFF_GREY = "#45484d"
DARK_TEXT = "#14150f"


def _ngrok_config_path():
    return app_paths.data_dir() / "ngrok.yml"


def read_ngrok_token() -> str:
    try:
        text = _ngrok_config_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^\s*authtoken:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1).strip("\"'") if match else ""


def write_ngrok_token(token: str) -> None:
    target = _ngrok_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if re.search(r"^\s*authtoken:.*$", text, re.MULTILINE):
        text = re.sub(r"^(\s*authtoken:).*$", lambda m: f"{m.group(1)} {token}", text, flags=re.MULTILINE)
    elif text.strip():
        text = text.rstrip() + f"\nagent:\n  authtoken: {token}\n"
    else:
        text = f'version: "3"\nagent:\n  authtoken: {token}\n'
    target.write_text(text, encoding="utf-8")


def _clock(value):
    return time.strftime("%H:%M:%S", time.localtime(value)) if value else ""


def _where(backend: str) -> str:
    low = (backend or "").lower()
    if not low:
        return ""
    if low.startswith("local") or low.startswith("lm"):
        return "Local"
    if low.startswith("requesty"):
        return "Requesty"
    if low.startswith("nvidia"):
        return "NVIDIA NIM"
    return "Gemini" if low.startswith("gemini") else "OpenRouter"


class Switch(tk.Canvas):
    def __init__(self, parent, theme, value=False, command=None, bg=None):
        super().__init__(parent, width=50, height=28, bg=bg or theme["BG_PANEL"], highlightthickness=0,
                         cursor="hand2")
        self.accent = theme["ACCENT"]
        self.value = bool(value)
        self.command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    def _pill(self, x0, y0, x1, y1, color):
        r = (y1 - y0) / 2
        self.create_oval(x0, y0, x0 + 2 * r, y1, fill=color, outline="")
        self.create_oval(x1 - 2 * r, y0, x1, y1, fill=color, outline="")
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="")

    def _draw(self):
        self.delete("all")
        self._pill(2, 2, 48, 26, self.accent if self.value else OFF_GREY)
        x = 36 if self.value else 14
        self.create_oval(x - 9, 5, x + 9, 23, fill="#ffffff" if self.value else "#c9ccd0", outline="")

    def set(self, value):
        self.value = bool(value)
        self._draw()

    def _click(self, _event):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)


class Segmented(tk.Frame):
    def __init__(self, parent, theme, options, value, command):
        super().__init__(parent, bg=theme["BG_LOG"], padx=3, pady=3)
        self.theme = theme
        self.command = command
        self.active = True
        self.value = value
        self.buttons = {}
        ui = theme.get("UI_FONT", "Segoe UI")
        for key, label in options.items():
            button = tk.Label(self, text=label, font=(ui, 10, "bold"), padx=18, pady=7, cursor="hand2")
            button.pack(side="left", padx=1)
            button.bind("<Button-1>", lambda _e, k=key: self._pick(k))
            self.buttons[key] = button
        self._paint()

    def _paint(self):
        for key, button in self.buttons.items():
            selected = key == self.value
            if selected:
                button.configure(bg=self.theme["ACCENT"] if self.active else OFF_GREY,
                                 fg=DARK_TEXT if self.active else self.theme["FG_DIM"])
            else:
                button.configure(bg=self.theme["BG_LOG"], fg=self.theme["FG_DIM"])

    def _pick(self, key):
        if not self.active or key == self.value:
            return
        self.value = key
        self._paint()
        if self.command:
            self.command(key)

    def set_active(self, active):
        self.active = bool(active)
        self._paint()


class AgentsWindow:
    def __init__(self, root, theme, parent, on_changed=None):
        self.root = root
        self.parent = parent
        self.theme = theme
        self.on_changed = on_changed
        self.win = None
        self.offset = 0
        self.rows: dict[str, dict] = {}
        self.snapshot: dict = {}
        self._snap_busy = False
        self._alive = False
        self._pending: list = []
        self.status_rows: dict = {}
        self.model_vars: dict = {}
        self.free_vars: dict = {}

    def show(self):
        if self._alive and self.win is not None and self.win.winfo_exists():
            return
        t = self.theme
        self.ui = t.get("UI_FONT", "Segoe UI")
        self.red = t.get("RED", "#e06c75")
        self.win = self.parent
        self._style()
        self.cfg = settings.load()
        self.key_var = tk.StringVar(value=self.cfg["openrouter_api_key"])
        self.rq_key_var = tk.StringVar(value=self.cfg["requesty_api_key"])
        self.rq_url_var = tk.StringVar(value=self.cfg["requesty_base_url"])
        self.gm_key_var = tk.StringVar(value=self.cfg["gemini_api_key"])
        self.gm_url_var = tk.StringVar(value=self.cfg["gemini_base_url"])
        self.nv_key_var = tk.StringVar(value=self.cfg["nvidia_api_key"])
        self.nv_url_var = tk.StringVar(value=self.cfg["nvidia_base_url"])
        self.ngrok_var = tk.StringVar(value=read_ngrok_token())
        self.msg_var = tk.StringVar(value="")
        self.hint_var = tk.StringVar(value=MODE_HINTS[self.cfg["mode"]])
        self._header()
        footer = tk.Frame(self.win, bg=t["BG"])
        footer.pack(side="bottom", fill="x", padx=20, pady=(0, 12))
        self.msg_label = tk.Label(footer, textvariable=self.msg_var, bg=t["BG"], fg=t["FG_DIM"],
                                  font=(self.ui, 9), anchor="w")
        self.msg_label.pack(fill="x")
        notebook = ttk.Notebook(self.win, style="Agent.TNotebook")
        notebook.pack(fill="both", expand=True, padx=14, pady=(4, 4))
        self.notebook = notebook
        self._tab_control(notebook)
        self._tab_providers(notebook)
        self._tab_activity(notebook)
        self._tab_resources(notebook)
        self._apply_enabled()
        self._alive = True
        self.offset = 0
        self.rows.clear()
        self._tick()
        self._snap_loop()

    def _style(self):
        t = self.theme
        style = ttk.Style(self.win)
        style.configure("Agent.Treeview", background=t["BG_LOG"], fieldbackground=t["BG_LOG"],
                        foreground=t["FG"], borderwidth=0, rowheight=28, font=(self.ui, 9))
        style.configure("Agent.Treeview.Heading", background=t["BG_PANEL"], foreground=t["FG_DIM"],
                        relief="flat", font=(self.ui, 9, "bold"), padding=(8, 7))
        style.map("Agent.Treeview", background=[("selected", "#33353a")])
        style.map("Agent.Treeview.Heading", background=[("active", "#33353a")])
        style.configure("Agent.TNotebook", background=t["BG"], borderwidth=0, tabmargins=(6, 0, 6, 0))
        style.configure("Agent.TNotebook.Tab", background=t["BG"], foreground=t["FG_DIM"], padding=(16, 7),
                        borderwidth=0, font=(self.ui, 9, "bold"))
        style.map("Agent.TNotebook.Tab", background=[("selected", t["BG_PANEL"])],
                  foreground=[("selected", t["ACCENT"])])

    def _header(self):
        t = self.theme
        head = tk.Frame(self.win, bg=t["BG"])
        head.pack(fill="x", padx=20, pady=(10, 4))
        left = tk.Frame(head, bg=t["BG"])
        left.pack(side="left")
        tk.Label(left, text="Agents", bg=t["BG"], fg=t["FG"], font=(self.ui, 15, "bold")).pack(side="left")
        self.sub_var = tk.StringVar()
        tk.Label(left, textvariable=self.sub_var, bg=t["BG"], fg=t["FG_DIM"], font=(self.ui, 10)).pack(side="left", padx=(14, 0), pady=(4, 0))
        right = tk.Frame(head, bg=t["BG"])
        right.pack(side="right")
        self.master = Switch(right, t, self.cfg["enabled"], self._toggle_master, bg=t["BG"])
        self.master.pack(side="right", padx=(12, 0))
        self.pill = tk.Label(right, text="OFF", font=(self.ui, 10, "bold"), padx=12, pady=3)
        self.pill.pack(side="right")

    def _say(self, text, ok=True):
        self.msg_var.set(text)
        self.msg_label.configure(fg=self.theme["ACCENT"] if ok else self.red)

    def _entry(self, parent, var, show=""):
        t = self.theme
        return tk.Entry(parent, textvariable=var, show=show, bg=t["BG_LOG"], fg=t["FG"], insertbackground=t["FG"],
                        relief="flat", font=(self.ui, 10), highlightthickness=1, highlightbackground=t["BORDER"],
                        highlightcolor=t["ACCENT"], disabledbackground=t["BG_LOG"])

    def _btn(self, parent, text, command, primary=True):
        t = self.theme
        if primary:
            return tk.Button(parent, text=text, command=command, bg=t["ACCENT"], fg=DARK_TEXT,
                             activebackground=t.get("ACCENT_DARK", t["ACCENT"]), activeforeground=DARK_TEXT,
                             relief="flat", bd=0, padx=14, pady=6, font=(self.ui, 9, "bold"), cursor="hand2")
        return tk.Button(parent, text=text, command=command, bg="#33353a", fg=t["FG"], activebackground="#3d4046",
                         activeforeground=t["FG"], relief="flat", bd=0, padx=12, pady=6, font=(self.ui, 9),
                         cursor="hand2")

    def _label(self, parent, text, dim=True, size=9, bold=False, **pack):
        t = self.theme
        label = tk.Label(parent, text=text, bg=t["BG_PANEL"], fg=t["FG_DIM"] if dim else t["FG"],
                         font=(self.ui, size, "bold" if bold else "normal"), anchor="w", justify="left")
        if pack:
            label.pack(**pack)
        return label

    def _card(self, parent, title, subtitle=""):
        t = self.theme
        outer = tk.Frame(parent, bg=t["BG_PANEL"], highlightbackground=t["BORDER"], highlightthickness=1)
        outer.pack(fill="x", padx=18, pady=(0, 14))
        head = tk.Frame(outer, bg=t["BG_PANEL"])
        head.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(head, text=title, bg=t["BG_PANEL"], fg=t["FG"], font=(self.ui, 12, "bold")).pack(side="left")
        body = tk.Frame(outer, bg=t["BG_PANEL"])
        body.pack(fill="x", padx=18, pady=(0, 16))
        if subtitle:
            self._label(body, subtitle, anchor="w", pady=(0, 6))
        return head, body

    def _page(self, nb, title):
        t = self.theme
        outer = tk.Frame(nb, bg=t["BG"])
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, bg=t["BG"], highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=t["BG"])
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        tk.Frame(inner, bg=t["BG"], height=14).pack()
        return inner

    def _secret_row(self, parent, label, var, save_text=None, save_cmd=None):
        self._label(parent, label, anchor="w", pady=(8, 4))
        row = tk.Frame(parent, bg=self.theme["BG_PANEL"])
        row.pack(fill="x")
        entry = self._entry(row, var, "*")
        entry.pack(side="left", fill="x", expand=True, ipady=7)
        state = {"shown": False}
        toggle = self._btn(row, "Show", None, primary=False)

        def flip():
            state["shown"] = not state["shown"]
            entry.configure(show="" if state["shown"] else "*")
            toggle.configure(text="Hide" if state["shown"] else "Show")

        toggle.configure(command=flip)
        toggle.pack(side="left", padx=(8, 0))
        if save_cmd:
            self._btn(row, save_text, save_cmd).pack(side="left", padx=(8, 0))
        return entry

    def _status_row(self, parent, name):
        t = self.theme
        row = tk.Frame(parent, bg=t["BG_PANEL"])
        row.pack(fill="x", pady=5)
        dot = tk.Canvas(row, width=12, height=12, bg=t["BG_PANEL"], highlightthickness=0)
        dot.pack(side="left", padx=(0, 12))
        oval = dot.create_oval(1, 1, 11, 11, fill=OFF_GREY, outline="")
        tk.Label(row, text=name, width=13, anchor="w", bg=t["BG_PANEL"], fg=t["FG"],
                 font=(self.ui, 10, "bold")).pack(side="left")
        var = tk.StringVar()
        tk.Label(row, textvariable=var, anchor="w", bg=t["BG_PANEL"], fg=t["FG_DIM"],
                 font=(self.ui, 10)).pack(side="left", fill="x")
        self.status_rows[name] = (dot, oval, var)

    def _set_row(self, name, color, text):
        dot, oval, var = self.status_rows[name]
        dot.itemconfigure(oval, fill=color)
        var.set(text)

    def _tab_control(self, nb):
        page = self._page(nb, "Control")
        _head, body = self._card(page, "Model source", "Choose where agent tasks are sent.")
        self.segment = Segmented(body, self.theme, MODE_LABELS, self.cfg["mode"], self._set_mode)
        self.segment.pack(anchor="w", pady=(4, 8))
        tk.Label(body, textvariable=self.hint_var, bg=self.theme["BG_PANEL"], fg=self.theme["FG_DIM"],
                 font=(self.ui, 9), anchor="w").pack(anchor="w")
        _head, body = self._card(page, "Live status")
        for name in ("OpenRouter", "Requesty", "Gemini", "NVIDIA NIM", "Local model"):
            self._status_row(body, name)
        _head, body = self._card(page, "When agents are off")
        self._label(body, "The MCP server behaves exactly as before. No orchestration and no agent models are used.",
                    anchor="w")

    def _tab_providers(self, nb):
        t = self.theme
        page = self._page(nb, "Providers")
        self.prov_outer = page.master.master
        self.picker_host = None
        _head, body = self._card(page, "OpenRouter", "Use Select models to choose which ones can be used.")
        self._secret_row(body, "API key", self.key_var, "Save key", self._save_key)
        self._models_row(body, "openrouter")

        head, body = self._card(page, "Requesty", "OpenAI-compatible router. Enabled models are tried in the order they were added.")
        self.rq_switch = Switch(head, t, self.cfg["requesty_enabled"], self._toggle_requesty)
        self.rq_switch.pack(side="right")
        self._secret_row(body, "API key", self.rq_key_var)
        self._label(body, "Base URL", anchor="w", pady=(10, 4))
        self._entry(body, self.rq_url_var).pack(fill="x", ipady=7)
        self._models_row(body, "requesty")
        actions = tk.Frame(body, bg=t["BG_PANEL"])
        actions.pack(fill="x", pady=(12, 0))
        self._btn(actions, "Save Requesty", self._save_requesty).pack(side="left")

        head, body = self._card(page, "Gemini", "Google Gemini through its OpenAI-compatible endpoint. Enabled models are tried in the order they were added.")
        self.gm_switch = Switch(head, t, self.cfg["gemini_enabled"], self._toggle_gemini)
        self.gm_switch.pack(side="right")
        self._secret_row(body, "API key", self.gm_key_var)
        self._label(body, "Base URL", anchor="w", pady=(10, 4))
        self._entry(body, self.gm_url_var).pack(fill="x", ipady=7)
        self._models_row(body, "gemini")
        actions = tk.Frame(body, bg=t["BG_PANEL"])
        actions.pack(fill="x", pady=(12, 0))
        self._btn(actions, "Save Gemini", self._save_gemini).pack(side="left")

        head, body = self._card(page, "NVIDIA NIM", "NVIDIA NIM hosted models through the OpenAI-compatible API. Enabled models are tried in the order they were added.")
        self.nv_switch = Switch(head, t, self.cfg["nvidia_enabled"], self._toggle_nvidia)
        self.nv_switch.pack(side="right")
        self._secret_row(body, "API key", self.nv_key_var)
        self._label(body, "Base URL", anchor="w", pady=(10, 4))
        self._entry(body, self.nv_url_var).pack(fill="x", ipady=7)
        self._models_row(body, "nvidia")
        actions = tk.Frame(body, bg=t["BG_PANEL"])
        actions.pack(fill="x", pady=(12, 0))
        self._btn(actions, "Save NVIDIA NIM", self._save_nvidia).pack(side="left")

        _head, body = self._card(page, "ngrok", "A new token takes effect after you restart the server.")
        self._secret_row(body, "Auth token", self.ngrok_var, "Save token", self._save_ngrok)

    def _tab_activity(self, nb):
        t = self.theme
        frame = tk.Frame(nb, bg=t["BG"])
        nb.add(frame, text="Activity")
        holder = tk.Frame(frame, bg=t["BG_LOG"], highlightbackground=t["BORDER"], highlightthickness=1)
        holder.pack(fill="both", expand=True, padx=18, pady=14)
        columns = ("start", "end", "task", "kind", "source", "model", "status")
        self.tree = ttk.Treeview(holder, columns=columns, show="headings", style="Agent.Treeview")
        widths = {"start": 70, "end": 70, "task": 250, "kind": 70, "source": 85, "model": 190, "status": 210}
        for name in columns:
            self.tree.heading(name, text=name.capitalize())
            self.tree.column(name, width=widths[name], anchor="w", stretch=name in ("task", "status"))
        self.tree.tag_configure("ok", foreground=t["ACCENT"])
        self.tree.tag_configure("fail", foreground=self.red)
        self.tree.tag_configure("run", foreground=AMBER)
        bar = ttk.Scrollbar(holder, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._show_detail)

    def _tab_resources(self, nb):
        t = self.theme
        frame = tk.Frame(nb, bg=t["BG"])
        nb.add(frame, text="Resources")
        holder = tk.Frame(frame, bg=t["BG_LOG"], highlightbackground=t["BORDER"], highlightthickness=1)
        holder.pack(fill="both", expand=True, padx=18, pady=14)
        self.res_text = tk.Text(holder, bg=t["BG_LOG"], fg=t["FG"], font=(t["MONO_FONT"], 10),
                                borderwidth=0, padx=16, pady=14, state="disabled")
        self.res_text.pack(fill="both", expand=True)

    def _apply_enabled(self):
        on = bool(self.master.value)
        t = self.theme
        self.pill.configure(text="ON" if on else "OFF", bg=t["ACCENT"] if on else OFF_GREY,
                            fg=DARK_TEXT if on else t["FG_DIM"])
        self.sub_var.set("Orchestration is running" if on else "Switched off. The server runs as normal.")
        self.segment.set_active(on)

    def sync_master(self):
        if self.win is None or not self.win.winfo_exists():
            return
        self.master.set(settings.enabled())
        self._apply_enabled()

    def _toggle_master(self, value):
        settings.save({"enabled": bool(value)})
        self._apply_enabled()
        self._say("Agents on. Applies to the next call, no restart needed." if value else
                  "Agents off. Applies to the next call, no restart needed.")
        if self.on_changed:
            self.on_changed()

    def _set_mode(self, key):
        settings.save({"mode": key})
        self.hint_var.set(MODE_HINTS[key])
        self._say(f"Model source: {MODE_LABELS[key]}.")
        if self.on_changed:
            self.on_changed()

    def _save_key(self):
        settings.save({"openrouter_api_key": self.key_var.get().strip()})
        self._say("OpenRouter key saved.")

    def _toggle_requesty(self, value):
        settings.save({"requesty_enabled": bool(value)})
        self._say("Requesty on." if value else "Requesty off.")

    def _toggle_gemini(self, value):
        settings.save({"gemini_enabled": bool(value)})
        self._say("Gemini on." if value else "Gemini off.")

    def _toggle_nvidia(self, value):
        settings.save({"nvidia_enabled": bool(value)})
        self._say("NVIDIA NIM on." if value else "NVIDIA NIM off.")

    def _save_requesty(self):
        url = self.rq_url_var.get().strip().rstrip("/")
        if not re.match(r"^https?://\S+$", url):
            self._say("Base URL must start with http:// or https://", False)
            return
        saved = settings.save({"requesty_api_key": self.rq_key_var.get().strip(), "requesty_base_url": url})
        self.rq_url_var.set(saved["requesty_base_url"])
        self._say("Requesty saved.")

    def _save_gemini(self):
        url = self.gm_url_var.get().strip().rstrip("/")
        if not re.match(r"^https?://\S+$", url):
            self._say("Base URL must start with http:// or https://", False)
            return
        saved = settings.save({"gemini_api_key": self.gm_key_var.get().strip(), "gemini_base_url": url})
        self.gm_url_var.set(saved["gemini_base_url"])
        self._say("Gemini saved.")

    def _save_nvidia(self):
        url = self.nv_url_var.get().strip().rstrip("/")
        if not re.match(r"^https?://\S+$", url):
            self._say("Base URL must start with http:// or https://", False)
            return
        saved = settings.save({"nvidia_api_key": self.nv_key_var.get().strip(), "nvidia_base_url": url})
        self.nv_url_var.set(saved["nvidia_base_url"])
        self._say("NVIDIA NIM saved.")

    def _provider(self, which):
        if which == "openrouter":
            return "OpenRouter", None, None
        if which == "gemini":
            return "Gemini", self.gm_key_var, self.gm_url_var
        if which == "nvidia":
            return "NVIDIA NIM", self.nv_key_var, self.nv_url_var
        return "Requesty", self.rq_key_var, self.rq_url_var

    def _models_row(self, parent, which):
        t = self.theme
        row = tk.Frame(parent, bg=t["BG_PANEL"])
        row.pack(fill="x", pady=(12, 0))
        var = tk.StringVar()
        self.model_vars[which] = var
        tk.Label(row, textvariable=var, bg=t["BG_PANEL"], fg=t["FG_DIM"], font=(self.ui, 9),
                 anchor="w").pack(side="left")
        free = tk.BooleanVar(value=settings.load()[f"{which}_free_only"])
        self.free_vars[which] = free
        tk.Checkbutton(parent, text="Fetch free models only", variable=free,
                       command=lambda: settings.save({f"{which}_free_only": free.get()}),
                       bg=t["BG_PANEL"], fg=t["FG"], selectcolor="#33353a",
                       activebackground=t["BG_PANEL"], activeforeground=t["ACCENT"], anchor="w",
                       font=(self.ui, 9), highlightthickness=0, borderwidth=0,
                       cursor="hand2").pack(fill="x", anchor="w", pady=(6, 0))
        self._btn(row, "Select models", lambda: self._select_models(which)).pack(side="right")
        self._btn(row, "Fetch models", lambda: self._fetch_models(which), primary=False).pack(side="right", padx=(0, 8))
        self._refresh_models(which)

    def _refresh_models(self, which):
        var = self.model_vars.get(which)
        if var is None:
            return
        enabled = settings.load()[f"{which}_models"]
        stored = len(modelcache.load(which))
        head = "All free models (auto)" if which == "openrouter" and not enabled else f"{len(enabled)} model(s) enabled"
        var.set(f"{head}   |   {stored} fetched")

    def _fetch_models(self, which):
        label, key_var, url_var = self._provider(which)
        key = url = ""
        if which != "openrouter":
            key = key_var.get().strip()
            url = url_var.get().strip().rstrip("/")
            if not key or not url:
                self._say(f"Enter the {label} API key and base URL first.", False)
                return
        free_only = self.free_vars[which].get()
        self._say(f"Fetching {label} models...")

        def work():
            try:
                if which == "openrouter":
                    ids = openrouter.fetch_free_ids(free_only)
                else:
                    ids = openrouter.free_model_ids(which, url, key, free_only)
                ids = sorted(set(ids))
                if not ids:
                    self._pending.append(lambda: self._say(f"{label} returned no models.", False))
                    return
                modelcache.save(which, ids)
                self._pending.append(lambda: self._fetched(which, label, len(ids)))
            except Exception as error:
                message = str(error)[:160]
                self._pending.append(lambda: self._say(f"Could not fetch models: {message}", False))

        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, which, label, count):
        self._refresh_models(which)
        kind = "free " if self.free_vars[which].get() else ""
        self._say(f"{count} {kind}{label} models fetched and stored.")
        self._open_picker(which)

    def _select_models(self, which):
        label = self._provider(which)[0]
        if not modelcache.load(which) and not settings.load()[f"{which}_models"]:
            self._say(f"No {label} models stored yet. Click Fetch models first.", False)
            return
        self._open_picker(which)

    def _close_picker(self):
        host = self.picker_host
        self.picker_host = None
        if host is not None and host.winfo_exists():
            host.destroy()

    def _open_picker(self, which):
        t = self.theme
        label = self._provider(which)[0]
        enabled = list(settings.load()[f"{which}_models"])
        stored = modelcache.load(which)
        if which == "openrouter" and not enabled:
            enabled = list(stored)
        all_ids = sorted(set(stored) | set(enabled))
        checks = {m: tk.BooleanVar(value=m in enabled) for m in all_ids}
        stamp = modelcache.fetched_at(which)
        when = time.strftime("fetched %d %b %H:%M", time.localtime(stamp)) if stamp else "not fetched yet"
        note = "Only free models are listed. " if self.free_vars[which].get() else ""

        self._close_picker()
        try:
            self.notebook.select(self.prov_outer)
        except tk.TclError:
            pass
        host = tk.Frame(self.prov_outer, bg=t["BG"])
        host.place(x=0, y=0, relwidth=1, relheight=1)
        host.lift()
        self.picker_host = host

        filter_var = tk.StringVar()
        counter = tk.StringVar()
        add_var = tk.StringVar()

        def close():
            canvas.unbind_all("<MouseWheel>")
            self._close_picker()

        head = tk.Frame(host, bg=t["BG"])
        head.pack(fill="x", padx=18, pady=(10, 2))
        self._btn(head, "\u2190  Providers", close, primary=False).pack(side="left")
        tk.Label(head, text=f"{label} models", bg=t["BG"], fg=t["FG"], font=(self.ui, 13, "bold")).pack(side="left", padx=(14, 0))
        tk.Label(head, textvariable=counter, bg=t["BG"], fg=t["ACCENT"], font=(self.ui, 10, "bold")).pack(side="right")
        tk.Label(host, text=f"{note}Tick the models agents may use ({when}).", bg=t["BG"], fg=t["FG_DIM"],
                 font=(self.ui, 9), anchor="w").pack(fill="x", padx=18, pady=(0, 6))
        bar = tk.Frame(host, bg=t["BG"])
        bar.pack(fill="x", padx=18, pady=(0, 6))
        foot = tk.Frame(host, bg=t["BG"])
        foot.pack(side="bottom", fill="x", padx=18, pady=(8, 10))
        holder = tk.Frame(host, bg=t["BG_LOG"], highlightbackground=t["BORDER"], highlightthickness=1)
        holder.pack(fill="both", expand=True, padx=18)
        canvas = tk.Canvas(holder, bg=t["BG_LOG"], highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=t["BG_LOG"])
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def wheel(event):
            try:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            except tk.TclError:
                pass

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def count():
            counter.set(f"{sum(1 for v in checks.values() if v.get())} of {len(checks)} selected")

        def refill(*_):
            needle = filter_var.get().strip().lower()
            for child in inner.winfo_children():
                child.destroy()
            for model_id in all_ids:
                if needle in model_id.lower():
                    tk.Checkbutton(inner, text=model_id, variable=checks[model_id], command=count,
                                   bg=t["BG_LOG"], fg=t["FG"], selectcolor="#33353a",
                                   activebackground=t["BG_LOG"], activeforeground=t["ACCENT"], anchor="w",
                                   font=(t["MONO_FONT"], 10), highlightthickness=0, borderwidth=0,
                                   cursor="hand2").pack(fill="x", anchor="w", padx=8, pady=2)
            canvas.yview_moveto(0)

        def mark(value):
            needle = filter_var.get().strip().lower()
            for model_id in all_ids:
                if needle in model_id.lower():
                    checks[model_id].set(value)
            count()

        def add_manual():
            name = add_var.get().strip()
            if which == "gemini" and name.startswith("models/"):
                name = name[7:]
            if not name:
                return
            if name in checks:
                checks[name].set(True)
            else:
                checks[name] = tk.BooleanVar(value=True)
                all_ids.append(name)
                all_ids.sort()
            add_var.set("")
            filter_var.set("")
            refill()
            count()

        def save():
            chosen = [m for m in all_ids if checks[m].get()]
            if which == "openrouter" and not chosen:
                self._say("Select at least one OpenRouter model.", False)
                return
            ordered = [m for m in enabled if m in chosen] + [m for m in chosen if m not in enabled]
            settings.save({f"{which}_models": ordered})
            self._refresh_models(which)
            self._say(f"{label}: {len(ordered)} model(s) enabled.")
            close()

        self._btn(bar, "Clear shown", lambda: mark(False), primary=False).pack(side="right")
        self._btn(bar, "Select shown", lambda: mark(True), primary=False).pack(side="right", padx=(0, 8))
        self._entry(bar, filter_var).pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self._btn(foot, "Save selection", save).pack(side="right")
        self._btn(foot, "Cancel", close, primary=False).pack(side="right", padx=(0, 8))
        tk.Label(foot, text="Add model id", bg=t["BG"], fg=t["FG_DIM"], font=(self.ui, 9)).pack(side="left", padx=(0, 8))
        add_entry = self._entry(foot, add_var)
        add_entry.pack(side="left", fill="x", expand=True, ipady=6)
        add_entry.bind("<Return>", lambda _e: add_manual())
        self._btn(foot, "Add", add_manual, primary=False).pack(side="left", padx=(8, 16))
        filter_var.trace_add("write", refill)
        refill()
        count()

    def _save_ngrok(self):
        token = self.ngrok_var.get().strip()
        if not token or re.search(r"[\s\"']", token):
            messagebox.showerror("ngrok token", "That does not look like an ngrok auth token.", parent=self.win)
            return
        try:
            write_ngrok_token(token)
        except OSError as error:
            messagebox.showerror("ngrok token", f"Could not save: {error}", parent=self.win)
            return
        self._say("ngrok token saved. Restart the server to use it.")

    def _show_detail(self, _event):
        selected = self.tree.selection()
        if not selected:
            return
        t = self.theme
        row = self.rows.get(selected[0], {})
        top = tk.Toplevel(self.win)
        top.title("Task " + selected[0])
        top.geometry("680x460")
        top.configure(bg=t["BG"])
        holder = tk.Frame(top, bg=t["BG_LOG"], highlightbackground=t["BORDER"], highlightthickness=1)
        holder.pack(fill="both", expand=True, padx=14, pady=14)
        text = tk.Text(holder, bg=t["BG_LOG"], fg=t["FG"], wrap="word", font=(t["MONO_FONT"], 10),
                       borderwidth=0, padx=14, pady=12)
        text.pack(fill="both", expand=True)
        body = f"Task:\n{row.get('task', '')}\n\nStatus: {row.get('status', '')}\n"
        if row.get("error"):
            body += f"Last error: {row['error']}\n"
        if row.get("ms"):
            body += f"Duration: {row['ms'] / 1000:.1f}s  tokens: {row.get('pt', 0)} in / {row.get('ct', 0)} out\n"
        body += f"\nResult:\n{row.get('result', '(none yet)')}"
        text.insert("1.0", body)
        text.configure(state="disabled")

    def _ingest(self, entries):
        for e in entries:
            tid = e.get("id")
            if not tid:
                continue
            row = self.rows.setdefault(tid, {"t0": e.get("t")})
            phase = e.get("phase")
            if phase == "start":
                row["t0"] = e.get("t")
                row["task"] = e.get("task", "")
            for key in ("kind", "backend", "model", "status", "error"):
                if e.get(key):
                    row[key] = e[key]
            if phase == "end":
                row["t1"] = e.get("t")
                row["ms"] = e.get("ms")
                row["result"] = e.get("result", "")
                row["ok"] = e.get("ok")
                row["pt"] = e.get("prompt_tokens", 0)
                row["ct"] = e.get("completion_tokens", 0)
        while len(self.rows) > MAX_ROWS:
            oldest = next(iter(self.rows))
            self.rows.pop(oldest)
            if self.tree.exists(oldest):
                self.tree.delete(oldest)
        for tid in {e.get("id") for e in entries if e.get("id")} & set(self.rows):
            row = self.rows[tid]
            values = (_clock(row.get("t0")), _clock(row.get("t1")), (row.get("task") or "").replace("\n", " ")[:120],
                      row.get("kind", ""), _where(row.get("backend", "")), row.get("model", ""),
                      row.get("status", ""))
            tag = "ok" if row.get("ok") is True else "fail" if row.get("ok") is False else "run"
            if self.tree.exists(tid):
                self.tree.item(tid, values=values, tags=(tag,))
            else:
                self.tree.insert("", 0, iid=tid, values=values, tags=(tag,))

    def _tick(self):
        if not self._alive or self.win is None:
            return
        try:
            while self._pending:
                self._pending.pop(0)()
            if settings.enabled() != self.master.value:
                self.sync_master()
            entries, self.offset = activity.read_since(self.offset)
            if entries:
                self._ingest(entries)
            self._render_state()
            self._render_resources()
        except Exception:
            pass
        self.win.after(1000, self._tick)

    def _snap_loop(self):
        if not self._alive:
            return
        if not self._snap_busy:
            self._snap_busy = True
            pid = (activity.read_state().get("laya") or {}).get("pid")

            def work():
                try:
                    self.snapshot = resources.snapshot(pid)
                except Exception:
                    pass
                finally:
                    self._snap_busy = False

            threading.Thread(target=work, daemon=True).start()
        self.win.after(2500, self._snap_loop)

    def _render_state(self):
        t = self.theme
        cfg = settings.load()
        state = activity.read_state()
        orr = state.get("openrouter") or {}
        local = state.get("local") or {}
        cloud_off = cfg["mode"] == "local"
        if cloud_off:
            self._set_row("OpenRouter", OFF_GREY, "Not used in local-only mode")
        elif not cfg["openrouter_api_key"]:
            self._set_row("OpenRouter", OFF_GREY, "No API key")
        elif orr.get("blocked_s"):
            self._set_row("OpenRouter", self.red,
                          f"Blocked {orr['blocked_s']}s ({orr.get('blocked_reason', '')})")
        else:
            text = f"{orr.get('used', 0)}/{orr.get('rpm', cfg['openrouter_rpm'])} requests this minute"
            if orr.get("cooling"):
                text += f", {len(orr['cooling'])} model(s) cooling"
            self._set_row("OpenRouter", t["ACCENT"], text)
        if cloud_off:
            self._set_row("Requesty", OFF_GREY, "Not used in local-only mode")
        elif not cfg["requesty_enabled"]:
            self._set_row("Requesty", OFF_GREY, "Off")
        elif not cfg["requesty_api_key"] or not cfg["requesty_models"]:
            self._set_row("Requesty", AMBER, "Needs an API key and at least one model")
        else:
            self._set_row("Requesty", t["ACCENT"], f"{len(cfg['requesty_models'])} model(s) ready")
        if cloud_off:
            self._set_row("Gemini", OFF_GREY, "Not used in local-only mode")
        elif not cfg["gemini_enabled"]:
            self._set_row("Gemini", OFF_GREY, "Off")
        elif not cfg["gemini_api_key"] or not cfg["gemini_models"]:
            self._set_row("Gemini", AMBER, "Needs an API key and at least one model")
        else:
            self._set_row("Gemini", t["ACCENT"], f"{len(cfg['gemini_models'])} model(s) ready")
        if cloud_off:
            self._set_row("NVIDIA NIM", OFF_GREY, "Not used in local-only mode")
        elif not cfg["nvidia_enabled"]:
            self._set_row("NVIDIA NIM", OFF_GREY, "Off")
        elif not cfg["nvidia_api_key"] or not cfg["nvidia_models"]:
            self._set_row("NVIDIA NIM", AMBER, "Needs an API key and at least one model")
        else:
            self._set_row("NVIDIA NIM", t["ACCENT"], f"{len(cfg['nvidia_models'])} model(s) ready")
        if cfg["mode"] == "openrouter":
            self._set_row("Local model", OFF_GREY, "Not used in cloud-only mode")
        else:
            phase = local.get("phase", "idle")
            self._set_row("Local model", t["ACCENT"] if phase not in ("idle", "-") else OFF_GREY,
                          f"{local.get('model') or '-'}  ({phase})")

    def _render_resources(self):
        snap = self.snapshot or {}
        out = []
        sysinfo = snap.get("system") or {}
        if sysinfo:
            out.append(f"System   CPU {sysinfo['cpu']:.0f}%   RAM {sysinfo['ram_used_gb']:.1f}/"
                       f"{sysinfo['ram_total_gb']:.1f} GB ({sysinfo['ram_percent']:.0f}%)")
        gpu = snap.get("gpu")
        if gpu:
            extra = ""
            if gpu.get("temp_c") is not None:
                extra += f"   {gpu['temp_c']:.0f} C"
            if gpu.get("power_w") is not None:
                extra += f"   {gpu['power_w']:.0f} W"
            out.append(f"GPU      {gpu['name']}   util {gpu['util']:.0f}%   VRAM {gpu['used_mb']:.0f}/"
                       f"{gpu['total_mb']:.0f} MB{extra}")
        else:
            out.append("GPU      not detected")
        out.append("")
        for key, title in (("lmstudio", "LM Studio"), ("laya", "Laya worker")):
            group = snap.get(key)
            if group:
                vram = f"{group['vram_mb']:.0f} MB" if group.get("vram_mb") is not None else "n/a"
                out.append(f"{title:<12} CPU {group['cpu']:.0f}%   RAM {group['ram_mb']:.0f} MB   VRAM {vram}   "
                           f"pids {','.join(map(str, group['pids'][:6]))}")
            else:
                out.append(f"{title:<12} not running")
        local = (activity.read_state().get("local") or {})
        out.append("")
        out.append(f"Loaded by agent: {', '.join(local.get('we_loaded') or []) or 'nothing'}")
        out.append(f"Server started by agent: {bool(local.get('we_started_server'))}")
        self.res_text.configure(state="normal")
        self.res_text.delete("1.0", "end")
        self.res_text.insert("1.0", "\n".join(out))
        self.res_text.configure(state="disabled")
