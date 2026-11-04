"""Talks to the Laya worker process: starts it on demand, stops it when idle."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import app_paths

from . import activity, settings
from .openrouter import BackendError

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
WORKER = (app_paths.resource_dir() / "agent" / "laya_worker.py") if app_paths.FROZEN else Path(__file__).with_name("laya_worker.py")
REQUIRED = "import torch, transformers, safetensors, huggingface_hub, numpy"


class LayaBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._python: str | None = None
        self._python_checked = False
        self._counter = 0
        self.last_used = 0.0
        self.device = ""
        self.error = ""

    def _candidates(self) -> list[str]:
        found = []
        configured = settings.load()["laya_python"].strip() or os.environ.get("CODY_LAYA_PYTHON", "").strip()
        if configured:
            found.append(configured)
        if not app_paths.FROZEN:
            found.append(sys.executable)
            root = Path(__file__).resolve().parent.parent
            found.append(str(root / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")))
        for name in ("python", "python3"):
            path = shutil.which(name)
            if path:
                found.append(path)
        unique = []
        for item in found:
            if item and item not in unique and Path(item).is_file():
                unique.append(item)
        return unique

    def python(self) -> str | None:
        if self._python_checked:
            return self._python
        for candidate in self._candidates():
            try:
                done = subprocess.run([candidate, "-c", REQUIRED], capture_output=True, timeout=90,
                                      creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0:
                self._python = candidate
                break
        self._python_checked = True
        return self._python

    def _reader(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                self._lines.put(json.loads(raw))
            except ValueError:
                continue
        self._lines.put({"closed": True})

    def _start(self) -> None:
        cfg = settings.load()
        python = self.python()
        if not python:
            raise BackendError("gone", "no Python with torch + transformers found for Laya (set laya_python)")
        if not WORKER.is_file():
            raise BackendError("gone", f"Laya worker missing: {WORKER}")
        env = {**os.environ, "OMP_NUM_THREADS": str(cfg["laya_threads"]), "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1"}
        if not cfg["laya_allow_download"]:
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
        self._lines = queue.Queue()
        try:
            app_paths.logs_dir().mkdir(parents=True, exist_ok=True)
            log = open(app_paths.logs_dir() / "laya_worker.log", "a", encoding="utf-8")
            self._proc = subprocess.Popen(
                [python, str(WORKER), cfg["laya_checkpoint"], str(cfg["laya_threads"])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                env=env, creationflags=CREATE_NO_WINDOW, cwd=str(WORKER.parent))
        except OSError as error:
            raise BackendError("gone", f"could not start Laya worker: {error}") from None
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
        activity.write_state({"laya": {"phase": "loading", "pid": self._proc.pid}})
        try:
            message = self._lines.get(timeout=180)
        except queue.Empty:
            self._stop()
            raise BackendError("transient", "Laya worker did not become ready in 180s") from None
        if not message.get("ready"):
            self._stop()
            raise BackendError("gone", "Laya failed to load: " + str(message.get("error") or "worker exited"))
        self.device = str(message.get("device", "cpu"))
        self.last_used = time.time()
        activity.write_state({"laya": {"phase": "loaded", "pid": self._proc.pid, "device": self.device}})

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()
        activity.write_state({"laya": {"phase": "unloaded", "pid": None}})

    def predict(self, state, questions: dict, timeout: float = 30.0) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            self._counter += 1
            request_id = self._counter
            try:
                self._proc.stdin.write(json.dumps({"id": request_id, "state": state, "questions": questions}) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._stop()
                raise BackendError("transient", "Laya worker pipe closed") from None
            deadline = time.time() + timeout
            while True:
                try:
                    message = self._lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    self._stop()
                    raise BackendError("transient", "Laya prediction timed out") from None
                if message.get("closed"):
                    self._stop()
                    raise BackendError("transient", "Laya worker exited")
                if message.get("id") == request_id:
                    break
            self.last_used = time.time()
            if message.get("error"):
                raise BackendError("bad", "Laya: " + str(message["error"]))
            return message["answers"]

    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def reap_if_idle(self, force: bool = False) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._proc is None:
                return
            if force or time.time() - self.last_used >= settings.load()["laya_idle_seconds"]:
                self._stop()
        finally:
            self._lock.release()

    def shutdown(self) -> None:
        with self._lock:
            self._stop()


LAYA = LayaBridge()
