"""LM Studio control: start its server headless, load a model sized to what is free,
call it, and unload it again when idle. Uses the `lms` CLI plus the OpenAI-compatible API.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import activity, resources, settings
from .openrouter import BackendError

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
GPU_HEADROOM_MB = 600
OVERHEAD = 1.15


def _http(method: str, url: str, body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


class LMStudio:
    def __init__(self):
        self._lock = threading.RLock()
        self._generate = threading.Semaphore(1)
        self.last_used = 0.0
        self.we_started_server = False
        self.we_loaded: set[str] = set()
        self.model_id = ""
        self.last_error = ""
        self.active = 0
        self._ls_cache: tuple[float, list] = (0.0, [])

    def lms_path(self) -> str | None:
        configured = settings.load()["lms_path"]
        if configured and Path(configured).is_file():
            return configured
        found = shutil.which("lms")
        if found:
            return found
        home = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform == "win32" else "lms")
        return str(home) if home.is_file() else None

    def _cli(self, args: list[str], timeout: float = 60.0, stdout_only: bool = False) -> tuple[int, str]:
        exe = self.lms_path()
        if not exe:
            return 127, "lms CLI not found"
        try:
            done = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout,
                                  creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
            return done.returncode, ((done.stdout or "") if stdout_only else (done.stdout or "") + (done.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            return 124, f"lms {' '.join(args)} timed out after {int(timeout)}s"
        except OSError as error:
            return 126, str(error)

    def _base(self) -> str:
        return settings.load()["lmstudio_url"].rstrip("/")

    def server_up(self) -> bool:
        try:
            _http("GET", self._base() + "/v1/models", timeout=1.5)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def installed(self, refresh: bool = False) -> list[dict]:
        stamp, cached = self._ls_cache
        if cached and not refresh and time.time() - stamp < 120:
            return cached
        code, out = self._cli(["ls", "--json"], 30, True)
        models: list[dict] = []
        if code == 0:
            try:
                models = [m for m in json.loads(out) if m.get("type", "llm") == "llm"]
            except ValueError:
                models = []
        self._ls_cache = (time.time(), models)
        return models

    def loaded(self) -> list[dict]:
        code, out = self._cli(["ps", "--json"], 20, True)
        if code != 0:
            return []
        try:
            return json.loads(out)
        except ValueError:
            return []

    def pick_model(self) -> str:
        wanted = settings.load()["local_model"].strip()
        if wanted:
            return wanted
        models = self.installed()
        if not models:
            raise BackendError("gone", "no local LLM found in LM Studio (lms ls is empty)")
        return min(models, key=lambda m: m.get("sizeBytes") or 1 << 60)["modelKey"]

    def ensure_server(self) -> None:
        if self.server_up():
            return
        if not self.lms_path():
            raise BackendError("gone", "LM Studio server is not running and the lms CLI was not found")
        url = self._base()
        port = url.rsplit(":", 1)[-1].strip("/") if url.rsplit(":", 1)[-1].strip("/").isdigit() else "1234"
        activity.write_state({"local": {"phase": "starting LM Studio server"}})
        code, out = self._cli(["server", "start", "--port", port], 90)
        deadline = time.time() + 45
        while time.time() < deadline and not self.server_up():
            time.sleep(1.0)
        if not self.server_up():
            raise BackendError("transient", f"could not start LM Studio server (lms exit {code}: {out[-200:]})")
        self.we_started_server = True

    def _gpu_ratio(self, size_bytes: int) -> tuple[str, float]:
        free = resources.gpu_free_mb()
        if free is None:
            return "off", 0.0
        need = max(size_bytes / 1e6 * OVERHEAD, 1.0)
        ratio = (free - GPU_HEADROOM_MB) / need
        if ratio >= 1.0:
            return "max", 1.0
        if ratio < 0.12:
            return "off", 0.0
        return f"{ratio:.2f}", ratio

    def ensure_loaded(self, key: str) -> str:
        cfg = settings.load()
        for model in self.loaded():
            if key in (model.get("identifier"), model.get("modelKey")):
                return model.get("identifier") or key
        entry = next((m for m in self.installed() if m.get("modelKey") == key), None)
        size = int((entry or {}).get("sizeBytes") or 4_000_000_000)
        gpu_flag, ratio = self._gpu_ratio(size)
        available = resources.ram_available_gb()
        need_gb = size / 1e9 * OVERHEAD * (1.0 - ratio) + 0.8
        if available is not None and available - need_gb < cfg["local_min_free_ram_gb"]:
            raise BackendError(
                "resource",
                f"not enough free RAM for the local model ({available:.1f} GB free, needs about {need_gb:.1f} GB "
                f"and {cfg['local_min_free_ram_gb']:.1f} GB must stay free)")
        activity.write_state({"local": {"phase": f"loading {key} (gpu {gpu_flag})"}})
        args = ["load", key, "--gpu", gpu_flag, "-c", str(cfg["local_context"]),
                "--ttl", str(cfg["local_idle_seconds"] + 30), "-y"]
        code, out = self._cli(args, 420)
        if code != 0:
            raise BackendError("transient", f"lms load failed (exit {code}): {out[-240:]}")
        self.we_loaded.add(key)
        return key

    def chat(self, messages: list[dict], max_tokens: int | None = None, timeout: float | None = None) -> dict:
        cfg = settings.load()
        self.active += 1
        try:
            return self._chat(cfg, messages, max_tokens, timeout)
        finally:
            self.active -= 1
            self.last_used = time.time()

    def _chat(self, cfg: dict, messages: list[dict], max_tokens: int | None, timeout: float | None) -> dict:
        with self._generate:
            with self._lock:
                self.ensure_server()
                key = self.pick_model()
                identifier = self.ensure_loaded(key)
                self.model_id = identifier
            body = {
                "model": identifier,
                "messages": messages,
                "max_tokens": max_tokens or cfg["local_max_tokens"],
                "temperature": 0.2,
                "stream": False,
            }
            activity.write_state({"local": {"phase": "generating"}})
            try:
                payload = _http("POST", self._base() + "/v1/chat/completions", body, timeout or cfg["local_timeout"])
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:240]
                raise BackendError("transient", f"LM Studio HTTP {error.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise BackendError("transient", f"LM Studio unreachable: {getattr(error, 'reason', error)}") from None
            finally:
                self.last_used = time.time()
                self._publish()
        try:
            text = (payload["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BackendError("empty", "LM Studio returned no choices") from None
        if not text:
            raise BackendError("empty", "local model returned empty content")
        usage = payload.get("usage") or {}
        return {
            "text": text,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "model": identifier,
        }

    def _publish(self) -> None:
        activity.write_state({"local": {
            "phase": "idle", "model": self.model_id, "we_loaded": sorted(self.we_loaded),
            "we_started_server": self.we_started_server, "last_used": self.last_used,
        }})

    def reap_if_idle(self, force: bool = False) -> None:
        cfg = settings.load()
        with self._lock:
            if not self.we_loaded and not self.we_started_server:
                return
            if self.active > 0:
                return
            idle = time.time() - self.last_used
            if not force and idle < cfg["local_idle_seconds"]:
                return
            for key in sorted(self.we_loaded):
                self._cli(["unload", key], 60)
            self.we_loaded.clear()
            if self.we_started_server and not self.loaded():
                self._cli(["server", "stop"], 30)
                self.we_started_server = False
            activity.write_state({"local": {
                "phase": "unloaded (idle)", "we_loaded": [], "we_started_server": self.we_started_server}})

    def ping(self) -> str:
        """Used by the GUI test button: start the server if needed and report what is installed."""
        self.ensure_server()
        names = [m.get("modelKey") for m in self.installed(True)]
        return "LM Studio reachable; installed: " + (", ".join(names) or "none")


def cleanup_from_state() -> None:
    """Called when the server is stopped from the GUI (the server process is killed hard and
    cannot clean up itself): unload what this server loaded and stop a server it started."""
    local = activity.read_state().get("local") or {}
    controller = LMStudio()
    controller.we_loaded = set(local.get("we_loaded") or [])
    controller.we_started_server = bool(local.get("we_started_server"))
    controller.reap_if_idle(force=True)


LOCAL = LMStudio()
